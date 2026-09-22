"""Offline regressions for #11 and #14. Run: python -B -m unittest discover -s tests -v.

Load the actual HUD methods through AST so the tests never start Cocoa, read the
screen, load user credentials, or make model calls. Perception uses synthetic OCR.
"""
import ast
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from perception import TextBlock, extract_chat_title, extract_messages, find_wechat_window


def hud_harness():
    tree = ast.parse((ROOT / 'src/hud.py').read_text())
    source = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'HudController')
    names = {'_work_inner', '_set_foreground_state', '_push', '_reply_task', '_reply_current', '_push_reply',
             'applyReplyUpdate_', 'applyWaiting_', '_context_text', '_stream_hook',
             '_take_pregen', '_gen_with_pregen', '_finish_generate',
             '_prejudge_loop', '_pregen_loop'}
    methods = [n for n in source.body if isinstance(n, ast.FunctionDef) and n.name in names]
    for method in methods:
        method.decorator_list = []
    klass = ast.ClassDef(name='Harness', bases=[], keywords=[], body=methods, decorator_list=[])
    scope = {'fill': SimpleNamespace(locate_input=Mock(return_value={'box': None, 'rect': None, 'reason': 'test'})), 'time': time, 'threading': threading, '_log': lambda *_: None,
             'frontmost_app_is_wechat': Mock(return_value=True),
             'screen_capture_ok': Mock(return_value=True), 'request_screen_capture': Mock(),
             'read_conversation': Mock(),
             'PALETTE': {'muted': None}, 'CONTEXT_TURNS': 8, 'JUDGE_TURNS': 4,
             'SLOW_TICK': 1, 'BURST_TICK': .45, 'FAST_TICK': .25, 'BURST_READS': 3,
             'READ_FAILURE_HIDE_S': 2,
             'SETTLE_S': 1.2, 'STABLE_READS': 3, 'EARLY_SETTLE_S': .7, 'MIN_GAP_S': 2}
    module = ast.fix_missing_locations(ast.Module(body=[klass], type_ignores=[]))
    exec(compile(module, str(ROOT / 'src/hud.py'), 'exec'), scope)
    return scope['Harness'], scope


Harness, HUD = hud_harness()


def block(text, x, y, w, h=.035):
    return TextBlock(text, 1.0, x, y, w, h)


class OutgoingTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('input_region.locate_visual_input', return_value=None))
        self.h = h = Harness()
        HUD['read_conversation'].reset_mock()
        HUD['read_conversation'].side_effect = None
        HUD['frontmost_app_is_wechat'].reset_mock()
        HUD['frontmost_app_is_wechat'].side_effect = None
        HUD['frontmost_app_is_wechat'].return_value = True
        HUD['screen_capture_ok'].reset_mock()
        HUD['screen_capture_ok'].return_value = True
        for name, value in dict(
            _reply_key=None, _reply_epoch=0, _reply_worker=threading.local(),
            last_seen=None, analyzed_text=None, _win_wid=None, _fingerprint=None,
            _burst_left=3, _last_full=None, _show_boxes=False, _read_once=True,
            _gen_epoch=0, _last_skip_reason=None, _prejudge_req=None, _prejudge_result=None,
            _pregen_req=None, _pregen_result=None, _pregen_running=False,
            _prejudging=False, _paused=False, _analyzing=False,
            _stable_n=0, last_change_ts=0, last_analyze_ts=0,
            _wechat_frontmost=None, _foreground_epoch=0,
            _read_fail_since=None, _read_fail_hidden=False,
            _prejudge_event=threading.Event(), _pregen_event=threading.Event(),
            slot_tones=['normal'], _stream_rows={}, _last_context=None,
        ).items():
            setattr(h, name, value)
        h._show = Mock()
        h._render = Mock()
        h._clear_candidates = Mock()
        h.rows = {'cand_header': Mock()}
        h._slot_active = lambda _: True
        h._payload_current = lambda _: True
        h.generator = Mock()
        h.judge = Mock()
        h._model_lock = threading.Lock()
        h._judged_once = True
        for name in ['applyIncoming_', 'applyPending_', 'applyJudgment_',
                     'applyCandidates_', 'applyStreamLine_', 'applyError_',
                     'applyPosition_', 'applyChat_', 'applyBoxes_', 'applyHidden_',
                     'applyForegroundHidden_']:
            setattr(h, name, Mock())
        self.queue = []
        h.performSelectorOnMainThread_withObject_waitUntilDone_ = lambda s, p, w: self.queue.append((s, p))

    def read(self, blocks, title='chat'):
        messages = extract_messages(blocks)
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

    def test_short_chat_titles_survive_header_controls(self):
        for name in ('王', '张三', '李经理', 'A', '7', '项目讨论群'):
            with self.subTest(name=name):
                blocks = [block(name, .40, .94, .16, .025),
                          block('...', .92, .96, .03, .025),
                          block('口', .85, .94, .025, .025)]
                self.assertEqual(extract_chat_title(blocks), name)

    def test_title_fragments_join_without_distant_controls(self):
        blocks = [block('项目讨论', .40, .94, .12, .025),
                  block('组', .54, .945, .025, .025),
                  block('口口', .85, .95, .05, .025),
                  block('折叠聊天', .40, .905, .10, .025),
                  block('侧栏联系人', .10, .94, .15, .025),
                  block('下午开会', .40, .70, .15)]
        self.assertEqual(extract_chat_title(blocks), '项目讨论 组')
        self.assertEqual(extract_chat_title(blocks[2:]), '')

    def test_switch_short_titles_with_same_message_invalidates_old_reply(self):
        titles = [extract_chat_title([block(name, .40, .94, .10, .025)])
                  for name in ('张三', '李经理')]
        self.read([block('下午开会', .40, .70, .15)], title=titles[0])
        epoch = self.h._reply_epoch
        self.h._push_reply('applyCandidates:', 'old replies', epoch)
        self.read([block('下午开会', .40, .70, .15)], title=titles[1])
        self.assertGreater(self.h._reply_epoch, epoch)
        self.assertEqual(self.h._reply_key, ('李经理', '下午开会'))
        self.assertEqual(self.h._prejudge_req[0], '下午开会')
        self.assertEqual(self.h._pregen_req[0], '下午开会')
        self.flush()
        self.h.applyCandidates_.assert_not_called()

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

    def test_opposite_known_sides_never_fold_despite_close_edges(self):
        messages = extract_messages([block('收到的消息', .495, .70, .18),
                                     block('自己发出的消息', .51, .66, .34)])
        self.assertEqual([m.side for m in messages], ['them', 'me'])

    def test_real_incoming_still_triggers_both_jobs_and_keeps_own_context(self):
        self.read([block('下午开会', .40, .70, .15), block('我会带材料', .78, .50, .10)])
        self.assertEqual(self.h._prejudge_req[0], '下午开会')
        self.assertEqual(self.h._pregen_req[0], '下午开会')
        self.assertIn('我: 我会带材料', self.h._prejudge_req[1])
        self.flush()
        self.h.applyIncoming_.assert_called_once()

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
        self.incoming()
        self.read([])
        self.assertIsNone(self.h._reply_key)
        self.assertIsNone(self.h._pregen_req)

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
        HUD['frontmost_app_is_wechat'].return_value = False
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

    def test_return_to_wechat_forces_fresh_window_read(self):
        self.h._wechat_frontmost = False
        self.h._win_wid = 7
        self.h._fingerprint = b'old-frame'
        self.h._last_full = {'messages': ['stale']}
        HUD['frontmost_app_is_wechat'].return_value = True
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
        HUD['frontmost_app_is_wechat'].return_value = None

        self.h._work_inner()
        self.flush()

        HUD['read_conversation'].assert_not_called()
        self.h.applyHidden_.assert_not_called()
        self.assertEqual(self.h._reply_epoch, old_epoch)
        self.assertEqual(self.h._reply_key, old_key)
        self.assertIs(self.h._last_full, old_full)

    def test_return_with_multiple_wechat_windows_rediscovers_main(self):
        self.h._wechat_frontmost = False
        self.h._win_wid = 7
        self.h._fingerprint = b'old-frame'
        HUD['frontmost_app_is_wechat'].return_value = True
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
        states = iter([True, False])
        HUD['frontmost_app_is_wechat'].side_effect = lambda: next(states)
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

    def test_leave_and_return_during_capture_discards_old_snapshot(self):
        HUD['frontmost_app_is_wechat'].return_value = True

        def read_then_round_trip(**_kwargs):
            self.h._set_foreground_state(False)
            self.h._set_foreground_state(True)
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
