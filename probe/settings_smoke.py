"""Native settings smoke test on macOS, using only temporary files and a local HTTP server.

Run: uv run python -B probe/settings_smoke.py
Renders the real window to /tmp/jev-settings-smoke.png when screen capture is available.
Does not read messages, real credentials, or modify the user's configuration.
"""
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

import AppKit as A
import Quartz
from Foundation import NSDate

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tests'))
from test_settings import Server, SettingsNetwork
import settings_config as config
import userconfig
from settings import SettingsController


def wait_for_request(controller):
    deadline = time.monotonic() + 5
    while controller.busy and time.monotonic() < deadline:
        A.NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.02))
    assert not controller.busy, 'UI never received request completion'
    assert controller.save_button.isEnabled()


def request_button(controller, prefix, title):
    item = next(i for i in controller.tabs.tabViewItems() if i.identifier() == prefix)
    return next(v for v in item.view().subviews() if isinstance(v, A.NSButton) and v.title() == title)


def render_window(controller, path):
    controller.window.display()
    image = Quartz.CGWindowListCreateImage(
        Quartz.CGRectNull, Quartz.kCGWindowListOptionIncludingWindow,
        controller.window.windowNumber(), Quartz.kCGWindowImageBoundsIgnoreFraming)
    if image:
        data = A.NSBitmapImageRep.alloc().initWithCGImage_(image)
        data.representationUsingType_properties_(
            A.NSBitmapImageFileTypePNG, {}).writeToFile_atomically_(path, True)


