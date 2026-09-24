import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import Quartz as Q
from input_region import input_outline

class VisualRegionTests(unittest.TestCase):
    def canvas(self, left=None, open_bottom=False, distractor=False, right=790,
               left_divider=True):
        ctx=Q.CGBitmapContextCreate(None,800,600,8,3200,Q.CGColorSpaceCreateDeviceRGB(),Q.kCGImageAlphaPremultipliedLast)
        Q.CGContextSetRGBFillColor(ctx,.98,.98,.98,1)
        Q.CGContextFillRect(ctx,Q.CGRectMake(0,0,800,600))
        if left is not None:
            Q.CGContextSetRGBStrokeColor(ctx,.65,.65,.65,1)
            Q.CGContextSetLineWidth(ctx,2)
            if open_bottom:
                # WeChat 4.x ends the composer at the window edge: there is a top
                # separator and a left divider, but no visible bottom border.
                Q.CGContextMoveToPoint(ctx,left,150)
                Q.CGContextAddLineToPoint(ctx,right,150)
                if left_divider:
                    Q.CGContextMoveToPoint(ctx,left,0)
                    Q.CGContextAddLineToPoint(ctx,left,150)
                Q.CGContextStrokePath(ctx)
            else:
                Q.CGContextStrokeRect(ctx,Q.CGRectMake(left,10,right-left,140))
        if distractor:
            # A wide quoted card in the chat pane. It is longer than the resized
            # composer below, but neither starts at the pane boundary nor reaches the
            # window edge, so it must never become the OCR cutoff.
            Q.CGContextSetRGBStrokeColor(ctx,.55,.55,.55,1)
            Q.CGContextSetLineWidth(ctx,2)
            Q.CGContextStrokeRect(ctx,Q.CGRectMake(90,260,610,90))
        return Q.CGBitmapContextCreateImage(ctx)

    def test_blank_image_has_no_fake_target(self):
        self.assertIsNone(input_outline(self.canvas()))

    def test_sidebar_width_is_measured(self):
        for left in (160,280):
            with self.subTest(left=left):
                rect=input_outline(self.canvas(left))
                self.assertIsNotNone(rect)
                self.assertAlmostEqual(rect[0],left/800,delta=.01)
                self.assertAlmostEqual(rect[1],.75,delta=.01)

    def test_wechat_4_composer_may_end_at_window_bottom(self):
        rect=input_outline(self.canvas(230,open_bottom=True))
        self.assertIsNotNone(rect)
        self.assertAlmostEqual(rect[0],230/800,delta=.01)
        self.assertAlmostEqual(rect[1],.75,delta=.01)
        self.assertAlmostEqual(rect[3],.25,delta=.02)

    def test_wide_chat_card_does_not_crop_newer_messages(self):
        rect=input_outline(self.canvas(280,distractor=True))
        self.assertIsNotNone(rect)
        self.assertAlmostEqual(rect[0],280/800,delta=.01)
        self.assertAlmostEqual(rect[1],.75,delta=.01)

    def test_shorter_composer_separator_with_bottom_border(self):
        rect=input_outline(self.canvas(230,right=700))
        self.assertIsNotNone(rect)
        self.assertAlmostEqual(rect[1],.75,delta=.01)

    def test_shorter_open_bottom_separator_with_left_divider(self):
        rect=input_outline(self.canvas(230,open_bottom=True,right=700))
        self.assertIsNotNone(rect)
        self.assertAlmostEqual(rect[1],.75,delta=.01)

    def test_unsupported_horizontal_rule_is_not_input(self):
        self.assertIsNone(input_outline(self.canvas(distractor=True)))

    def test_edge_to_edge_horizontal_rule_without_divider_is_not_input(self):
        self.assertIsNone(input_outline(self.canvas(
            230,open_bottom=True,left_divider=False)))

    def test_composer_without_sidebar_still_has_boundary(self):
        rect=input_outline(self.canvas(8,open_bottom=True,right=700))
        self.assertIsNotNone(rect)
        self.assertAlmostEqual(rect[1],.75,delta=.01)
