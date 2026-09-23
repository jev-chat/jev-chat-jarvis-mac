"""HUD reply-pipeline regressions: reply epochs/keys, incoming gating, prejudge and
pregen slots, and foreground/read-failure handling. Run: python -B -m unittest
discover -s tests -v.

Loads the actual HudController methods through AST (see tests/support_hud.py) so the
tests never start Cocoa, read the screen, load user credentials, or make model calls.
"""
import sys
import threading
import time
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/: for support_hud
from perception import Message, extract_chat_title, extract_messages, find_wechat_window
import chat_context
from support_hud import Harness, HUD, block


def qq_app():
    """A minimal QQ-shaped adapter: AX path, no screen capture anywhere."""
    return SimpleNamespace(key='qq', display_name='QQ', needs_screen_capture=False,
                           read_conversation=Mock(),
                           locate_input=Mock(return_value={'box': None, 'rect': None,
                                                           'reason': 'test'}),
                           fill_text=Mock(return_value=(True, '已填入')),
                           warm=Mock(return_value=None))


class HudReplyTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('input_region.locate_visual_input', return_value=None))
        self.h = h = Harness()
        HUD['read_conversation'].reset_mock()
        HUD['read_conversation'].side_effect = None
        HUD['frontmost_app'].reset_mock()
        HUD['frontmost_app'].side_effect = None
        HUD['frontmost_app'].return_value = HUD['FAKE_APP']
        HUD['screen_capture_ok'].reset_mock()
        HUD['screen_capture_ok'].return_value = True
        HUD['fill'].has_accessibility.reset_mock()
        HUD['fill'].has_accessibility.return_value = True
        HUD['fill'].request_accessibility.reset_mock()
        HUD['find_wechat_window'].reset_mock()
        HUD['find_wechat_window'].return_value = None
        HUD['FAKE_APP'].fill_text.reset_mock()
        HUD['FAKE_APP'].fill_text.return_value = (True, '已填入')
        for name, value in dict(
            _active_context=None, conversations=None, history_enabled=False, context_limit=20,
            _observed_messages=None, _observed_offset=0, _context_version=0,
            _context_lock=threading.RLock(), _reply_key=None, _reply_epoch=0, _reply_worker=threading.local(),
            last_seen=None, analyzed_text=None, _win_wid=None, _fingerprint=None,
            _burst_left=3, _last_full=None, _show_boxes=False, _read_once=True,
            _gen_epoch=0, _last_skip_reason=None, _prejudge_req=None, _prejudge_result=None,
            _pregen_req=None, _pregen_result=None, _pregen_running=False,
            _prejudging=False, _paused=False, _analyzing=False, _regenerating=False,
            _stable_n=0, last_change_ts=0, last_analyze_ts=0,
            _app=None, _asked_accessibility=False, _foreground_epoch=0,
            _wechat_frontmost=None,
            _read_fail_since=None, _read_fail_hidden=False, _empty_frame_since=None,
            _prejudge_event=threading.Event(), _pregen_event=threading.Event(),
            slot_tones=['normal'], candidate_count=2,
            generation_context_turns=8, judge_context_turns=4,
            _stream_rows={}, _last_context=None,
            _judgment_enabled=True, background_capture=False,
        ).items():
            setattr(h, name, value)
        h._show = Mock()
        h._render = Mock()
        h._clear_candidates = Mock()
        h.panel = Mock()
        h._set_candidate_header = Mock()
        h.rows = {'cand_header': Mock()}
        h._slot_active = lambda _: True
        h._payload_current = lambda _: True
        h.generator = Mock()
        h.judge = Mock()
        h._model_lock = threading.Lock()
        h._judged_once = True
        for name in ['applyIncoming_', 'applyPending_', 'applyJudgment_',
                     'applyCandidates_', 'applyRegenerated_', 'applyStreamLine_', 'applyError_',
                     'applyUnreadable_',
                     'applyPosition_', 'applyChat_', 'applyBoxes_', 'applyHidden_',
                     'applyForegroundHidden_', 'applyForegroundShown_']:
            setattr(h, name, Mock())
        self.queue = []
        h.performSelectorOnMainThread_withObject_waitUntilDone_ = lambda s, p, w: self.queue.append((s, p))

    def read(self, blocks, title='chat', input_top=None):
        messages = extract_messages(blocks, input_top=input_top)
        HUD['read_conversation'].return_value = {
            'ok': True, 'unchanged': False, 'fingerprint': None,
            'window': {'wid': 1}, 'chat_title': title, 'messages': messages,
        }
        self.h._work_inner()
        return messages

    def flush(self):
        while self.queue:
            selector, payload = self.queue.pop(0)
            getattr(self.h, selector.replace(':', '_'))(payload)

    def incoming(self):
        self.read([block('下午开会', .40, .70, .15)])

    def test_always_on_top_toggle_changes_window_level_and_menu_state(self):
        self.h.panel = Mock()
        self.h.always_on_top_item = Mock()
        self.h._always_on_top = True

        self.h.toggleAlwaysOnTop_(None)

        self.assertFalse(self.h._always_on_top)
        self.h.panel.setLevel_.assert_called_once_with(HUD['AppKit'].NSNormalWindowLevel)
        self.h.always_on_top_item.setState_.assert_called_once_with(HUD['AppKit'].NSOffState)

        self.h.toggleAlwaysOnTop_(None)

        self.assertTrue(self.h._always_on_top)
        self.assertEqual(self.h.panel.setLevel_.call_args_list[-1].args,
                         (HUD['AppKit'].NSFloatingWindowLevel,))
        self.assertEqual(self.h.always_on_top_item.setState_.call_args_list[-1].args,
                         (HUD['AppKit'].NSOnState,))

    def test_saved_settings_apply_all_runtime_changes_immediately(self):
        popups = [Mock() for _ in range(3)]
        judgment_view = Mock()
        self.h._dds = popups
        self.h._fixed = [(self.h.rows['cand_header'], 1, 2, 3, 4)]
        self.h._judgment_views = [judgment_view]
        self.h._collapsed = False
        self.h._judgment_enabled = True
        self.h._clear_candidates = Mock()
        self.h._relayout = Mock()
        self.h._regenerate = Mock()
        self.h._set_foreground_state = Mock()
        self.h.analyzed_text = '待重新生成'
        self.h.background_capture = True
        self.h._wechat_frontmost = False
        self.h.panel.isVisible.return_value = True
        new_generator = Mock()
        new_judge = SimpleNamespace(enabled=False)
        config = Mock()
        config.CONTEXT_KEYS = ('JUDGE_CONTEXT_TURNS', 'GENERATION_CONTEXT_TURNS')
        config.context_turns.side_effect = lambda key: {
            'GENERATION_CONTEXT_TURNS': 6, 'JUDGE_CONTEXT_TURNS': 3}[key]
        config.bool_setting.side_effect = lambda key: {
            'JEV_AUTO_HIDE': False, 'JEV_AUTO_DOCK': False,
            'JEV_BACKGROUND_CAPTURE': False, 'JEV_PANEL_ALWAYS_ON_TOP': False,
        }[key]
        config.candidate_count.return_value = 3
        config.default_tones.return_value = ['自然沟通', '不用', '不用']
        changes = {
            'OPENAI_MODEL': 'new-model', 'JUDGE_BACKEND': 'cloud',
            'JEV_DEFAULT_TONE_1': '自然沟通', 'JEV_CANDIDATES_PER_TONE': '3',
            'JEV_AUTO_DOCK': '0', 'JEV_PANEL_ALWAYS_ON_TOP': '0',
            'JEV_BACKGROUND_CAPTURE': '0',
        }
        with patch.dict(HUD, {'settings_config': config,
                              'Generator': Mock(return_value=new_generator),
                              'make_judge': Mock(return_value=new_judge)}), \
                patch.object(threading, 'Thread') as thread:
            self.h.applySettings_(None)
            self.h.applySettings_(changes)

        self.assertIs(self.h.generator, new_generator)
        self.assertIs(self.h.judge, new_judge)
        self.assertEqual(self.h.candidate_count, 3)
        self.assertEqual(self.h.slot_tones, ['自然沟通', '不用', '不用'])
        self.h._set_foreground_state.assert_called_once_with(None)
        self.h._clear_candidates.assert_called_once()
        self.h._relayout.assert_called_once()
        self.h._regenerate.assert_called_once()
        self.h._show.assert_called_once()
        thread.assert_called_once()
        judgment_view.setHidden_.assert_called_once_with(True)
        for popup in popups:
            popup.setToolTip_.assert_called_once()

        config.bool_setting.side_effect = lambda key: {
            'JEV_AUTO_HIDE': True, 'JEV_AUTO_DOCK': True,
            'JEV_BACKGROUND_CAPTURE': True, 'JEV_PANEL_ALWAYS_ON_TOP': True,
        }[key]
        self.h.background_capture = False
        self.h.analyzed_text = None
        with patch.dict(HUD, {'settings_config': config}):
            self.h.applySettings_({'JEV_BACKGROUND_CAPTURE': '1'})
        self.assertEqual(self.h._next_read_ts, 0)
        self.h.panel.orderOut_.assert_called_once_with(None)

    def test_panel_follows_window_even_when_read_not_due(self):
        """#93: positioning is tick-driven from cheap window metadata, not gated on
        reads — in manual-calibration mode one read is a multi-second OCR, and the
        panel used to wait out two full read cycles before snapping to a drag."""
        win = SimpleNamespace(wid=7, x=100, y=200, w=550, h=719)
        self.h._app = HUD['FAKE_APP']          # no foreground transition inside tick_
        self.h._paused = False
        self.h._busy = True                    # a slow read is in flight …
        self.h._next_read_ts = time.time() + 60  # … and the next one is not due
        self.h._refresh_model_status = Mock()
        self.h._position_near = Mock()
        HUD['find_wechat_window'].return_value = win
        self.h.tick_(None)
        self.h._position_near.assert_called_once_with(
            {'wid': 7, 'x': 100, 'y': 200, 'w': 550, 'h': 719})

    def test_panel_positioning_skipped_when_paused(self):
        self.h._app = HUD['FAKE_APP']
        self.h._paused = True
        self.h._refresh_model_status = Mock()
        self.h._position_near = Mock()
        self.h.tick_(None)
        self.h._position_near.assert_not_called()

    def test_transient_missing_input_target_does_not_abort_read(self):
        locate = HUD['fill'].locate_input  # shared with FAKE_APP and later tests: restore it
        original = locate.return_value
        locate.return_value = None
        self.addCleanup(setattr, locate, 'return_value', original)
        self.incoming()
        self.assertIsInstance(self.h._input_target, dict)
        self.assertIsNone(self.h._input_target['box'])
        self.assertEqual(self.h._reply_key[:3], ('wechat', 'chat', '下午开会'))

    def test_foreground_change_during_input_signature_discards_read(self):
        def switch_foreground(*args):
            self.h._set_foreground_state(None)
            return 'stale-signature'

        with patch.object(HUD['fill'], 'locate_input', return_value={'box': None}), \
             patch('input_region.locate_visual_input', return_value=(1, 2, 3, 4)), \
             patch('visual_fill.chat_signature', side_effect=switch_foreground):
            self.incoming()
        self.assertIsNone(self.h._input_target)
        self.assertIsNone(self.h._reply_key)
        self.assertIsNone(self.h._pregen_req)

    def test_switch_short_titles_with_same_message_invalidates_old_reply(self):
        titles = [extract_chat_title([block(name, .40, .94, .10, .025)])
                  for name in ('张三', '李经理')]
        self.read([block('下午开会', .40, .70, .15)], title=titles[0])
        epoch = self.h._reply_epoch
        self.h._push_reply('applyCandidates:', 'old replies', epoch)
        self.read([block('下午开会', .40, .70, .15)], title=titles[1])
        self.assertGreater(self.h._reply_epoch, epoch)
        self.assertEqual(self.h._reply_key[:3], ('wechat', '李经理', '下午开会'))
        self.assertEqual(self.h._prejudge_req[0], '下午开会')
        self.assertEqual(self.h._pregen_req[0], '下午开会')
        self.flush()
        self.h.applyCandidates_.assert_not_called()

    def test_all_model_paths_share_twenty_messages_including_target(self):
        blocks = [block(f'消息{i:02}', .40, .95 - i * .055, .15)
                  for i in range(1, 13)]
        messages = self.read(blocks)
        early_judge = self.h._prejudge_req[1]
        early_gen = self.h._pregen_req[1]
        self.assertNotEqual(early_judge, early_gen)
        self.assertIn('消息06', early_judge)
        self.assertNotIn('消息05', early_judge)
        self.assertIn('消息02', early_gen)
        self.assertNotIn('消息01', early_gen)
        self.assertNotIn(messages[-1].text, early_gen)
        self.h.generator.generate.return_value = {
            'groups': [{'slot': 0, 'tone': 'normal', 'texts': ['合成候选']}]}
        self.h.judge.rank_candidates.return_value = [{'text': '合成候选', 'prob': 1}]
        self.h._reply_task(self.h._reply_epoch, self.h._regen_work,
                           messages[-1].text, '闲聊', ['normal'])
        self.assertEqual(self.h.generator.generate.call_args.args[3], early_gen)
        self.assertEqual(self.h.judge.rank_candidates.call_args.kwargs['context'], early_gen)

    def test_manual_regeneration_shares_bounded_context_and_retires_old_work(self):
        text = '合成私密当前' + '甲' * 9000
        self.read([block(text, .40, .70, .15)])
        context = self.h._active_context
        tone = HUD['styles'].DEFAULT_SLOTS[0]
        self.h.slot_tones = [tone]
        self.h.analyzed_text = text
        self.h._last_intent = '闲聊'
        self.h.generator.generate.return_value = {
            'groups': [{'slot': 0, 'tone': tone, 'texts': ['合成候选']}]}
        self.h.judge.rank_candidates.return_value = [{'text': '合成候选', 'prob': 1}]
        logs = []
        with patch.object(threading, 'Thread') as thread, patch.dict(HUD, {'_log': logs.append}):
            self.h.regenerateReply_(None)
            task = thread.call_args.kwargs
            task['target'](*task['args'])
            sent = self.h.generator.generate.call_args.args
            self.assertEqual(sent[3], context)
            self.assertLessEqual(len(sent[0]) + len(context or ''), chat_context.CONTEXT_CHARS)
            self.assertEqual(self.h.judge.rank_candidates.call_args.kwargs['context'], context)
            self.flush()
            self.h.applyRegenerated_.assert_called_once()
            self.assertFalse(self.h._regenerating)
            self.h.regenerateReply_(None)
            task = thread.call_args.kwargs
            self.h.configure_context(False, '1')
            task['target'](*task['args'])
            self.flush()
            self.h.generator.generate.assert_called_once()
            self.h.applyRegenerated_.assert_called_once()
            self.assertFalse(self.h._regenerating)
        self.assertNotIn('合成私密当前', str(logs))

    def test_background_is_bound_durable_and_invalidates_late_results(self):
        with tempfile.TemporaryDirectory() as directory:
            self.h.conversations = chat_context.Conversations(Path(directory) / 'chats.json')
            self.incoming()
            old = self.h._reply_epoch
            self.h.save_background('chat', 'AAA是群主\nBBB是老板')
            self.h._push_reply('applyCandidates:', '过期候选', old)
            self.flush()
            self.h.applyCandidates_.assert_not_called()
            self.incoming()
            self.assertIn('BBB是老板', self.h._pregen_req[1])
            self.assertEqual(chat_context.Conversations(Path(directory) / 'chats.json').background('chat'),
                             'AAA是群主\nBBB是老板')
            self.read([block('下午开会', .40, .70, .15)], title='other')
            self.h.save_background('chat', '新的背景')
            self.assertNotIn('新的背景', self.h._pregen_req[1] or '')
            self.h.save_background('chat', '')
            self.assertEqual(self.h.conversations.background('chat'), '')

    def test_external_clear_does_not_restore_stored_chats_on_next_save(self):
        for contents in ['', '{}', None]:
            with self.subTest(contents=contents), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'chats.json'
                store = chat_context.Conversations(path)
                store.save_background('旧会话', '合成旧背景')
                store.observe('旧会话', [('旧记录', 'them', '')])
                store.observe('旧会话', [('无法衔接的旧帧', 'them', '')])
                if contents is None:
                    path.unlink()
                else:
                    path.write_text(contents)
                store.save_background('新会话', '合成新背景')
                self.assertEqual(set(chat_context.Conversations(path).data), {'新会话'})
                self.assertEqual(store._anchors, {})

    def test_external_clear_retires_old_context_and_late_model_results(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'chats.json'
            self.h.conversations = chat_context.Conversations(path)
            self.h.configure_context(True, '20')
            self.h.save_background('chat', '合成旧背景')
            self.read([block('合成旧历史', .40, .80, .15), block('当前消息', .40, .60, .15)])
            messages = self.read([block('当前消息', .40, .60, .15)])
            self.assertIn('合成旧历史', self.h._active_context)
            old_epoch = self.h._reply_epoch
            path.write_text('')
            self.h._reply_task(old_epoch, self.h._regen_work, '当前消息', '闲聊', ['normal'])
            self.h.generator.generate.assert_not_called()
            self.h._analyzing = True
            self.h._reply_task(old_epoch, self.h._run_generation, messages[-1], messages, {'intent': '闲聊'})
            self.assertFalse(self.h._analyzing, 'retiring old work must release the analysis gate')
            self.h._push_reply('applyCandidates:', '旧上下文候选', old_epoch)
            self.flush()
            self.h.applyCandidates_.assert_not_called()
            HUD['read_conversation'].return_value['unchanged'] = True
            self.h._work_inner()
            for request in [self.h._pregen_req, self.h._prejudge_req]:
                self.assertNotIn('合成旧历史', request[1])
                self.assertNotIn('合成旧背景', request[1])
            self.assertEqual(path.read_text(), '', 'cached frames must not refill an externally cleared file')
            self.read([])
            self.assertEqual(path.read_text(), '', 'reused empty frames must not refill cleared history')
            self.assertEqual(self.h._prejudge_req[1], self.h._pregen_req[1])
            self.read([block('当前消息', .40, .80, .15), block('新读到的消息', .40, .60, .15)])
            self.assertEqual([m[0] for m in self.h.conversations.history('chat')],
                             ['当前消息', '新读到的消息'])

    def test_invalid_external_data_blocks_writes_until_repaired(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'chats.json'
            store = chat_context.Conversations(path)
            for invalid in ['{私密损坏标记', '{"chat":{"messages":"invalid"}}']:
                store.save_background('旧会话', '合成旧背景')
                path.write_text(invalid)
                with self.assertRaises(ValueError):
                    store.save_background('新会话', '新背景')
                with self.assertRaises(ValueError):
                    store.clear_history()
                self.assertEqual(path.read_text(), invalid)
                self.assertEqual(store.history('旧会话'), [])
                self.assertEqual(store.background('旧会话'), '')
                self.assertNotIn('私密损坏标记', store.error)
                path.write_text('{}')
            store.save_background('修复后的会话', '新背景')
            self.assertEqual(set(chat_context.Conversations(path).data), {'修复后的会话'})
            path.touch()
            with patch.object(Path, 'read_text', side_effect=PermissionError('合成读取失败')):
                self.assertEqual(store.background('修复后的会话'), '')
                with self.assertRaises(ValueError):
                    store.save_background('无法写入', '背景')
            self.assertEqual(store.background('修复后的会话'), '新背景')

    def test_external_edit_during_save_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'chats.json'
            store = chat_context.Conversations(path)
            store.save_background('旧会话', '旧背景')
            with patch('os.fsync', side_effect=lambda _fd: path.write_text('{}')):
                with self.assertRaises(OSError):
                    store.save_background('新会话', '新背景')
            self.assertEqual(path.read_text(), '{}')
            self.assertEqual(store.data, {})
            self.assertEqual(list(path.parent.glob('.conversations-*')), [])

    def test_history_records_observed_frames_and_restores_last_hundred(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'chats.json'
            self.h.conversations = chat_context.Conversations(path)
            self.incoming()
            self.assertEqual(self.h.conversations.history('chat'), [])
            self.h.history_enabled = True
            for number in range(1, 106):
                start = max(1, number - 3)
                self.read([block(f'记录{i:03}', .40, .80 - (i-start)*.10, .15)
                           for i in range(start, number+1)])
            restored = chat_context.Conversations(path)
            self.assertEqual(len(restored.history('chat')), 100)
            self.assertEqual(restored.history('chat')[0][0], '记录006')
            self.assertEqual(restored.history('chat')[-1][0], '记录105')
            self.assertIn('记录095', self.h._pregen_req[1])
            self.assertNotIn('记录094', self.h._pregen_req[1])
            self.assertNotIn('记录105', self.h._pregen_req[1])
            self.read([block('记录105', .40, .80, .15)])
            self.assertEqual(len(self.h.conversations.history('chat')), 100)
            self.h.history_enabled = False
            self.read([block('记录105', .40, .80, .15), block('记录106', .40, .60, .15)])
            self.assertNotIn('记录086', self.h._pregen_req[1])
            self.assertEqual(restored.history('chat'), self.h.conversations.history('chat'))

    def test_context_limit_and_clear_preserve_background_and_retained_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'chats.json'
            self.h.conversations = chat_context.Conversations(path)
            self.h.configure_context(True, '20')
            for i in range(1, 106):
                self.read([block(f'项{n:03}', .40, .80 - k*.10, .15)
                           for k, n in enumerate(range(max(1, i-2), i+1))])
            self.h.save_background('chat', '独立背景')
            for limit, first in [('1', None), ('20', '项097'), ('100', '项097')]:
                self.h.configure_context(True, limit)
                self.read([block('项105', .40, .70, .15)])
                context = self.h._pregen_req[1]
                self.assertIn('独立背景', context)
                self.assertNotIn('项105', context)
                if first:
                    self.assertIn(first, context)
                else:
                    self.assertNotIn('项104', context)
            for bad in ['0', '101', '-1', '1.5', 'no']:
                with self.assertRaises(ValueError):
                    self.h.configure_context(False, bad)
                self.assertTrue(self.h.history_enabled)
            self.assertEqual(len(chat_context.Conversations(path).history('chat')), 100)
            self.read([block('另一会话', .40, .70, .15)], title='B')
            self.h.save_background('B', '另一背景')
            old = self.h._reply_epoch
            self.h.clear_history('chat')
            self.assertEqual(chat_context.Conversations(path).history('chat'), [])
            self.assertEqual(len(self.h.conversations.history('B')), 1)
            self.h.clear_history(None)
            self.h._push_reply('applyTones:', '迟到换话术', old)
            self.flush()
            self.assertEqual(chat_context.Conversations(path).history('B'), [])
            self.assertEqual(self.h.conversations.background('chat'), '独立背景')
            self.assertEqual(self.h.conversations.background('B'), '另一背景')

    def test_observation_handles_own_repeats_uncertain_titles_and_write_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'chats.json'
            self.h.conversations = chat_context.Conversations(path)
            self.h.configure_context(True, '20')
            self.read([block('相同发言', .80, .70, .10)])
            self.assertIsNone(self.h._pregen_req)
            self.read([block('相同发言', .80, .70, .10), block('相同发言', .80, .50, .10)])
            self.assertEqual(len(self.h.conversations.history('chat')), 2)
            self.read([block('相同发言', .80, .70, .10), block('相同发言', .80, .50, .10)])
            self.assertEqual(len(self.h.conversations.history('chat')), 2)
            self.read([block('无法衔接', .40, .70, .15)])
            self.assertEqual(len(self.h.conversations.history('chat')), 2)
            self.assertEqual(self.h._pregen_req[0], '无法衔接')
            for title in ['', '   ', 'new name']:
                self.read([block('当前屏幕', .40, .70, .15)], title=title)
            self.assertEqual(self.h.conversations.history(''), [])
            self.assertEqual(self.h.conversations.history('   '), [])
            self.assertEqual(len(self.h.conversations.history('new name')), 1)
            self.h.save_background('chat', '旧背景')
            with patch('os.replace', side_effect=OSError('合成私密错误')):
                with self.assertRaises(OSError):
                    self.h.save_background('chat', '新背景')
                with self.assertRaises(OSError):
                    self.h.clear_history(None)
            restored = chat_context.Conversations(path)
            self.assertEqual(restored.background('chat'), '旧背景')
            self.assertEqual(len(restored.history('chat')), 2)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_long_request_drops_whole_old_messages_and_keeps_originals(self):
        with tempfile.TemporaryDirectory() as directory:
            self.h.conversations = chat_context.Conversations(Path(directory) / 'chats.json')
            self.h.configure_context(True, '20')
            messages = self.read([block('最旧' + '甲'*5000, .40, .80, .15),
                                  block('较新' + '乙'*4000, .40, .60, .15),
                                  block('当前', .40, .40, .15)])
            context = self.h._pregen_req[1]
            self.assertNotIn('最旧', context)
            self.assertIn('较新', context)
            self.h.save_background('chat', '背景' + '丙'*9000)
            self.read([block('当前', .40, .70, .15), block('目标' + '丁'*9000, .40, .50, .15)])
            context = self.h._pregen_req[1]
            self.assertLessEqual(len(context) + len(chat_context.model_message(self.h._pregen_req[0], context)),
                                 chat_context.CONTEXT_CHARS)
            self.assertEqual(len(self.h.conversations.background('chat')), 9002)
            self.assertEqual(len(self.h.conversations.history('chat')[-1][0]), 9002)
            self.assertNotIn('最旧', context)

    def test_clear_during_capture_discards_old_recording_and_callbacks(self):
        with tempfile.TemporaryDirectory() as directory:
            self.h.conversations = chat_context.Conversations(Path(directory) / 'chats.json')
            self.h.configure_context(True, '20')
            self.incoming()
            old = self.h._reply_epoch
            snapshot = HUD['read_conversation'].return_value
            def capture_then_clear(**kwargs):
                self.h.clear_history(None)
                return snapshot
            HUD['read_conversation'].side_effect = capture_then_clear
            self.h._work_inner()
            self.assertEqual(self.h.conversations.history('chat'), [])
            for selector in ['applyJudgment:', 'applyCandidates:', 'applyStreamLine:', 'applyTones:']:
                self.h._push_reply(selector, 'late', old)
            self.flush()
            self.h.applyCandidates_.assert_not_called()
            self.h.applyJudgment_.assert_not_called()
            self.h.applyStreamLine_.assert_not_called()

    def test_request_preserves_current_message_that_fits_budget(self):
        text = '合成当前' + '甲' * 6000
        self.read([block(text, .40, .70, .15)])
        context = self.h._pregen_req[1]
        self.h.generator.generate.return_value = {'groups': []}
        self.h._reply_task(self.h._reply_epoch, self.h._regen_work, text, '闲聊', ['normal'])
        self.assertEqual(self.h.generator.generate.call_args.args[0], text)

    def test_identical_new_message_invalidates_old_target_even_at_limit_one(self):
        self.h.configure_context(False, '1')
        self.read([block('相同消息', .40, .70, .15)])
        old = self.h._reply_epoch
        self.read([block('相同消息', .40, .70, .15), block('相同消息', .40, .50, .15)])
        self.assertGreater(self.h._reply_epoch, old)
        self.h._push_reply('applyCandidates:', '旧消息候选', old)
        self.flush()
        self.h.applyCandidates_.assert_not_called()

    def test_recording_resumes_after_gap_without_joining_unrelated_segments(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'chats.json'
            self.h.conversations = chat_context.Conversations(path)
            self.h.configure_context(True, '100')
            for numbers in [(1, 2), (10, 11), (11, 12), (12, 13)]:
                self.read([block(f'片段{i:02}', .40, .80 - k*.15, .15)
                           for k, i in enumerate(numbers)])
            restored = chat_context.Conversations(path)
            self.assertEqual([m[0] for m in restored.history('chat')],
                             ['片段01', '片段02', '片段10', '片段11', '片段12', '片段13'])
            self.assertNotIn('片段02', self.h._pregen_req[1])
            self.assertIn('片段10', self.h._pregen_req[1])
            self.h.conversations = restored
            self.read([block('片段01', .40, .80, .15), block('片段02', .40, .60, .15)])
            self.read([block('片段12', .40, .80, .15), block('片段13', .40, .60, .15)])
            self.assertEqual(len(restored.history('chat')), 6)
            self.read([block('清除前锚点', .40, .80, .15)])
            self.h.clear_history(None)
            self.read([block('清除后的新帧', .40, .80, .15)])
            self.assertEqual([m[0] for m in restored.history('chat')], ['清除后的新帧'])

    def test_invalidated_generation_does_not_start_an_old_context_ranking_request(self):
        self.incoming()
        def generate_then_change(*args, **kwargs):
            self.h.configure_context(False, '1')
            return {'groups': [{'slot': 0, 'tone': 'normal', 'texts': ['合成候选']}]}
        self.h.generator.generate.side_effect = generate_then_change
        self.h._reply_task(self.h._reply_epoch, self.h._regen_work, '下午开会', '闲聊', ['normal'])
        self.h.judge.rank_candidates.assert_not_called()

    def test_member_count_change_keeps_conversation_history_and_background(self):
        with tempfile.TemporaryDirectory() as directory:
            self.h.conversations = chat_context.Conversations(Path(directory) / 'chats.json')
            self.h.configure_context(True, '20')
            self.h.save_background('项目讨论组', '合成群背景')
            for header in ['项目讨论组（20）', '项目讨论组(21)']:
                title = extract_chat_title([block(header, .40, .94, .20)])
                self.read([block('合成群消息', .40, .70, .15)], title=title)
                self.assertIn('合成群背景', self.h._pregen_req[1])
            self.assertEqual(len(self.h.conversations.history('项目讨论组')), 1)

    def test_default_storage_lives_in_home_support_for_source_and_app_alike(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / 'home'
            expected = home / 'Library/Application Support/jev-jarvis/conversations.json'
            with patch.object(Path, 'home', return_value=home):
                self.h.conversations = chat_context.Conversations()
                self.assertEqual(self.h.conversations.path, expected)
                self.h.configure_context(True, '20')
                self.read([block('合成讨论消息', .40, .70, .15)])
                self.h.save_background('chat', '合成人物背景')
                restored = chat_context.Conversations()
            self.assertTrue(expected.is_file())
            self.assertEqual(expected.stat().st_mode & 0o777, 0o600)
            self.assertEqual(restored.background('chat'), '合成人物背景')
            self.assertEqual(restored.history('chat')[0][0], '合成讨论消息')

    def test_only_own_short_message_never_enqueues_models(self):
        messages = self.read([block('11', .862, .284, .024, .024)])
        self.assertEqual(messages[0].side, 'me')
        self.assertIsNone(self.h._prejudge_req)
        self.assertIsNone(self.h._pregen_req)
        self.assertFalse(self.h._prejudge_event.is_set())
        self.assertFalse(self.h._pregen_event.is_set())
        self.flush()
        self.h.applyIncoming_.assert_not_called()
        self.h._clear_candidates.assert_called_once()

    def test_ambiguous_wide_message_is_not_incoming(self):
        messages = self.read([block('宽文本横跨左右分界', .38, .70, .55)])
        self.assertEqual(messages[0].side, 'unknown')
        self.assertIsNone(self.h._reply_key)
        self.assertIsNone(self.h._pregen_req)

    def test_punctuation_prefixed_latest_message_uses_known_side_anchors(self):
        messages = self.read([
            block('旧收到', .48, .75, .12),
            block('我的回复', .78, .55, .10),
            block('。。2000一个月', .515, .30, .16),
        ])
        self.assertEqual([m.side for m in messages], ['them', 'me', 'them'])
        self.assertEqual(self.h._prejudge_req[0], '。。2000一个月')
        self.assertEqual(self.h._pregen_req[0], '。。2000一个月')

    def test_screenshot_order_targets_bottom_incoming_not_older_one(self):
        messages = self.read([
            block('凌波干什么。。', .48, .82, .18),
            block('花钱', .80, .70, .08),
            block('。。', .515, .60, .04),
            block('他现在就一个人', .72, .50, .18),
            block('10：19', .58, .40, .08, .020),
            block('你不是有失业金拿吗', .70, .30, .22),
            block('。。2000一个月', .515, .18, .16),
        ], input_top=.90)
        self.assertNotIn('10：19', [m.text for m in messages])
        self.assertEqual(self.h._reply_key[:3], ('wechat', 'chat', '。。2000一个月'))
        self.assertEqual(self.h._prejudge_req[0], '。。2000一个月')

    def test_timestamp_variants_never_enter_messages_or_context(self):
        for stamp in ('10:25', '10：25', '昨天 11:37', '9月23日 10：25'):
            with self.subTest(stamp=stamp):
                messages = extract_messages([
                    block(stamp, .48, .70, .10, .020),
                    block('最新消息', .48, .45, .16),
                ])
                self.assertEqual([m.text for m in messages], ['最新消息'])

    def test_shifted_outgoing_continuation_stays_with_bubble(self):
        messages = self.read([block('自己长消息第一行', .55, .70, .29),
                              block('较短续行', .535, .66, .12)])
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].side, 'me')
        self.assertAlmostEqual(messages[0].x + messages[0].w, .84)
        self.assertIsNone(self.h._prejudge_req)

    def test_cropped_central_continuation_is_unknown(self):
        messages = self.read([block('只剩续行', .535, .66, .12)])
        self.assertEqual(messages[0].side, 'unknown')
        self.assertIsNone(self.h._pregen_req)

    def test_later_wide_line_can_resolve_initial_unknown(self):
        messages = self.read([block('短首行', .535, .70, .12),
                              block('后面更长的一行', .55, .66, .29),
                              block('第三行', .54, .62, .13)])
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].side, 'me')
        self.assertEqual(len(messages[0].lines), 3)
        self.assertIsNone(self.h._pregen_req)

    def test_real_incoming_still_triggers_both_jobs_and_keeps_own_context(self):
        self.read([block('下午开会', .40, .70, .15), block('我会带材料', .78, .50, .10)])
        self.assertEqual(self.h._prejudge_req[0], '下午开会')
        self.assertEqual(self.h._pregen_req[0], '下午开会')
        self.assertIn('我: 我会带材料', self.h._prejudge_req[1])
        self.flush()
        self.h.applyIncoming_.assert_called_once()

    def test_disabled_judgment_starts_generation_but_never_prejudges(self):
        self.h._judgment_enabled = False
        self.read([block('下午开会', .40, .70, .15)])
        self.assertIsNone(self.h._prejudge_req)
        self.assertFalse(self.h._prejudge_event.is_set())
        self.assertEqual(self.h._pregen_req[0], '下午开会')
        self.assertTrue(self.h._pregen_event.is_set())

    def test_read_result_lists_direction_and_latest_last(self):
        messages = [
            Message('不够啊', 'them', .3, 1, sender='fish'),
            Message('“小军”加入了群聊', 'public', .35, 1),
            Message('我的回复', 'me', .4, 1),
            Message('[未识别文字/表情]', 'them', .5, 0, visual_only=True),
        ]
        self.assertEqual(self.h._format_read_result(messages).splitlines(), [
            '对方（fish）｜不够啊', '公共信息｜“小军”加入了群聊',
            '我｜我的回复', '对方｜⚠ [未识别文字/表情]'])

    def test_public_info_never_becomes_a_reply_target(self):
        messages = self.read([
            block('“fish”邀请“小军”加入了群聊', .48, .55, .40, .020),
        ])
        self.assertEqual([m.side for m in messages], ['public'])
        self.assertIsNone(self.h._reply_key)
        self.assertIsNone(self.h._prejudge_req)
        self.assertIsNone(self.h._pregen_req)

    def test_public_info_is_attributed_in_context(self):
        public = Message('“小军”加入了群聊', 'public', .3, 1)
        newest = Message('欢迎', 'them', .4, 1, sender='fish')
        self.assertEqual(self.h._context_text([public, newest], newest, 2),
                         '公共信息: “小军”加入了群聊')

    def test_adjacent_same_sender_messages_are_one_model_target(self):
        first = Message('明天', 'them', .30, 1, h=.035, sender='fish')
        second = Message('下午三点', 'them', .37, 1, h=.035, sender='fish')
        latest = Message('可以吗', 'them', .44, 1, h=.035, sender='fish')
        HUD['read_conversation'].return_value = {
            'ok': True, 'unchanged': False, 'fingerprint': None,
            'window': {'wid': 1}, 'chat_title': '群聊',
            'messages': [first, second, latest],
        }
        self.h._work_inner()
        self.assertEqual(self.h._prejudge_req[0], '明天\n下午三点\n可以吗')
        self.assertEqual(self.h._pregen_req[0], '明天\n下午三点\n可以吗')
        self.assertIsNone(self.h._pregen_req[1])
        self.assertEqual(self.h._reply_key[:3],
                         ('wechat', '群聊', '明天\n下午三点\n可以吗'))
        self.assertIn('对方（fish）｜可以吗', self.h._read_result_text)

    def test_quote_is_attributed_and_kept_out_of_current_body(self):
        quoted = Message('你试试这个源', 'them', .4, 1, h=.05, sender='fish',
                         quote_sender='白正秋', quote_text='网络环境有关系')
        HUD['read_conversation'].return_value = {
            'ok': True, 'unchanged': False, 'fingerprint': None,
            'window': {'wid': 1}, 'chat_title': '群聊', 'messages': [quoted],
        }
        self.h._work_inner()
        self.assertEqual(quoted.text, '你试试这个源')
        self.assertEqual(self.h._prejudge_req[0],
                         '你试试这个源\n[引用 白正秋: 网络环境有关系]')
        self.assertEqual(self.h._prejudge_req[5], '你试试这个源')
        self.assertIn('↳ 引用（白正秋）｜网络环境有关系', self.h._read_result_text)

    def test_adjusted_candidate_updates_only_its_source_and_rejects_stale_result(self):
        row = {name: Mock() for name in ('text', 'prob', 'track', 'fill')}
        self.h._rows = [[row for _ in range(4)]]
        self.h._candidate_sources = ['另一条', '原候选', None, None]
        self.h.cand_texts = ['另一条', '原候选', None, None]
        self.h._candidate_overrides = {}
        self.h._adjust_seq = {(0, '原候选'): 2}
        self.h._set_probability_label = Mock()
        self.h._set_progress = Mock()
        self.h._relayout = Mock()

        self.h.applyAdjusted_((0, '原候选', '原候选', '过期改写', 1, 'normal'))
        self.assertEqual(self.h.cand_texts[1], '原候选')
        self.h.applyAdjusted_((0, '原候选', '原候选', '短一点', 2, 'normal'))
        self.assertEqual(self.h.cand_texts[:2], ['另一条', '短一点'])
        self.assertEqual(self.h._candidate_overrides[(0, '原候选')], '短一点')
        self.h.slot_tones = ['别的语气']
        self.h.applyAdjusted_((0, '原候选', '短一点', '错误覆盖', 2, 'normal'))
        self.assertEqual(self.h.cand_texts[1], '短一点')

    def test_adjustment_survives_later_ranking_reorder(self):
        self.h._rows = [[{name: Mock() for name in
                          ('text', 'prob', 'track', 'fill')}
                         for _ in range(HUD['styles'].PER_TONE)] for _ in range(3)]
        self.h.cand_texts = [None] * (3 * HUD['styles'].PER_TONE)
        self.h._candidate_sources = [None] * (3 * HUD['styles'].PER_TONE)
        self.h._candidate_overrides = {(0, '原候选'): '微调后的句子'}
        self.h._set_probability_label = Mock()
        self.h._set_progress = Mock()
        self.h._relayout = Mock()
        self.h._row_controls = lambda slot, row: tuple(self.h._rows[slot][row].values())
        self.h._slot_active = lambda slot: slot == 0
        self.h.slot_tones = ['normal', '不用', '不用']
        self.h._render_groups([(
            0, 'normal', [
                {'text': '另一条', 'prob': .90},
                {'text': '原候选', 'prob': .70},
            ])])
        self.assertEqual(self.h.cand_texts[:2], ['另一条', '微调后的句子'])
        self.assertEqual(self.h._candidate_sources[:2], ['另一条', '原候选'])
        self.h._set_probability_label.assert_any_call(
            self.h._rows[0][1]['prob'], 1, '已微调')
        self.h._rows[0][1]['track'].setHidden_.assert_called_with(True)

    def test_visual_only_latest_message_pauses_generation(self):
        message = Message('[未识别文字/表情]', 'them', .8, 0,
                          h=.04, x=.45, w=.10, last_y=.8, visual_only=True)
        HUD['read_conversation'].return_value = {
            'ok': True, 'unchanged': False, 'fingerprint': None,
            'window': {'wid': 1}, 'chat_title': 'chat', 'messages': [message],
        }
        self.h._work_inner()
        self.assertIsNone(self.h._prejudge_req)
        self.assertIsNone(self.h._pregen_req)
        self.flush()
        self.h.applyUnreadable_.assert_called_once()
        self.h.applyIncoming_.assert_not_called()

    def test_disabled_analysis_never_calls_judge(self):
        self.h._judgment_enabled = False
        self.h._wechat_frontmost = True
        self.h._app = HUD['FAKE_APP']
        self.h._reply_key = ('chat', '下午开会')
        self.h.generator.generate.return_value = {'groups': [], 'elapsed_s': 0}
        self.h._finish_generate = Mock()
        newest = SimpleNamespace(text='下午开会', sender=None, side='them')
        self.h._analyze(newest, [newest])
        self.h.judge.judge.assert_not_called()
        self.h.judge.rank_candidates.assert_not_called()
        self.h.generator.generate.assert_called_once()
        self.h._finish_generate.assert_called_once()

    def test_zero_context_means_no_history(self):
        old = SimpleNamespace(text='旧消息', sender=None, side='them')
        newest = SimpleNamespace(text='当前消息', sender=None, side='them')
        self.assertIsNone(self.h._context_text([old, newest], newest, 0))
        self.assertEqual(self.h._context_text([old, newest], newest, 1), '对方: 旧消息')

    def test_incoming_wrapped_message_and_sender_preserved(self):
        messages = extract_messages([block('小王', .40, .80, .05, .020),
                                     block('第一行正文', .40, .65, .25),
                                     block('续行正文', .405, .61, .12)])
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].side, 'them')
        self.assertEqual(messages[0].sender, '小王')
        self.assertEqual(len(messages[0].lines), 2)

    def test_no_incoming_clears_jobs_and_pending_ui(self):
        self.incoming()
        old_epoch = self.h._reply_epoch
        for selector in ('applyJudgment:', 'applyCandidates:', 'applyStreamLine:'):
            self.h._push_reply(selector, 'late result', old_epoch)
        self.read([block('自己回复', .80, .50, .10)])
        self.flush()
        for name in ('applyIncoming_', 'applyJudgment_', 'applyCandidates_', 'applyStreamLine_'):
            getattr(self.h, name).assert_not_called()
        self.assertIsNone(self.h.last_seen)
        self.assertIsNone(self.h.analyzed_text)
        self.assertIsNone(self.h._prejudge_req)
        self.assertIsNone(self.h._pregen_req)

    def test_empty_ocr_also_invalidates_target(self):
        # #58: an empty frame no longer clears the reply target (that reset is what kept
        # the settle analysis from ever finishing), but it must still retire every
        # in-flight worker and re-arm both prework halves at the new epoch — a candidate
        # computed for a message that might have vanished must never surface.
        self.incoming()
        epoch = self.h._reply_epoch
        self.read([])
        self.assertGreater(self.h._reply_epoch, epoch)
        self.assertEqual(self.h._prejudge_req[4], self.h._reply_epoch)
        self.assertEqual(self.h._pregen_req[4], self.h._reply_epoch)
        self.assertIsNone(self.h.analyzed_text)
        self.assertEqual(self.h._reply_key[:3], ('wechat', 'chat', '下午开会'))
        self.assertEqual(self.h.last_seen, '下午开会')

    def test_empty_frame_streak_reuses_without_resetting_settle(self):
        # #58: reads during the empty streak keep the last good frame; the recovery read
        # with the same message is not a new arrival — the epoch stays where the streak
        # put it and the settle timer keeps running.
        self.incoming()
        epoch = self.h._reply_epoch
        change_ts = self.h.last_change_ts
        self.read([])
        self.read([block('下午开会', .40, .70, .15)])
        self.assertEqual(self.h._reply_epoch, epoch + 1)
        self.assertEqual(self.h.last_change_ts, change_ts)
        self.assertEqual(self.h._prejudge_req[4], epoch + 1)

    def test_persistent_empty_frame_gives_up_reuse(self):
        # after the reuse grace the streak falls back to a real empty read: the stale
        # target clears exactly the way the pre-#58 behavior cleared it
        self.incoming()
        self.read([])
        self.h._empty_frame_since = time.monotonic() - 60.0
        self.read([])
        self.assertIsNone(self.h._reply_key)
        self.assertIsNone(self.h.last_seen)
        self.assertIsNone(self.h._prejudge_req)
        self.assertIsNone(self.h._pregen_req)
        self.assertIsNone(self.h._last_full)

    def test_old_worker_completion_and_same_text_reappearance(self):
        self.incoming()
        epoch = self.h._reply_epoch
        self.read([])
        self.incoming()
        self.flush()
        self.h._reply_task(epoch, self.h._push, 'applyCandidates:', 'old replies')
        self.flush()
        self.h.applyCandidates_.assert_not_called()

    def test_stream_callback_retains_original_epoch(self):
        self.incoming()
        callback = self.h._reply_task(self.h._reply_epoch, self.h._stream_hook, time.perf_counter())
        self.read([])
        self.incoming()
        callback(0, 'normal', 'old reply')
        self.flush()
        self.h.applyStreamLine_.assert_not_called()

    def test_old_generation_does_not_restart_or_rank(self):
        self.incoming()
        epoch = self.h._reply_epoch
        self.read([])
        self.h._reply_task(epoch, self.h._gen_with_pregen, '下午开会', None)
        self.h.generator.generate.assert_not_called()
        self.h._reply_task(epoch, self.h._finish_generate, {}, None, time.perf_counter(), None)
        self.h.judge.rank_candidates.assert_not_called()

    def test_same_text_in_different_chat_changes_epoch(self):
        self.incoming()
        epoch = self.h._reply_epoch
        self.read([block('下午开会', .40, .70, .15)], title='another chat')
        self.assertGreater(self.h._reply_epoch, epoch)

    def test_prejudge_completion_cannot_repopulate_cleared_state(self):
        self.incoming()
        class Finished(BaseException):
            pass
        self.h._prejudge_event = Mock()
        self.h._prejudge_event.wait.side_effect = [None, Finished()]
        def judge_then_clear(*args, **kwargs):
            self.read([])
            return {'intent': '约会议', 'confidence': 1, 'risk': 1}
        self.h.judge.judge.side_effect = judge_then_clear
        with self.assertRaises(Finished):
            self.h._prejudge_loop()
        self.assertIsNone(self.h._prejudge_result)

    def test_pregen_completion_cannot_repopulate_cleared_state(self):
        self.incoming()
        class Finished(BaseException):
            pass
        self.h._pregen_event = Mock()
        self.h._pregen_event.wait.side_effect = [None, Finished()]
        def generate_then_clear(*args, **kwargs):
            self.read([])
            return {'groups': []}
        self.h.generator.generate.side_effect = generate_then_clear
        with self.assertRaises(Finished):
            self.h._pregen_loop()
        self.assertIsNone(self.h._pregen_result)

    def test_background_app_hides_without_reading_and_invalidates_reply(self):
        self.incoming()
        old_epoch = self.h._reply_epoch
        HUD['read_conversation'].reset_mock()
        HUD['frontmost_app'].return_value = None
        HUD['screen_capture_ok'].return_value = False

        self.h._work_inner()
        self.flush()

        HUD['read_conversation'].assert_not_called()
        self.h.applyError_.assert_not_called()
        self.h.applyForegroundHidden_.assert_called_once()
        self.assertGreater(self.h._reply_epoch, old_epoch)
        self.assertIsNone(self.h._reply_key)
        self.assertIsNone(self.h._last_full)
        self.assertIsNone(self.h._fingerprint)

    def test_opted_in_background_capture_keeps_reading_without_invalidating_reply(self):
        self.incoming()
        old_epoch = self.h._reply_epoch
        old_key = self.h._reply_key
        self.h.background_capture = True
        HUD['read_conversation'].reset_mock()
        HUD['frontmost_app'].return_value = None
        HUD['read_conversation'].return_value = {
            'ok': True, 'unchanged': True, 'fingerprint': b'background-frame',
            'window': {'wid': 1}, 'chat_title': 'chat', 'messages': [],
        }

        self.h._work_inner()
        self.flush()

        HUD['read_conversation'].assert_called_once()
        self.h.applyHidden_.assert_called_with('微信不在前台 · 后台抓取中')
        self.h.applyForegroundHidden_.assert_not_called()
        self.assertEqual(self.h._reply_epoch, old_epoch)
        self.assertEqual(self.h._reply_key, old_key)

    def test_background_reply_updates_are_accepted_only_when_capture_is_enabled(self):
        self.h._app = HUD['FAKE_APP']
        self.h._wechat_frontmost = False
        self.h._reply_key = ('chat', '下午开会')
        payload = ('下午开会', None, '', '下午开会', 1)

        self.h._push_reply('applyIncoming:', payload, self.h._reply_epoch)
        self.flush()
        self.h.applyIncoming_.assert_not_called()

        self.h.background_capture = True
        self.h._push_reply('applyIncoming:', payload, self.h._reply_epoch)
        self.flush()
        self.h.applyIncoming_.assert_called_once_with(payload)

    def test_return_to_wechat_forces_fresh_window_read(self):
        self.h._app = None
        self.h._win_wid = 7
        self.h._fingerprint = b'old-frame'
        self.h._last_full = {'messages': ['stale']}
        HUD['frontmost_app'].return_value = HUD['FAKE_APP']
        HUD['read_conversation'].return_value = {
            'ok': True, 'unchanged': False, 'fingerprint': b'new-frame',
            'window': {'wid': 9}, 'chat_title': 'current', 'messages': [],
        }

        self.h._work_inner()

        HUD['read_conversation'].assert_called_once_with(
            previous_wid=None, prev_fingerprint=None, prev_layout=None)
        self.assertEqual(self.h._win_wid, 9)

    def test_unknown_foreground_state_does_not_fake_an_app_switch(self):
        self.incoming()
        old_epoch = self.h._reply_epoch
        old_key = self.h._reply_key
        old_full = self.h._last_full
        HUD['read_conversation'].reset_mock()
        HUD['frontmost_app'].return_value = HUD['UNKNOWN']

        self.h._work_inner()
        self.flush()

        HUD['read_conversation'].assert_not_called()
        self.h.applyHidden_.assert_not_called()
        self.assertEqual(self.h._reply_epoch, old_epoch)
        self.assertEqual(self.h._reply_key, old_key)
        self.assertIs(self.h._last_full, old_full)

    def test_return_with_multiple_wechat_windows_rediscovers_main(self):
        self.h._app = None
        self.h._win_wid = 7
        self.h._fingerprint = b'old-frame'
        HUD['frontmost_app'].return_value = HUD['FAKE_APP']
        detached = {
            'kCGWindowOwnerName': 'WeChat', 'kCGWindowNumber': 2,
            'kCGWindowName': '微信 (窗口)', 'kCGWindowOwnerPID': 1,
            'kCGWindowBounds': {'Width': 947, 'Height': 679},
        }
        main = {
            'kCGWindowOwnerName': 'WeChat', 'kCGWindowNumber': 1,
            'kCGWindowName': 'Weixin', 'kCGWindowOwnerPID': 1,
            'kCGWindowBounds': {'Width': 754, 'Height': 593},
        }

        def fresh_read(previous_wid, prev_fingerprint, prev_layout):
            self.assertIsNone(previous_wid)
            self.assertIsNone(prev_fingerprint)
            self.assertIsNone(prev_layout)
            with patch('Quartz.CGWindowListCopyWindowInfo', return_value=[detached, main]):
                selected = find_wechat_window(previous_wid)
            return {
                'ok': True, 'unchanged': False, 'fingerprint': b'new-frame',
                'window': {'wid': selected.wid}, 'chat_title': 'current', 'messages': [],
            }

        HUD['read_conversation'].side_effect = fresh_read
        self.h._work_inner()

        self.assertEqual(self.h._win_wid, 1)

    def test_switch_during_capture_discards_snapshot(self):
        states = iter([HUD['FAKE_APP'], None])
        HUD['frontmost_app'].side_effect = lambda: next(states)
        HUD['read_conversation'].return_value = {
            'ok': True, 'unchanged': False, 'fingerprint': b'stale',
            'window': {'wid': 7}, 'chat_title': 'stale', 'messages': [],
        }

        self.h._work_inner()
        self.flush()

        self.h.applyForegroundHidden_.assert_called_once()
        self.h.applyIncoming_.assert_not_called()
        self.h._show.assert_not_called()
        self.h.applyPosition_.assert_not_called()
        self.assertIsNone(self.h._fingerprint)
        self.assertIsNone(self.h._last_full)

    def test_fill_candidate_without_app_reports_not_frontmost(self):
        self.h.cand_texts = ['收到，马上看']
        self.h._app = None
        sender = Mock()
        sender.tag.return_value = 0

        self.h.fillCandidate_(sender)

        self.h._render.assert_any_call(
            'status', '填入失败：微信 / QQ 不在前台', HUD['PALETTE']['red'])
        HUD['FAKE_APP'].fill_text.assert_not_called()

    def test_fill_candidate_writes_through_current_app_adapter(self):
        HUD['FAKE_APP'].fill_text.reset_mock()
        self.h.cand_texts = ['收到，马上看']
        self.h._app = HUD['FAKE_APP']
        self.h._wechat_frontmost = True
        target = {'box': (1, 2, 3, 4), 'rect': None, 'reason': 'test', 'app': 'wechat'}
        self.h._input_target = target
        sender = Mock()
        sender.tag.return_value = 0

        self.h.fillCandidate_(sender)

        HUD['FAKE_APP'].fill_text.assert_called_once_with('收到，马上看', target=target)

    def test_fill_candidate_rejects_target_from_other_app(self):
        HUD['FAKE_APP'].fill_text.reset_mock()
        self.h.cand_texts = ['收到，马上看']
        self.h._app = HUD['FAKE_APP']     # key='wechat'
        self.h._wechat_frontmost = True
        self.h._input_target = {'box': (1, 2, 3, 4), 'rect': None,
                                'reason': 'test', 'app': 'qq'}
        sender = Mock()
        sender.tag.return_value = 0

        self.h.fillCandidate_(sender)

        self.h._render.assert_any_call(
            'status', '填入失败：输入目标属于另一应用，请等检测框更新后重试',
            HUD['PALETTE']['red'])
        HUD['FAKE_APP'].fill_text.assert_not_called()

    def test_warm_apps_pays_each_apps_one_off_loads(self):
        warm_wechat = SimpleNamespace(display_name='微信', warm=Mock(return_value=5.0))
        ax_qq = SimpleNamespace(display_name='QQ', warm=Mock(return_value=None))
        self.h._read_once = False

        with patch.dict(HUD, {'APPS': (warm_wechat, ax_qq)}):
            self.h._warm_apps()

        self.assertTrue(self.h._read_once)
        warm_wechat.warm.assert_called_once()
        ax_qq.warm.assert_called_once()

    def test_qq_missing_accessibility_gates_reads(self):
        qq = qq_app()
        HUD['frontmost_app'].return_value = qq
        HUD['fill'].has_accessibility.return_value = False

        self.h._work_inner()
        self.h._work_inner()
        self.flush()

        self.h.applyError_.assert_called_with('需要辅助功能权限 · 系统设置 › 隐私与安全性')
        qq.read_conversation.assert_not_called()
        HUD['fill'].request_accessibility.assert_called_once()
        HUD['screen_capture_ok'].assert_not_called()

    def test_qq_path_never_enters_visual_fallback(self):
        qq = qq_app()
        qq.read_conversation.return_value = {
            'ok': True, 'unchanged': False, 'fingerprint': b'f',
            'window': {'wid': 5}, 'chat_title': 'chat',
            'messages': extract_messages([block('下午开会', .40, .70, .15)]),
        }
        HUD['frontmost_app'].return_value = qq

        with patch('visual_fill.chat_signature') as sig, \
                patch('input_region.locate_visual_input') as lvi:
            self.h._work_inner()

            sig.assert_not_called()
            lvi.assert_not_called()

        HUD['screen_capture_ok'].assert_not_called()
        self.assertIsNotNone(self.h._input_target)
        self.assertNotIn('visual_rect', self.h._input_target)
        self.assertNotIn('chat_signature', self.h._input_target)
        self.assertEqual(self.h._input_target['app'], 'qq')

    def test_switching_adapter_hides_panel_and_resets_reply(self):
        qq = qq_app()
        self.h._app = HUD['FAKE_APP']
        self.h._reply_key = ('wechat', 'chat', '下午开会')
        self.h._foreground_epoch = 3

        self.h._set_foreground_state(qq)
        self.flush()

        self.assertEqual(self.h._foreground_epoch, 4)
        self.assertIs(self.h._app, qq)
        self.assertIsNone(self.h._reply_key)
        self.h.applyForegroundHidden_.assert_called_once_with('已切换到QQ')

    def test_capture_returning_other_adapter_discards_snapshot(self):
        qq = qq_app()
        states = iter([HUD['FAKE_APP'], qq])
        HUD['frontmost_app'].side_effect = lambda: next(states)
        HUD['read_conversation'].return_value = {
            'ok': True, 'unchanged': False, 'fingerprint': b'stale',
            'window': {'wid': 7}, 'chat_title': 'stale',
            'messages': extract_messages([block('下午开会', .40, .70, .15)]),
        }

        self.h._work_inner()
        self.flush()

        self.h.applyForegroundHidden_.assert_called_once_with('已切换到QQ')
        self.h.applyIncoming_.assert_not_called()
        self.assertIsNone(self.h._last_full)
        self.assertIsNone(self.h._fingerprint)

    def test_leave_and_return_during_capture_discards_old_snapshot(self):
        HUD['frontmost_app'].return_value = HUD['FAKE_APP']

        def read_then_round_trip(**_kwargs):
            self.h._set_foreground_state(None)
            self.h._set_foreground_state(HUD['FAKE_APP'])
            return {
                'ok': True, 'unchanged': False, 'fingerprint': b'stale',
                'window': {'wid': 7}, 'chat_title': 'stale', 'messages': [],
            }

        HUD['read_conversation'].side_effect = read_then_round_trip
        self.h._work_inner()
        self.flush()

        self.h.applyForegroundHidden_.assert_called_once()
        self.h._show.assert_not_called()
        self.assertIsNone(self.h._fingerprint)
        self.assertIsNone(self.h._last_full)

    def test_transient_read_failure_does_not_hide_hud(self):
        HUD['read_conversation'].return_value = {'ok': False, 'error': 'transient'}

        self.h._work_inner()
        self.flush()

        self.h.applyHidden_.assert_not_called()
        self.assertIsNotNone(self.h._read_fail_since)

    def test_sustained_read_failure_hides_and_forces_rediscovery(self):
        self.h._app = HUD['FAKE_APP']
        self.h._wechat_frontmost = True
        self.h._foreground_epoch = 1
        self.h._win_wid = 7
        self.h._fingerprint = b'old'
        self.h._last_full = {'messages': ['old']}
        self.h._read_fail_since = time.monotonic() - 3
        old_epoch = self.h._reply_epoch
        self.h._reply_key = ('chat', 'old')
        HUD['read_conversation'].return_value = {'ok': False, 'error': 'gone'}

        self.h._work_inner()
        self.flush()

        self.h.applyForegroundHidden_.assert_called_once_with('gone')
        self.assertGreater(self.h._reply_epoch, old_epoch)
        self.assertIsNone(self.h._reply_key)
        self.assertIsNone(self.h._win_wid)
        self.assertIsNone(self.h._fingerprint)
        self.assertIsNone(self.h._last_full)
        self.h.applyReplyUpdate_((old_epoch, 'applyError:', 'late result'))
        self.h.applyError_.assert_not_called()


if __name__ == '__main__':
    unittest.main()
