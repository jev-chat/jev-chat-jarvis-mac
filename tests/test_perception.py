"""Perception pure-function regressions on synthetic OCR: chat-title extraction and
message folding/side assignment. Run: python -B -m unittest discover -s tests -v.

No Cocoa, no screen read, no model calls; `block()` comes from tests/support_hud.py.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/: for support_hud
from perception import Message, extract_chat_title, extract_messages, reply_span, reply_text
from support_hud import block


class PerceptionTests(unittest.TestCase):
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

    def test_opposite_known_sides_never_fold_despite_close_edges(self):
        messages = extract_messages([block('收到的消息', .495, .70, .18),
                                     block('自己发出的消息', .51, .66, .34)])
        self.assertEqual([m.side for m in messages], ['them', 'me'])

    def test_stray_small_type_sender_line_is_dropped(self):
        # #62: a group-chat image/voice bubble renders only the sender name above
        # it; the name alone must never become a message for the judge.
        self.assertEqual(extract_messages([block('小王', .40, .60, .05, .020)]), [])
        messages = extract_messages([block('小王', .40, .60, .05, .020),
                                     block('自己发出的消息', .51, .66, .34)])
        self.assertEqual([(m.text, m.side) for m in messages],
                         [('自己发出的消息', 'me')])

    def test_small_type_sender_above_message_still_attaches(self):
        messages = extract_messages([block('小王', .40, .60, .05, .020),
                                     block('下午开会', .40, .45, .15)])
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].sender, '小王')
        self.assertEqual(messages[0].text, '下午开会')

    def test_incoming_wrapped_message_and_sender_preserved(self):
        messages = extract_messages([block('小王', .40, .80, .05, .020),
                                     block('第一行正文', .40, .65, .25),
                                     block('续行正文', .405, .61, .12)])
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].side, 'them')
        self.assertEqual(messages[0].sender, '小王')
        self.assertEqual(len(messages[0].lines), 2)

    def test_calibrated_timing_carries_upstream_capture(self):
        # #110: read_calibrated receives pixels the caller already paid for; its
        # timing must carry that capture cost, not report 抓取 0ms in the read log.
        from calibration import Calibration
        import perception
        cal = Calibration(600, 600, 120, 60, 450, 480)
        win = {'wid': 1, 'w': 600, 'h': 600, 'x': 0, 'y': 0}
        with patch('perception.ocr_image', return_value=[]), \
                patch('calibrated_messages.recover_numeric_bubbles', return_value=[]), \
                patch('calibrated_messages.extract', return_value=[]):
            res = perception.read_calibrated(object(), cal, win, capture_ms=123.0)
        t = res['timing_ms']
        self.assertEqual(t['capture'], 123.0)
        self.assertAlmostEqual(t['total'], 123.0 + t['ocr'], places=6)
        self.assertEqual(t['capture_path'], 'manual')

    def test_read_conversation_passes_capture_cost_to_calibrated(self):
        # #110: the calibrated branch must forward the capture-side wall time so the
        # read log reports real capture cost — this covers the wiring itself.
        from types import SimpleNamespace
        from calibration import Calibration
        import perception
        cal = Calibration(600, 600, 120, 60, 450, 480)
        win = SimpleNamespace(wid=1, title='t', w=600, h=600, x=0, y=0)
        with patch.object(perception, 'find_wechat_window', return_value=win), \
                patch.object(perception, 'capture_window', return_value=True), \
                patch.object(perception, '_load_png_image', return_value=object()), \
                patch.object(perception, 'read_calibrated', return_value={'ok': True}) as rc:
            res = perception.read_conversation(calibration=cal)
        self.assertEqual(res, {'ok': True})
        self.assertGreaterEqual(rc.call_args.kwargs['capture_ms'], 0.0)
        self.assertLess(rc.call_args.kwargs['capture_ms'], 1000.0)
        self.assertEqual(rc.call_args.args[2]['wid'], 1)
    def test_group_system_rows_are_public_not_messages_from_either_side(self):
        messages = extract_messages([
            block('“飞”通过扫描“fish”分享的二维码加入群聊', .48, .80, .40, .020),
            block('fish', .40, .70, .05, .020),
            block('有可能，你试试该 brew 源', .40, .64, .28),
            block('“小军”与群里其他人都不是朋友关系，请注意隐私安全',
                  .37, .50, .55, .020),
            block('我的回复', .78, .40, .10),
        ])
        self.assertEqual([m.side for m in messages],
                         ['public', 'them', 'public', 'me'])
        self.assertEqual(messages[1].sender, 'fish')
        self.assertEqual(messages[1].text, '有可能，你试试该 brew 源')

    def test_ordinary_group_wording_is_not_promoted_to_public_info(self):
        messages = extract_messages([
            block('怎么邀请他加入群聊？', .40, .60, .28),
            block('他说自己和群里人不是朋友', .40, .50, .32),
        ])
        self.assertEqual([m.side for m in messages], ['them', 'them'])

    def test_wrapped_group_system_notice_is_one_public_row(self):
        messages = extract_messages([
            block('“小军”与群里其他人都不是朋友关系，', .38, .62, .48, .020),
            block('请注意隐私安全', .50, .58, .20, .020),
        ])
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].side, 'public')
        self.assertEqual(messages[0].lines,
                         ['“小军”与群里其他人都不是朋友关系，\n请注意隐私安全'])

    def test_public_row_never_folds_into_adjacent_message(self):
        messages = extract_messages([
            block('收到', .48, .66, .16),
            block('“fish”邀请“小军”加入了群聊', .48, .625, .36),
        ])
        self.assertEqual([m.side for m in messages], ['them', 'public'])

    def test_small_quoted_preview_is_separate_from_current_words(self):
        messages = extract_messages([
            block('你试试这个源', .40, .70, .24, .035),
            block('白正秋: 和我的网络环境有关系', .40, .675, .32, .020),
        ])
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].text, '你试试这个源')
        self.assertEqual(messages[0].quote_sender, '白正秋')
        self.assertEqual(messages[0].quote_text, '和我的网络环境有关系')
        self.assertEqual(reply_text(messages),
                         '你试试这个源\n[引用 白正秋: 和我的网络环境有关系]')

    def test_normal_size_colon_line_remains_body(self):
        messages = extract_messages([
            block('安排如下', .40, .70, .22, .035),
            block('负责人: 我', .40, .665, .18, .035),
        ])
        self.assertEqual(len(messages), 1)
        self.assertIsNone(messages[0].quote_text)
        self.assertIn('负责人: 我', messages[0].text)

    def test_quote_line_before_next_bubble_is_not_its_sender(self):
        messages = extract_messages([
            block('你试试这个源', .40, .75, .24, .035),
            block('白正秋: 网络环境有关系', .40, .725, .28, .020),
            block('我也试试', .40, .66, .22, .035),
        ])
        self.assertEqual([m.text for m in messages], ['你试试这个源', '我也试试'])
        self.assertEqual(messages[0].quote_sender, '白正秋')
        self.assertIsNone(messages[1].sender)

    def test_one_character_quoted_preview_is_kept_as_history(self):
        messages = extract_messages([
            block('我也试试', .40, .70, .22, .035),
            block('Lee: 可', .40, .675, .16, .020),
        ])
        self.assertEqual(messages[0].text, '我也试试')
        self.assertEqual((messages[0].quote_sender, messages[0].quote_text),
                         ('Lee', '可'))

    def test_reply_span_stops_at_other_sender_or_long_gap(self):
        fish1 = Message('明天', 'them', .30, 1, h=.035, sender='fish')
        fish2 = Message('下午三点', 'them', .37, 1, h=.035, sender='fish')
        lee = Message('我也去', 'them', .44, 1, h=.035, sender='Lee')
        fish3 = Message('可以吗', 'them', .51, 1, h=.035, sender='fish')
        self.assertEqual(reply_span([fish1, fish2, lee, fish3], fish2), [fish1, fish2])
        self.assertEqual(reply_span([fish1, fish2, lee, fish3], fish3), [fish3])
        distant = Message('还有件事', 'them', .60, 1, h=.035, sender='fish')
        self.assertEqual(reply_span([fish1, distant], distant), [distant])

    def test_reply_span_stops_at_own_public_and_unreadable(self):
        first = Message('一', 'them', .30, 1, h=.035)
        for side, visual in [('me', False), ('public', False), ('them', True)]:
            blocker = Message('中间', side, .36, 1, h=.035, visual_only=visual)
            last = Message('二', 'them', .42, 1, h=.035)
            self.assertEqual(reply_span([first, blocker, last], last), [last])


if __name__ == '__main__':
    unittest.main()
