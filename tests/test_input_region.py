import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import Quartz as Q
from input_region import input_outline

class VisualRegionTests(unittest.TestCase):
    def canvas(self, left=None, open_bottom=False):
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
                Q.CGContextAddLineToPoint(ctx,790,150)
                Q.CGContextMoveToPoint(ctx,left,0)
                Q.CGContextAddLineToPoint(ctx,left,150)
                Q.CGContextStrokePath(ctx)
            else:
                Q.CGContextStrokeRect(ctx,Q.CGRectMake(left,10,790-left,140))
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