app = A.NSApplication.sharedApplication()
app.setActivationPolicy_(A.NSApplicationActivationPolicyRegular)
SettingsNetwork.setUpClass()
try:
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True), patch.object(userconfig, '_startup_sources', None), patch.object(userconfig, 'env_files', return_value=[Path(directory) / 'env']), patch.object(userconfig, 'PROJECT_ENV', Path(directory) / '.env'):
        path = Path(directory) / 'env'
        path.write_text('# keep\nJEV_TONES="名字=说明"\n')
        userconfig.load()
        c = SettingsController.alloc().init().build()
        c.show()
        jev = c.fields['TYPESAFE']
        jev['API_KEY'].setStringValue_('test-jev-key')
        jev['BASE_URL'].setStringValue_(SettingsNetwork.base)
        Server.response = {'models': [{'name': 'jev-latest'}, {'name': 'jev-preview'}]}
        Server.code = 200
        request_button(c, 'TYPESAFE', '获取模型列表').performClick_(None)
        wait_for_request(c)
        assert jev['MODEL'].objectValues() == ['jev-latest', 'jev-preview']
        assert jev['API_KEY'].stringValue() == 'test-jev-key'
        fields = c.fields['OPENAI']
        fields['API_KEY'].setStringValue_('test-only-key')
        fields['BASE_URL'].setStringValue_(SettingsNetwork.base + '/v1')
        fields['MODEL'].setStringValue_('typed-model')
        Server.response = {'data': [{'id': 'served-model'}]}
        Server.code = 200
        request_button(c, 'OPENAI', '获取模型列表').performClick_(None)
        assert not c.save_button.isEnabled()
        wait_for_request(c)
        assert fields['MODEL'].objectValues() == ['served-model']
        assert fields['MODEL'].stringValue() == 'typed-model', 'must not silently switch model'
        Server.response = {'choices': [{'message': {'content': '连接成功'}}]}
        request_button(c, 'OPENAI', '测试连接').performClick_(None)
        wait_for_request(c)
        assert '连接成功' in c.status.stringValue(), c.status.stringValue()
        Server.code = 401
        request_button(c, 'OPENAI', '获取模型列表').performClick_(None)
        wait_for_request(c)
        assert '401' in c.status.stringValue() and '手填' in c.status.stringValue()
        assert fields['MODEL'].isEnabled()
        # OrcaRouter page: two explicit entrances, a catalog-only model combo, and the
        # capability filter dropping a non-chat route from the dropdown.
        orca = c.fields['ORCAROUTER']
        assert c.orca_login_button.title() == 'Connect with OrcaRouter'
        assert c.orca_login_button.isEnabled() and not c.orca_cancel_button.isEnabled()
        orca['API_KEY'].setStringValue_('sk-orca-smoke-only-key')
        orca['BASE_URL'].setStringValue_(SettingsNetwork.base)
        Server.response = {'data': [
            {'id': 'openai/gpt-5.5', 'supported_endpoint_types': ['openai'],
             'architecture': {'input_modalities': ['text']}},
            {'id': 'google/gemini-3.5-flash', 'supported_endpoint_types': ['openai'],
             'architecture': {'input_modalities': ['text', 'image']}},
            {'id': 'vendor/image-xl', 'supported_endpoint_types': ['image-generation']},
        ]}
        Server.code = 200
        Server.requests = []
        request_button(c, 'ORCAROUTER', '获取模型列表').performClick_(None)
        wait_for_request(c)
        assert Server.requests[0][0] == '/models?capability=chat', Server.requests[0][0]
        assert orca['MODEL'].objectValues() == ['google/gemini-3.5-flash', 'openai/gpt-5.5'], \
            orca['MODEL'].objectValues()
        # A model the catalog does not offer for this capability is cleared, not kept.
        orca['MODEL'].setStringValue_('openai/gpt-5.5')
        c.apply_orca_catalog(config.list_orcarouter_models(
            SettingsNetwork.base, 'sk-orca-smoke-only-key', 'chat', 'image'), 'chat', 'image')
        assert orca['MODEL'].objectValues() == ['google/gemini-3.5-flash']
        assert orca['MODEL'].stringValue() == '', orca['MODEL'].stringValue()
        # Free text is not a model source here: the dropdown is the only path.
        assert c.tabs.tabViewItems()[3].identifier() == 'ORCAROUTER'
        c.save_button.performClick_(None)
        assert '已保存' in c.status.stringValue(), c.status.stringValue()
        assert userconfig.parse_env_file(path)['ORCAROUTER_API_KEY'] == 'sk-orca-smoke-only-key'
        assert userconfig.parse_env_file(path)['OPENAI_MODEL'] == 'typed-model'
        assert '# keep\nJEV_TONES="名字=说明"\n' in path.read_text()
        assert path.stat().st_mode & 0o777 == 0o600
        assert not userconfig.get('ORCAROUTER_API_KEY'), 'must not hot reload'
        assert not c.changed()
        # Leaving the OrcaRouter tab releases an in-flight login without a restart.
        generation = c.orca_login.begin()
        c.orca_login_button.setEnabled_(False)
        c.orca_cancel_button.setEnabled_(True)
        c.tabs.selectTabViewItemAtIndex_(0)
        assert not c.orca_login.busy and not c.orca_login.current(generation)
        assert c.orca_login_button.isEnabled() and not c.orca_cancel_button.isEnabled()
        c.tabs.selectTabViewItemAtIndex_(3)
        for index, name in enumerate(('jev', 'openai', 'anthropic', 'orcarouter')):
            c.tabs.selectTabViewItemAtIndex_(index)
            A.NSRunLoop.currentRunLoop().runUntilDate_(
                NSDate.dateWithTimeIntervalSinceNow_(0.1))
            render_window(c, f'/tmp/jev-settings-{name}.png')
            if name == 'openai':
                render_window(c, '/tmp/jev-settings-smoke.png')
        c.window.close()
        reopened = SettingsController.alloc().init().build()
        assert reopened.fields['OPENAI']['MODEL'].stringValue() == 'typed-model'
        # Existing keychain expression remains byte-for-byte when editing only the model.
        path.write_text('export OPENAI_API_KEY="$(security find-generic-password -w)" # keep expression\nOPENAI_MODEL=old\n')
        shell = SettingsController.alloc().init().build()
        shell.fields['OPENAI']['MODEL'].setStringValue_('new-model')
        shell.save_button.performClick_(None)
        assert 'export OPENAI_API_KEY="$(security find-generic-password -w)" # keep expression\n' in path.read_text()
        assert shell.fields['OPENAI']['API_KEY'].stringValue() == ''
        print('PASS: native buttons, async completion, models/manual entry, HTTP failure, secure save, restart isolation, reopen, shell-expression preservation, OrcaRouter dual entrances + capability-filtered catalog')
finally:
    SettingsNetwork.tearDownClass()
