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
import userconfig
from settings import SettingsController
from test_hud_reply import HudReplyTests, block
import chat_context
from types import SimpleNamespace


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
    if not image:
        view = controller.window.contentView()
        bitmap = view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
        view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), bitmap)
        bitmap.representationUsingType_properties_(
            A.NSBitmapImageFileTypePNG, {}).writeToFile_atomically_(path, True)
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
        fixture = HudReplyTests()
        fixture.setUp()
        h = fixture.h
        h.conversations = chat_context.Conversations(Path(directory) / 'conversations.json')
        h.conversations.save_background('另外的聊天', '原有背景')
        h.judge = SimpleNamespace()
        fixture.incoming()
        c = SettingsController.alloc().init().build(h)
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
        c.save_button.performClick_(None)
        assert '已保存' in c.status.stringValue(), c.status.stringValue()
        assert userconfig.parse_env_file(path)['OPENAI_MODEL'] == 'typed-model'
        assert '# keep\nJEV_TONES="名字=说明"\n' in path.read_text()
        assert path.stat().st_mode & 0o777 == 0o600
        assert not userconfig.get('OPENAI_API_KEY'), 'must not hot reload'
        assert not c.changed()
        for index, name in enumerate(('jev', 'openai', 'anthropic')):
            c.tabs.selectTabViewItemAtIndex_(index)
            A.NSRunLoop.currentRunLoop().runUntilDate_(
                NSDate.dateWithTimeIntervalSinceNow_(0.1))
            render_window(c, f'/tmp/jev-settings-{name}.png')
            if name == 'openai':
                render_window(c, '/tmp/jev-settings-smoke.png')
        c.tabs.selectTabViewItemWithIdentifier_("CONTEXT")
        assert c.tabs.selectedTabViewItem().label() == '会话记录与背景'
        assert set(c.context_picker.itemTitles()) == {'chat', '另外的聊天'}
        assert c.context_title == 'chat'
        assert not h.history_enabled
        c.history_switch.setState_(A.NSOnState)
        c.background_editor.setString_('AAA是群主\nBBB是公司老板')
        c.context_count.setStringValue_('0')
        c.save_button.performClick_(None)
        assert not h.history_enabled
        assert not h.conversations.background('chat'), 'invalid count must not save backgrounds'

        def select_chat(title):
            c.context_picker.selectItemWithTitle_(title)
            app.sendAction_to_from_(c.context_picker.action(), c, c.context_picker)

        select_chat('另外的聊天')
        assert c.background_editor.string() == '原有背景'
        c.background_editor.setString_('另一会话的新背景')
        select_chat('chat')
        assert c.background_editor.string() == 'AAA是群主\nBBB是公司老板'
        assert h.conversations.background('另外的聊天') == '原有背景', 'switching must not save'
        c.context_count.setStringValue_('1')
        c.save_button.performClick_(None)
        assert h.history_enabled and h.context_limit == 1
        assert h.conversations.background('chat') == 'AAA是群主\nBBB是公司老板'
        assert h.conversations.background('另外的聊天') == '另一会话的新背景'
        assert not c.changed() and not c.changed_backgrounds()

        # A failed background write keeps its draft; already saved changes remain retry-safe.
        c.context_count.setStringValue_('2')
        c.background_editor.setString_('AAA是群主\nBBB是公司老板\n直接说明结论')
        select_chat('另外的聊天')
        c.background_editor.setString_('尚未保存的背景')
        save_background = h.save_background

        def fail_second(title, text):
            if title == '另外的聊天':
                raise OSError('synthetic disk failure')
            save_background(title, text)

        with patch.object(h, 'save_background', side_effect=fail_second):
            c.save_button.performClick_(None)
        assert '部分配置已保存' in c.status.stringValue()
        assert h.context_limit == 2
        assert c.changed_backgrounds() == {'另外的聊天': '尚未保存的背景'}
        c.save_button.performClick_(None)
        assert h.conversations.background('另外的聊天') == '尚未保存的背景'
        assert not c.changed_backgrounds()
        c.background_editor.setString_('')
        c.save_button.performClick_(None)
        assert h.conversations.background('另外的聊天') == ''

        fixture.incoming()
        assert len(h.conversations.history('chat')) == 1
        fixture.read([block('合成新聊天', .40, .70, .15)], title='另外的聊天')
        select_chat('chat')
        c.history_menu.menu().performActionForItemAtIndex_(1)
        assert h.conversations.history('chat') == []
        assert len(h.conversations.history('另外的聊天')) == 1
        c.history_menu.menu().performActionForItemAtIndex_(2)
        assert h.conversations.history('另外的聊天') == []
        assert h.conversations.background('chat')
        c.context_count.setStringValue_('20')
        c.save_button.performClick_(None)
        render_window(c, '/tmp/jev-settings-context.png')
        c.window.close()

        # Opening/focusing settings reloads disk, retaining only unsaved background drafts.
        sync = SettingsController.alloc().init().build(h)
        sync.tabs.selectTabViewItemWithIdentifier_('CONTEXT')
        h.conversations.path.write_text('')
        sync.show()
        assert list(sync.context_picker.itemTitles()) == ['另外的聊天'], 'deleted chats remain in picker'
        assert sync.background_editor.string() == ''
        assert not sync.changed_backgrounds(), 'an untouched old background must not become a draft'
        chat_context.Conversations(h.conversations.path).save_background('另外的聊天', '外部更新的背景')
        sync.show()
        assert sync.background_editor.string() == '外部更新的背景'
        assert not sync.changed_backgrounds()
        h.save_background('草稿会话', '已保存背景')
        sync.show()
        sync.context_picker.selectItemWithTitle_('草稿会话')
        app.sendAction_to_from_(sync.context_picker.action(), sync, sync.context_picker)
        sync.background_editor.setString_('用户尚未保存的草稿')
        h.conversations.path.write_text('{}')
        A.NSNotificationCenter.defaultCenter().postNotificationName_object_(
            A.NSWindowDidBecomeKeyNotification, sync.window)
        assert set(sync.context_picker.itemTitles()) == {'另外的聊天', '草稿会话'}
        assert sync.background_editor.string() == '用户尚未保存的草稿'
        assert h.conversations.path.read_text() == '{}'
        sync.save_button.performClick_(None)
        assert set(h.conversations.data) == {'草稿会话'}
        assert h.conversations.background('草稿会话') == '用户尚未保存的草稿'
        h.conversations.path.write_text('{合成损坏标记')
        sync.show()
        assert '暂停写入' in sync.status.stringValue()
        assert '合成损坏标记' not in sync.status.stringValue()
        sync.save_button.performClick_(None)
        assert '暂停写入' in sync.status.stringValue()
        render_window(sync, '/tmp/jev-settings-context-error.png')
        sync.background_editor.setString_('损坏期间保留的草稿')
        sync.save_button.performClick_(None)
        assert h.conversations.path.read_text() == '{合成损坏标记'
        h.conversations.path.write_text('{}')
        sync.show()
        assert sync.background_editor.string() == '损坏期间保留的草稿'
        sync.save_button.performClick_(None)
        assert h.conversations.background(sync.context_title) == '损坏期间保留的草稿'
        sync.window.close()
        with patch.object(userconfig, '_startup_sources', [('环境变量', {'JEV_CONTEXT_MESSAGES': '101'})]), patch.object(userconfig, '_session_overrides', {}):
            h.context_limit = 20  # startup's validated fallback
            invalid_env = SettingsController.alloc().init().build(h)
            assert not invalid_env.context_count.isEnabled()
            invalid_env.history_switch.setState_(A.NSOffState)
            invalid_env.save_button.performClick_(None)
            assert h.context_limit == 20
            assert '已保存' in invalid_env.status.stringValue()
            invalid_env.history_switch.setState_(A.NSOnState)
            invalid_env.save_button.performClick_(None)
            invalid_env.window.close()
        with patch.dict(os.environ, {}, clear=True), patch.object(userconfig, '_startup_sources', None), patch.object(userconfig, '_session_overrides', {}):
            userconfig.load()
            restart_context = SettingsController.alloc().init().build()
            assert restart_context.history_switch.state() == A.NSOnState
            assert restart_context.history_switch.isEnabled()
            assert restart_context.context_count.isEnabled()
            assert not restart_context.context_picker.isEnabled()
            assert not restart_context.background_editor.isEditable()
            restart_context.history_switch.setState_(A.NSOffState)
            restart_context.save_button.performClick_(None)
            assert userconfig.parse_env_file(path)['JEV_HISTORY'] == '0'
            restart_context.window.close()
        reopened = SettingsController.alloc().init().build()
        assert reopened.fields['OPENAI']['MODEL'].stringValue() == 'typed-model'
        # Existing keychain expression remains byte-for-byte when editing only the model.
        path.write_text('export OPENAI_API_KEY="$(security find-generic-password -w)" # keep expression\nOPENAI_MODEL=old\n')
        shell = SettingsController.alloc().init().build()
        shell.fields['OPENAI']['MODEL'].setStringValue_('new-model')
        shell.save_button.performClick_(None)
        assert 'export OPENAI_API_KEY="$(security find-generic-password -w)" # keep expression\n' in path.read_text()
        assert shell.fields['OPENAI']['API_KEY'].stringValue() == ''
        print('PASS: native controls, model requests, unified save, chat drafts, validation, partial-save retry, clear history, external file refresh, draft preservation, corrupt-file recovery, environment priority, restart isolation, shell-expression preservation')
finally:
    SettingsNetwork.tearDownClass()
