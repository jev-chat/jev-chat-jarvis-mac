import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import Quartz as Q
from input_region import input_outline

class VisualRegionTests(unittest.TestCase):
    def canvas(self, left=None):
        ctx=Q.CGBitmapContextCreate(None,800,600,8,3200,Q.CGColorSpaceCreateDeviceRGB(),Q.kCGImageAlphaPremultipliedLast)
        Q.CGContextSetRGBFillColor(ctx,.98,.98,.98,1)
        Q.CGContextFillRect(ctx,Q.CGRectMake(0,0,800,600))
        if left is not None:
            Q.CGContextSetRGBStrokeColor(ctx,.65,.65,.65,1)
            Q.CGContextSetLineWidth(ctx,2)
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
