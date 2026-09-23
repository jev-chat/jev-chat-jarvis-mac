"""Settings persistence and wire-level checks, no credentials or external services."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import userconfig
import settings_config as config


class SettingsFiles(unittest.TestCase):
    def test_preserves_comments_unknown_and_shell_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "env"
            original = '# comment\nexport OPENAI_API_KEY="old" # key comment\nJEV_TONES="名字=说明"\nOPENAI_API_KEY=duplicate\nCUSTOM=keep\n'
            path.write_text(original)
            value = 'a\'b"$HOME$(echo SHOULD_NOT_RUN)`echo BAD` # c\\d'
            text = config.write_settings(path, original, {"OPENAI_API_KEY": value, "OPENAI_MODEL": "模型"})
            self.assertIn('# key comment', text)
            self.assertIn('JEV_TONES="名字=说明"\n', text)
            self.assertIn('CUSTOM=keep\n', text)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(userconfig.parse_env_file(path)["OPENAI_API_KEY"], value)
            result = subprocess.check_output(['zsh', '-c', 'source "$1"; print -rn -- "$OPENAI_API_KEY"', 'test', str(path)], text=True)
            self.assertEqual(result, value)

    def test_conflicting_edit_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "env"
            path.write_text('new content')
            with self.assertRaises(ValueError):
                config.write_settings(path, 'old content', {'OPENAI_MODEL': 'test'})
            self.assertEqual(path.read_text(), 'new content')

    def test_no_partial_save_on_invalid_value(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "env"
            with self.assertRaises(ValueError):
                config.write_settings(path, '', {'OPENAI_API_KEY': 'a\nb'})
            self.assertFalse(path.exists())

    def test_parse_empty_quotes_comments_and_literals(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'env'
            path.write_text('A="" # empty\nB="abc # def" # outside\nC=abc#def\nD=\'$(security find-generic-password)\'\n')
            self.assertEqual(userconfig.parse_env_file(path), {'A': '', 'B': 'abc # def', 'C': 'abc#def', 'D': '$(security find-generic-password)'})

    def test_custom_provider_format_and_priority(self):
        from generate import load_credentials
        oai = {'key': '', 'base': '', 'model': '', 'source': 'none'}
        anth = {'key': 'custom', 'base': 'https://example.invalid/gateway', 'model': 'model', 'source': 'test'}
        # Third group added by the OrcaRouter integration: it is consulted last, so this
        # mock answers "no key" for it and the two-provider precedence below is unchanged.
        orca = {'key': '', 'base': '', 'model': '', 'source': 'none'}
        providers = {'OPENAI': oai, 'ANTHROPIC': anth, 'ORCAROUTER': orca}
        with patch.object(userconfig, 'provider', side_effect=lambda p: providers[p]):
            self.assertEqual(load_credentials()[-1], 'anthropic')
            oai.update(key='own', base='https://example.invalid/anthropic-name', model='other')
            self.assertEqual(load_credentials()[-1], 'openai')
            self.assertEqual(load_credentials()[2], 'other')

    def test_running_config_stays_until_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'env'
            path.write_text('OPENAI_API_KEY=old\nOPENAI_MODEL=before\n')
            with patch.dict(os.environ, {}, clear=True), patch.object(userconfig, '_startup_sources', None), patch.object(userconfig, 'env_files', return_value=[path]), patch.object(userconfig, 'PROJECT_ENV', Path(d) / '.env'):
                userconfig.load()
                self.assertNotEqual(userconfig.provider('OPENAI')['source'], '环境变量')
                config.write_settings(path, path.read_text(), {'OPENAI_API_KEY': 'new', 'OPENAI_MODEL': 'after'})
                self.assertEqual(userconfig.provider('OPENAI')['model'], 'before')
                with patch.object(userconfig, '_startup_sources', None), patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(userconfig.provider('OPENAI')['model'], 'after')


class Server(BaseHTTPRequestHandler):
    requests = []
    response = {}
    code = 200

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.respond(None)

    def do_POST(self):
        self.respond(json.loads(self.rfile.read(int(self.headers['Content-Length']))))

    def respond(self, body):
        self.requests.append((self.path, dict(self.headers), body))
        self.send_response(self.code)
        self.end_headers()
        self.wfile.write(json.dumps(self.response).encode())


class SettingsNetwork(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Server)
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join()

    def setUp(self):
        Server.requests = []
        Server.code = 200

    def test_dynamic_models_and_versioned_custom_base(self):
        Server.response = {'data': [{'id': 'actual-model'}, {'id': 'actual-model'}, {'id': 'new-model'}]}
        self.assertEqual(config.list_models('OPENAI', self.base + '/gateway/v4', 'draft-key'), ['actual-model', 'new-model'])
        self.assertEqual(Server.requests[0][0], '/gateway/v4/models')
        self.assertEqual(Server.requests[0][1]['authorization'], 'Bearer draft-key')

    def test_jev_models_follow_typesafe_schema(self):
        Server.response = {'models': [
            {'name': 'jev-latest', 'description': 'Stable', 'release_date': '2026-09-15'},
            {'name': 'jev-preview', 'description': 'Preview', 'release_date': '2026-09-15'},
        ]}
        self.assertEqual(config.list_models('TYPESAFE', self.base, 'draft-key'),
                         ['jev-latest', 'jev-preview'])
        self.assertEqual(Server.requests[0][0], '/v1/models')
        self.assertEqual(Server.requests[0][1]['authorization'], 'Bearer draft-key')

    def test_anthropic_models_headers(self):
        Server.response = {'data': [{'id': 'model'}]}
        config.list_models('ANTHROPIC', self.base, 'draft-key')
        self.assertEqual(Server.requests[0][0], '/v1/models')
        self.assertEqual(Server.requests[0][1]['x-api-key'], 'draft-key')

    def test_unsaved_generation_values_and_body(self):
        Server.response = {'choices': [{'message': {'content': '连接成功'}}]}
        with patch('generate.load_credentials', side_effect=AssertionError('must not use saved credentials')):
            config.test_connection('OPENAI', self.base + '/v1', 'draft-key', 'draft-model', {'enable_thinking': False})
        path, headers, body = Server.requests[0]
        self.assertEqual(path, '/v1/chat/completions')
        self.assertEqual(body['model'], 'draft-model')
        self.assertFalse(body['enable_thinking'])
        self.assertEqual(headers['authorization'], 'Bearer draft-key')

    def test_anthropic_and_jev_real_response_shape(self):
        Server.response = {'content': [{'type': 'text', 'text': '连接成功'}]}
        config.test_connection('ANTHROPIC', self.base, 'key', 'model')
        self.assertEqual(Server.requests[-1][0], '/v1/messages')
        Server.response = {'answers': {'test': {'choice': '问候'}}}
        config.test_connection('TYPESAFE', self.base, 'key', 'model')
        self.assertEqual(Server.requests[-1][0], '/v1/systemone')

    def test_empty_and_thinking_are_not_success(self):
        for response in ({}, {'choices': [{'message': {'content': '', 'reasoning_content': 'thinking'}}]}):
            Server.response = response
            with self.assertRaises(Exception):
                config.test_connection('OPENAI', self.base, 'key', 'model')

    def test_no_redirect_or_fallback_and_no_secret_in_error(self):
        for status in (302, 401, 403, 500):
            Server.code = status
            Server.response = {'error': 'SECRET'}
            with self.assertRaises(Exception) as caught:
                config.list_models('OPENAI', self.base, 'SECRET')
            msg = config.error_message(caught.exception)
            self.assertIn(str(status), msg)
            self.assertNotIn('SECRET', msg)
        self.assertEqual(len(Server.requests), 4)


if __name__ == '__main__':
    unittest.main()
