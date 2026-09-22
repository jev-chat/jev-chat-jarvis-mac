"""Drafts never enter message/context extraction; use synthetic OCR and images."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import Quartz as Q
import perception as p


def block(text, top, height=.025, x=.4, width=.15):
    return p.TextBlock(text, 1, x, 1-top-height, width, height)


class DraftExclusionTests(unittest.TestCase):
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
                block('明白收到', .4+.035*scale, .025*scale, x=.412),
                block('第二行正文', .4+.065*scale, .025*scale, x=.412)], input_top=.85)
            self.assertEqual(messages[0].sender, '测试成员-A')
            self.assertEqual(messages[0].text, '明白收到\n第二行正文')

    def test_equal_sized_short_body_is_not_a_sender(self):
        messages = p.extract_messages([block('明白', .4), block('收到', .43)], input_top=.8)
        self.assertIsNone(messages[0].sender)
        self.assertEqual(messages[0].text, '明白\n收到')

    def canvas(self, draft=False):
        ctx=Q.CGBitmapContextCreate(None,800,600,8,3200,Q.CGColorSpaceCreateDeviceRGB(),Q.kCGImageAlphaPremultipliedLast)
        Q.CGContextSetRGBFillColor(ctx,1,1,1,1)
        Q.CGContextFillRect(ctx,Q.CGRectMake(0,0,800,600))
        if draft:
            Q.CGContextSetRGBFillColor(ctx,0,0,0,1)
            Q.CGContextFillRect(ctx,Q.CGRectMake(350,150,100,20))
        return Q.CGBitmapContextCreateImage(ctx)

    def test_fingerprint_ignores_draft_below_actual_boundary(self):
        self.assertEqual(p._fingerprint(self.canvas(), .6), p._fingerprint(self.canvas(True), .6))
        self.assertNotEqual(p._fingerprint(self.canvas()), p._fingerprint(self.canvas(True)))

    def test_missing_boundary_returns_no_messages(self):
        win=p.WindowInfo(wid=1,pid=1,title='微信',x=0,y=0,w=800,h=600)
        with patch.object(p,'find_wechat_window',return_value=win), \
             patch.object(p,'capture_image',return_value=self.canvas()), \
             patch('input_region.input_outline',return_value=None), \
             patch('fill.locate_input',return_value={'rect':None}), \
             patch.object(p,'ocr_image',return_value=[block('草稿', .65)]):
            result=p.read_conversation()
        self.assertTrue(result['input_unresolved'])
        self.assertEqual(result['messages'], [])

    def test_layout_change_forces_new_ocr_even_with_same_fingerprint(self):
        win=p.WindowInfo(wid=1,pid=1,title='微信',x=0,y=0,w=800,h=600)
        with patch.object(p,'find_wechat_window',return_value=win), \
             patch.object(p,'capture_image',return_value=self.canvas()), \
             patch('input_region.input_outline',return_value=(.32,.6,.65,.39)), \
             patch.object(p,'_fingerprint',return_value=b'x'*100), \
             patch.object(p,'ocr_image',return_value=[block('消息', .5)]) as ocr:
            result=p.read_conversation(prev_fingerprint=b'x'*100,prev_layout=(1,800,600,.75))
            self.assertFalse(result['unchanged'])
            ocr.assert_called_once()
            result=p.read_conversation(prev_fingerprint=b'x'*100,prev_layout=(1,800,600,.6))
            self.assertTrue(result['unchanged'])
            self.assertEqual(ocr.call_count,1)
