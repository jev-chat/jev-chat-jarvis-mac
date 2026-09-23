"""Pure geometry checks for the resizable HUD."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hud import (CAND_BTN_GAP, CAND_BTN_W, PANEL_MIN_W, _candidate_geometry,
                 _responsive_xw, _viewport_fit)


class ResponsiveLayoutTests(unittest.TestCase):
    def test_candidate_text_receives_added_width(self):
        narrow = _candidate_geometry(PANEL_MIN_W)
        wide = _candidate_geometry(640)
        self.assertGreater(wide["text_w"], narrow["text_w"])
        self.assertAlmostEqual(wide["text_w"] - narrow["text_w"],
                               640 - PANEL_MIN_W)

    def test_candidate_actions_stay_inside_every_supported_width(self):
        for width in (PANEL_MIN_W, 360, 480, 720):
            with self.subTest(width=width):
                layout = _candidate_geometry(width)
                right = layout["button_x"] + 2 * CAND_BTN_W + CAND_BTN_GAP
                self.assertLessEqual(right, width - 26)
                self.assertGreater(layout["text_w"], 80)
                self.assertEqual(layout["row_w"], width - 40)

    def test_stretch_and_summary_segments_follow_width(self):
        for width in (PANEL_MIN_W, 360, 600):
            surface_x, surface_w = _responsive_xw(
                ("stretch", 14, 14), width, 14, 332)
            intent_x, intent_w = _responsive_xw(
                ("segment1", 26, 8), width, 26, 116)
            risk_x, risk_w = _responsive_xw(
                ("segment2", 14, 4), width, 164, 92)
            level_x, level_w = _responsive_xw(
                ("segment3", 12, 12), width, 272, 64)
            self.assertEqual((surface_x, surface_w), (14, width - 28))
            self.assertLess(intent_x + intent_w, risk_x)
            self.assertLess(risk_x + risk_w, level_x)
            self.assertLessEqual(level_x + level_w, width - 12)

    def test_tall_viewport_expands_read_result_before_leaving_empty_space(self):
        extra, content_h, scrolls = _viewport_fit(600, 760, 42, 170, True)
        self.assertEqual(extra, 128)
        self.assertEqual(content_h, 760)
        self.assertFalse(scrolls)

    def test_short_viewport_keeps_full_document_scrollable(self):
        extra, content_h, scrolls = _viewport_fit(600, 300, 42, 170, True)
        self.assertEqual(extra, 0)
        self.assertEqual(content_h, 600)
        self.assertTrue(scrolls)

    def test_initial_window_remains_content_driven(self):
        extra, content_h, scrolls = _viewport_fit(600, 760, 42, 170, False)
        self.assertEqual((extra, content_h, scrolls), (0, 600, False))


if __name__ == "__main__":
    unittest.main()
