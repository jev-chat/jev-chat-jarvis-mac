"""Drafts never enter message/context extraction; use synthetic OCR and images."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import Quartz as Q
import perception as p


def block(text, top, height=.03, x=.4, width=.15):
    # .03 is a body-sized line (≥ MESSAGE_H_MIN); a .025 default used to fall into
    # the sender-name/body dead zone (USERNAME_H_MAX=.026 .. MESSAGE_H_MIN=.028)
    # and read as a stray sender name once #62 restored that drop.
    return p.TextBlock(text, 1, x, 1-top-height, width, height)


class DraftExclusionTests(unittest.TestCase):
    def test_chat_title_ignores_only_trailing_numeric_member_count(self):
        for raw, expected in [
            ('项目讨论组（20）', '项目讨论组'),
            ('项目讨论组(21)', '项目讨论组'),
            ('项目讨论组 （ ２０ ） ', '项目讨论组'),
            ('项目讨论组（内部）（20）', '项目讨论组（内部）'),
            ('项目讨论组（内部）', '项目讨论组（内部）'),
            ('项目(20)讨论组', '项目(20)讨论组'),
            ('(20)', ''),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(p.extract_chat_title([block(raw, .04)]), expected)
        self.assertEqual(p.extract_chat_title([
            block('项目讨论组', .04), block('(20)', .04, x=.60)]), '项目讨论组')

    def test_resized_input_and_straddling_text_are_excluded(self):
        for boundary in (.52, .65, .82):
            messages = p.extract_messages([
                block('真实消息', boundary-.10),
                block('跨过输入边界', boundary-.01),
                block('未发送草稿', boundary+.01)], input_top=boundary)
            self.assertEqual([m.text for m in messages], ['真实消息'])

    def test_tight_sender_is_separate_at_multiple_window_heights(self):
        for scale in (.5, 1, 1.4):
            messages = p.extract_messages([
                block('测试成员-A', .4, .018*scale),
                block('明白收到', .4+.035*scale, .03*scale, x=.412),
                block('第二行正文', .4+.065*scale, .03*scale, x=.412)], input_top=.85)
            self.assertEqual(messages[0].sender, '测试成员-A')
            self.assertEqual(messages[0].text, '明白收到\n第二行正文')

    def test_equal_sized_short_body_is_not_a_sender(self):
        messages = p.extract_messages([block('明白', .4), block('收到', .43)], input_top=.8)
        self.assertIsNone(messages[0].sender)
        self.assertEqual(messages[0].text, '明白\n收到')

    def test_separate_short_bubbles_are_not_sender_names(self):
        messages = p.extract_messages([
            block('不够啊', .40, .016),
            block('还要交社保', .457, .020),
        ], input_top=.8)
        self.assertEqual([m.text for m in messages], ['不够啊', '还要交社保'])
        self.assertTrue(all(m.sender is None for m in messages))

    def test_single_short_message_is_kept_when_pixels_confirm_its_bubble(self):
        messages = p.extract_messages(
            [block('买了', .70, .020)], input_top=.9,
            defer_small_sender_filter=True)
        self.assertEqual([m.text for m in messages], ['买了'])
        self.assertTrue(messages[0].suspected_sender)
        with patch.object(p, '_incoming_bubble_boxes',
                          return_value=[(.39, .69, .18, .05)]):
            p._append_unreadable_incoming(object(), messages, .30, .90)
        self.assertEqual([m.text for m in messages], ['买了'])
        self.assertFalse(messages[0].visual_only)

    def test_single_sender_name_is_removed_without_a_matching_bubble(self):
        messages = p.extract_messages(
            [block('小王', .60, .020, width=.05)], input_top=.9,
            defer_small_sender_filter=True)
        self.assertTrue(messages[0].suspected_sender)
        with patch.object(p, '_incoming_bubble_boxes', return_value=[]):
            p._append_unreadable_incoming(object(), messages, .30, .90)
        self.assertEqual(messages, [])

    def test_incoming_bubble_corrects_side_and_splits_folded_sender(self):
        message = p.Message(
            'fish\n我已经解决了', 'me', .34, 1, h=.11, x=.48, w=.30,
            last_y=.405, lines=['fish', '我已经解决了'])
        messages = [message]
        with patch.object(p, '_incoming_bubble_boxes',
                          return_value=[(.39, .38, .42, .09)]):
            p._append_unreadable_incoming(object(), messages, .30, .90)
        self.assertEqual(len(messages), 1)
        self.assertEqual(message.side, 'them')
        self.assertEqual(message.sender, 'fish')
        self.assertEqual(message.text, '我已经解决了')

    def test_incoming_bubble_splits_sender_embedded_in_one_ocr_block(self):
        message = p.Message(
            'fish\n有可能，你试试该 brew 源', 'unknown', .34, 1,
            h=.11, x=.40, w=.34, last_y=.34,
            lines=['fish\n有可能，你试试该 brew 源'])
        with patch.object(p, '_incoming_bubble_boxes',
                          return_value=[(.39, .38, .42, .09)]):
            p._append_unreadable_incoming(object(), [message], .30, .90)
        self.assertEqual(message.side, 'them')
        self.assertEqual(message.sender, 'fish')
        self.assertEqual(message.text, '有可能，你试试该 brew 源')
        self.assertEqual(message.lines, ['有可能，你试试该 brew 源'])

    def test_sender_outside_bubble_attaches_even_when_font_is_not_smaller(self):
        sender = p.Message('James≈', 'them', .270, .30, h=.025, x=.40, w=.08,
                           last_y=.270, lines=['James≈'])
        body = p.Message('我也 fork 了一下', 'them', .305, .98, h=.040,
                         x=.415, w=.22, last_y=.305, lines=['我也 fork 了一下'])
        messages = [sender, body]
        with patch.object(p, '_incoming_bubble_boxes',
                          return_value=[(.39, .300, .30, .065)]):
            p._append_unreadable_incoming(object(), messages, .30, .90)
        self.assertEqual(messages, [body])
        self.assertEqual(body.sender, 'James≈')
        self.assertEqual(body.text, '我也 fork 了一下')

    def test_sentence_like_group_name_attaches_by_bubble_geometry(self):
        sender = p.Message('呜呜呜呜呜', 'them', .680, .50, h=.025, x=.40, w=.14,
                           last_y=.680, lines=['呜呜呜呜呜'])
        body = p.Message('感觉有些消息识别不出来', 'them', .715, .99,
                         h=.045, x=.415, w=.34, last_y=.715,
                         lines=['感觉有些消息识别不出来'])
        messages = [sender, body]
        with patch.object(p, '_incoming_bubble_boxes',
                          return_value=[(.39, .710, .39, .065)]):
            p._append_unreadable_incoming(object(), messages, .30, .90)
        self.assertEqual(messages, [body])
        self.assertEqual(body.sender, '呜呜呜呜呜')

    def test_two_real_bubbles_are_not_mistaken_for_sender_and_body(self):
        first = p.Message('这是第一条', 'them', .30, .99, h=.04, x=.415, w=.20,
                          last_y=.30, lines=['这是第一条'])
        second = p.Message('这是第二条', 'them', .38, .99, h=.04, x=.415, w=.20,
                           last_y=.38, lines=['这是第二条'])
        messages = [first, second]
        with patch.object(p, '_incoming_bubble_boxes', return_value=[
                (.39, .295, .28, .055), (.39, .375, .28, .055)]):
            p._append_unreadable_incoming(object(), messages, .30, .90)
        self.assertEqual(messages, [first, second])
        self.assertIsNone(second.sender)

    def test_sender_above_unreadable_bubble_is_preserved_on_placeholder(self):
        sender = p.Message('Lee', 'them', .50, .60, h=.022, x=.40, w=.06,
                           last_y=.50, lines=['Lee'], suspected_sender=True)
        messages = [sender]
        with patch.object(p, '_incoming_bubble_boxes',
                          return_value=[(.39, .53, .24, .06)]), \
             patch.object(p, '_punctuation_only_text', return_value=None):
            p._append_unreadable_incoming(object(), messages, .30, .90)
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0].visual_only)
        self.assertEqual(messages[0].sender, 'Lee')

    def test_public_info_is_not_reclassified_by_noisy_bubble_component(self):
        public = p.Message('“fish”邀请“小军”加入了群聊', 'public', .40, 1,
                           h=.03, x=.40, w=.42, last_y=.40,
                           lines=['“fish”邀请“小军”加入了群聊'])
        with patch.object(p, '_incoming_bubble_boxes',
                          return_value=[(.39, .39, .44, .05)]):
            p._append_unreadable_incoming(object(), [public], .30, .90)
        self.assertEqual(public.side, 'public')

    def test_timestamp_with_ocr_period_is_filtered(self):
        messages = p.extract_messages([
            block('0:55.', .40, .012, x=.65, width=.05),
            block('还要交社保', .46),
        ], input_top=.8)
        self.assertEqual([m.text for m in messages], ['还要交社保'])

    def test_dynamic_chat_left_excludes_sidebar_sliver(self):
        messages = p.extract_messages([
            block('侧栏未读', .35, x=.34, width=.03),
            block('对方消息', .45, x=.49, width=.12),
            block('我的长回复', .55, x=.50, width=.36),
        ], input_top=.8, chat_left=.372)
        self.assertEqual([m.text for m in messages], ['对方消息', '我的长回复'])
        self.assertEqual([m.side for m in messages], ['them', 'me'])

    def test_short_wrapped_outgoing_line_inherits_whole_bubble_side(self):
        # Regression from a real WeChat bubble.  Vision exposes each visual row as a
        # separate text box.  The long row reaches the outgoing/right anchor, but its
        # short continuation looks incoming if classified before the rows are folded.
        messages = p.extract_messages([
            block('2000搞这交完社保直接原地归零，给', .50,
                  x=.50, width=.38),
            block('社保打工啊', .531, x=.501, width=.13),
        ], input_top=.8, chat_left=.372)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].text,
                         '2000搞这交完社保直接原地归零，给\n社保打工啊')
        self.assertEqual(messages[0].side, 'me')

    def canvas(self, draft=False):
        ctx=Q.CGBitmapContextCreate(None,800,600,8,3200,Q.CGColorSpaceCreateDeviceRGB(),Q.kCGImageAlphaPremultipliedLast)
        Q.CGContextSetRGBFillColor(ctx,1,1,1,1)
        Q.CGContextFillRect(ctx,Q.CGRectMake(0,0,800,600))
        if draft:
            Q.CGContextSetRGBFillColor(ctx,0,0,0,1)
            Q.CGContextFillRect(ctx,Q.CGRectMake(350,150,100,20))
        return Q.CGBitmapContextCreateImage(ctx)

    def test_unreadable_incoming_bubble_gets_safe_placeholder(self):
        ctx=Q.CGBitmapContextCreate(None,800,600,8,3200,Q.CGColorSpaceCreateDeviceRGB(),Q.kCGImageAlphaPremultipliedLast)
        Q.CGContextSetRGBFillColor(ctx,.98,.98,.98,1)
        Q.CGContextFillRect(ctx,Q.CGRectMake(0,0,800,600))
        Q.CGContextSetRGBFillColor(ctx,.91,.91,.92,1)
        Q.CGContextFillRect(ctx,Q.CGRectMake(300,80,120,34))
        image=Q.CGBitmapContextCreateImage(ctx)
        messages = [p.Message('旧消息', 'them', .30, 1, h=.03, x=.40, w=.12,
                              last_y=.30)]
        p._append_unreadable_incoming(image, messages, .30, .90)
        self.assertTrue(messages[-1].visual_only)
        self.assertEqual(messages[-1].text, '[未识别文字/表情]')

    def test_short_character_missing_from_full_ocr_is_recovered_from_bubble(self):
        messages = [p.Message('上一条', 'them', .30, 1, h=.03, x=.40, w=.12,
                              last_y=.30)]
        box = (.39, .70, .16, .055)
        with patch.object(p, '_incoming_bubble_boxes', return_value=[box]), \
             patch.object(p, '_punctuation_only_text', return_value=None), \
             patch.object(p, '_short_bubble_text', return_value=('可', .96)):
            p._append_unreadable_incoming(object(), messages, .30, .90)
        self.assertEqual(messages[-1].text, '可')
        self.assertAlmostEqual(messages[-1].conf, .96)
        self.assertFalse(messages[-1].visual_only)

    def test_short_bubble_retry_disables_language_correction(self):
        with patch.object(p, '_vision_blocks', return_value=[
                p.TextBlock('可', .96, .2, .2, .2, .2)]) as vision:
            result = p._short_bubble_text(self.canvas(), (.36, .35, .20, .10))
        self.assertEqual(result, ('可', .96))
        self.assertFalse(vision.call_args.kwargs['language_correction'])
        self.assertEqual(vision.call_args.kwargs['minimum_text_height'], .005)

    def test_short_bubble_retry_excludes_sender_caught_in_crop_padding(self):
        # Vision coordinates are bottom-origin. The nickname sits at the crop's top,
        # outside the original bubble; only the body center is inside the bubble.
        blocks = [
            p.TextBlock('Lee', .99, .20, .94, .20, .04),
            p.TextBlock('可', .96, .30, .35, .15, .20),
        ]
        with patch.object(p, '_vision_blocks', return_value=blocks):
            result = p._short_bubble_text(self.canvas(), (.36, .35, .20, .10))
        self.assertEqual(result, ('可', .96))

    def test_short_bubble_retry_rejects_text_only_in_padding(self):
        with patch.object(p, '_vision_blocks', return_value=[
                p.TextBlock('Lee', .99, .20, .94, .20, .04)]):
            result = p._short_bubble_text(self.canvas(), (.36, .35, .20, .10))
        self.assertIsNone(result)

    def test_short_bubble_retry_keeps_message_that_matches_ui_word(self):
        with patch.object(p, '_vision_blocks', return_value=[
                p.TextBlock('发送', .95, .25, .35, .20, .20)]):
            result = p._short_bubble_text(self.canvas(), (.36, .35, .20, .10))
        self.assertEqual(result, ('发送', .95))

    def test_short_bubble_retry_rejects_low_confidence_texture_guess(self):
        with patch.object(p, '_vision_blocks', return_value=[
                p.TextBlock('可', p.SHORT_RETRY_MIN_CONF - .01, .25, .35, .20, .20)]):
            result = p._short_bubble_text(self.canvas(), (.36, .35, .20, .10))
        self.assertIsNone(result)

    def test_punctuation_recovery_skips_second_vision_request(self):
        messages = [p.Message('上一条', 'them', .30, 1, h=.03, x=.40, w=.12,
                              last_y=.30)]
        box = (.39, .70, .16, .055)
        with patch.object(p, '_incoming_bubble_boxes', return_value=[box]), \
             patch.object(p, '_punctuation_only_text', return_value='。。。'), \
             patch.object(p, '_short_bubble_text') as retry:
            p._append_unreadable_incoming(object(), messages, .30, .90)
        retry.assert_not_called()
        self.assertEqual(messages[-1].text, '。。。')

    def test_vision_omitted_full_stops_are_recovered_from_pixels(self):
        ctx=Q.CGBitmapContextCreate(None,800,600,8,3200,Q.CGColorSpaceCreateDeviceRGB(),Q.kCGImageAlphaPremultipliedLast)
        Q.CGContextSetRGBFillColor(ctx,.98,.98,.98,1)
        Q.CGContextFillRect(ctx,Q.CGRectMake(0,0,800,600))
        Q.CGContextSetRGBFillColor(ctx,.91,.91,.92,1)
        Q.CGContextFillRect(ctx,Q.CGRectMake(300,80,120,34))
        Q.CGContextSetRGBFillColor(ctx,.12,.12,.12,1)
        for x in (335,350,365):
            Q.CGContextFillEllipseInRect(ctx,Q.CGRectMake(x,94,5,5))
        image=Q.CGBitmapContextCreateImage(ctx)
        messages = [p.Message('旧消息', 'them', .30, 1, h=.03, x=.40, w=.12,
                              last_y=.30)]
        p._append_unreadable_incoming(image, messages, .30, .90)
        self.assertEqual(messages[-1].text, '。。。')
        self.assertFalse(messages[-1].visual_only)

    def test_fingerprint_ignores_draft_below_actual_boundary(self):
        self.assertEqual(p._fingerprint(self.canvas(), .6), p._fingerprint(self.canvas(True), .6))
        self.assertNotEqual(p._fingerprint(self.canvas()), p._fingerprint(self.canvas(True)))

    def test_missing_boundary_returns_no_messages(self):
        win=p.WindowInfo(wid=1,pid=1,title='微信',x=0,y=0,w=800,h=600)
        with patch.object(p,'find_wechat_window',return_value=win), \
             patch.object(p,'capture_window',side_effect=lambda _wid, out: (out.write_bytes(b'png') or True)), \
             patch.object(p.Quartz,'CGImageSourceCreateWithData',return_value=object()), \
             patch.object(p.Quartz,'CGImageSourceCreateImageAtIndex',return_value=self.canvas()), \
             patch('input_region.input_outline',return_value=None), \
             patch('fill.locate_input',return_value={'rect':None}), \
             patch.object(p,'ocr_image',return_value=[block('草稿', .65)]):
            result=p.read_conversation()
        self.assertTrue(result['input_unresolved'])
        self.assertEqual(result['messages'], [])

    def test_layout_change_forces_new_ocr_even_with_same_fingerprint(self):
        win=p.WindowInfo(wid=1,pid=1,title='微信',x=0,y=0,w=800,h=600)
        with patch.object(p,'find_wechat_window',return_value=win), \
             patch.object(p,'capture_window',side_effect=lambda _wid, out: (out.write_bytes(b'png') or True)), \
             patch.object(p.Quartz,'CGImageSourceCreateWithData',return_value=object()), \
             patch.object(p.Quartz,'CGImageSourceCreateImageAtIndex',return_value=self.canvas()), \
             patch('input_region.input_outline',return_value=(.32,.6,.65,.39)), \
             patch.object(p,'_fingerprint',return_value=b'x'*100), \
             patch.object(p,'ocr_image',return_value=[block('消息', .5)]) as ocr:
            result=p.read_conversation(prev_fingerprint=b'x'*100,prev_layout=(1,800,600,.75,.305))
            self.assertFalse(result['unchanged'])
            self.assertAlmostEqual(result['chat_left'], .305)
            ocr.assert_called_once()
            result=p.read_conversation(prev_fingerprint=b'x'*100,prev_layout=(1,800,600,.6,.305))
            self.assertTrue(result['unchanged'])
            self.assertEqual(ocr.call_count,1)
