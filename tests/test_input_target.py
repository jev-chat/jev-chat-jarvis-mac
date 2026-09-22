import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import fill

class InputTargetTests(unittest.TestCase):
    def test_coordinates_tolerate_rounding_not_resize(self):
        self.assertTrue(fill._same_rect((0, 25, 800, 600), (1, 25, 800, 601)))
        self.assertFalse(fill._same_rect((0, 25, 800, 600), (0, 25, 900, 600)))

    def test_no_permission_does_not_traverse(self):
        with patch.object(fill, 'has_accessibility', return_value=False), patch.object(fill, '_find_input_box') as locate:
            result = fill.locate_input({'x': 0, 'y': 25, 'w': 800, 'h': 600})
            self.assertIsNone(result['rect'])
            self.assertEqual(result['reason'], fill.REASON_NO_ACCESS)
            locate.assert_not_called()

    def test_changed_target_never_writes(self):
        old = {'window': {}, 'box': 'old', 'rect': (0, 400, 600, 100)}
        new = dict(old, box='new')
        with patch.object(fill, 'has_accessibility', return_value=True), patch.object(fill, '_wechat_app', return_value=Mock()), patch.object(fill, 'locate_input', return_value=new), patch.object(fill, '_ax_set_value') as write:
            ok, reason = fill.fill_text('test', target=old)
            self.assertFalse(ok)
            self.assertIn('目标已变化', reason)
            write.assert_not_called()

    def test_same_target_appends_and_verifies(self):
        target = {'window': {}, 'box': 'box', 'rect': (0, 400, 600, 100)}
        with patch.object(fill, 'has_accessibility', return_value=True), patch.object(fill, '_wechat_app', return_value=Mock()), patch.object(fill, 'locate_input', return_value=target), patch.object(fill, '_ax_value', side_effect=['draft', 'drafttest']), patch.object(fill, '_ax_set_value', return_value=True) as write, patch.object(fill, '_LAST_FILL', None):
            self.assertEqual(fill.fill_text('test', target=target), (True, '已填入'))
            write.assert_called_once_with('box', 'drafttest')
