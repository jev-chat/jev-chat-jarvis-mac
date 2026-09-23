"""HUD-to-HTTP acceptance: synthetic conversations, local server, no user data."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_hud_reply as fixtures
import test_settings as network
import chat_context
import generate
import judge
import judge_jev
import styles


class ContextRequests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        network.SettingsNetwork.setUpClass()

    @classmethod
    def tearDownClass(cls):
        network.SettingsNetwork.tearDownClass()

    def test_hud_all_paths_use_identical_bounded_context_on_wire(self):
        fixture = fixtures.HudReplyTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        h = fixture.h
        with tempfile.TemporaryDirectory() as directory, patch('generate.load_credentials', return_value=(
                network.SettingsNetwork.base, 'synthetic-key', 'synthetic-model', 'test', 'openai')), \
                patch('userconfig.get', return_value=''):
            h.conversations = chat_context.Conversations(Path(directory) / 'chats.json')
            h.configure_context(True, '20')
            h.save_background('chat', '合成背景：小王负责验收')
            msgs = fixture.read([fixtures.block('合成先前消息', .40, .80, .15),
                                 fixtures.block('合成当前消息', .40, .60, .15)])
            tone = styles.DEFAULT_SLOTS[0]
            h.slot_tones = [tone]
            h.generator = generate.Generator()
            h.judge = judge_jev.JevJudge(base=network.SettingsNetwork.base, key='test', model='test')
            network.Server.code = 200
            network.Server.response = {'choices': [{'message': {'content': '1. 合成候选甲\n2. 合成候选乙'}}],
                               'answers': {'intent': {'choice': '闲聊', 'confidence': .9},
                                           'risk': {'score': 1},
                                           'best': {'choice': '合成候选甲', 'confidence': .8}}}
            network.Server.requests = []
            context = h._prejudge_req[1]
            # Resident loops execute real adapters, with only their wait boundary replaced.
            class StopLoop(BaseException):
                pass
            for name, event in [('_prejudge_loop', '_prejudge_event'), ('_pregen_loop', '_pregen_event')]:
                signal = Mock()
                signal.wait.side_effect = [None, StopLoop()]
                setattr(h, event, signal)
                if name == '_pregen_loop':
                    h._pregen_req = (msgs[-1].text, context, (tone,), h._reply_epoch)
                with self.assertRaises(StopLoop):
                    getattr(h, name)()
            h._pregen_result = None
            h._reply_task(h._reply_epoch, h._analyze, msgs[-1], msgs, '')
            h._reply_task(h._reply_epoch, h._run_generation, msgs[-1], msgs,
                          {'intent': '闲聊'})
            h._reply_task(h._reply_epoch, h._regen_work, msgs[-1].text, '闲聊', [tone])
            h._reply_task(h._reply_epoch, h._regenerate_work, msgs[-1].text, '闲聊', [tone])
            requests = list(network.Server.requests)
            self.assertGreaterEqual(len(requests), 11)
            for path, _headers, body in requests:
                self.assertIn(path, ['/v1/chat/completions', '/v1/systemone'])
                text = body['state'] if 'state' in body else body['messages'][0]['content']
                self.assertIn(context, text)
                self.assertEqual(text.count('合成当前消息'), 1)
                self.assertEqual(text.count('合成先前消息'), 1)
            self.assertTrue(any('best' in r[2].get('questions', {}) for r in requests))
            self.assertTrue(any('intent' in r[2].get('questions', {}) for r in requests))
            # HTTP bodies may echo every private field. Neither HUD logs nor errors may do so.
            network.Server.code = 500
            network.Server.response = {'error': '合成隐私标记：消息、背景、候选'}
            logs = []
            with patch.dict(fixtures.HUD, {'_log': logs.append}):
                h._reply_task(h._reply_epoch, h._regen_work, msgs[-1].text, '闲聊', [tone])
                h._reply_task(h._reply_epoch, h._regenerate_work, msgs[-1].text, '闲聊', [tone])
            self.assertNotIn('合成隐私标记', str(logs) + str(fixture.queue))

    def test_local_fallback_forwards_same_context_to_judgment_and_rank(self):
        context = '合成历史与背景'
        fallback = judge.FallbackJudge.__new__(judge.FallbackJudge)
        fallback.primary = Mock()
        fallback.primary.judge.side_effect = OSError('私密错误正文')
        fallback.local = Mock()
        fallback.local.judge.return_value = {'intent': '闲聊'}
        fallback.fell_back = False
        fallback.reason = ''
        result = fallback.judge('当前', context)
        fallback.rank_candidates('当前', '闲聊', ['候选'], context)
        fallback.local.judge.assert_called_once_with('当前', context)
        fallback.local.rank_candidates.assert_called_once_with('当前', '闲聊', ['候选'], context)
        self.assertNotIn('私密错误正文', str(result))


if __name__ == '__main__':
    unittest.main()
