import sys
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import visual_fill

class VisualFillTests(unittest.TestCase):
    def test_control_characters_cannot_send_or_switch_fields(self):
        self.assertEqual(visual_fill.plain_text('你好\r\nworld\t!\x00'), '你好  world !')

    def test_changed_window_never_posts_input(self):
        with patch.object(visual_fill,'window_is_current',return_value=False), patch.object(visual_fill.Q,'CGEventPost') as post:
            ok,_=visual_fill.write_text('hello',{'window':{}},Mock())
            self.assertFalse(ok)
            post.assert_not_called()

    def test_changed_rectangle_never_posts_input(self):
        with patch.object(visual_fill,'window_is_current',return_value=True), patch.object(visual_fill,'locate_visual_input',return_value=(1,2,300,200)), patch.object(visual_fill.Q,'CGEventPost') as post:
            ok,_=visual_fill.write_text('hello',{'window':{},'visual_rect':(200,200,300,200)},Mock())
            self.assertFalse(ok)
            post.assert_not_called()

    def test_header_check_ignores_small_rendering_noise(self):
        self.assertTrue(visual_fill.same_signature(bytes([200]*100),bytes([201]*100)))
        self.assertFalse(visual_fill.same_signature(bytes([200]*100),bytes([100]*100)))
        self.assertFalse(visual_fill.same_signature(None,None))

    def prepare(self, before='', after='hello', current=True):
        from contextlib import ExitStack
        stack=ExitStack()
        self.addCleanup(stack.close)
        def mock(name, **kw):
            return stack.enter_context(patch.object(visual_fill,name,**kw))
        mock('window_is_current', return_value=current)
        mock('locate_visual_input', return_value=(0,100,600,200))
        mock('chat_signature', return_value=b'header')
        mock('input_text', side_effect=[before,after])
        mock('_LAST_ATTEMPT', new=None)
        stack.enter_context(patch.object(visual_fill.time,'sleep'))
        post=stack.enter_context(patch.object(visual_fill.Q,'CGEventPost'))
        target={'window':{'wid':7},'visual_rect':(0,100,600,200),'chat_signature':b'header'}
        return target,post

    def test_existing_draft_stops_before_mouse_or_keyboard(self):
        target,post=self.prepare(before='existing draft')
        ok,reason=visual_fill.write_text('hello',target,Mock())
        self.assertFalse(ok)
        self.assertIn('已有草稿',reason)
        post.assert_not_called()

    def test_changed_chat_stops_before_input(self):
        target,post=self.prepare()
        target['chat_signature']=b'another header'
        ok,reason=visual_fill.write_text('hello',target,Mock())
        self.assertFalse(ok)
        self.assertIn('会话已变化',reason)
        post.assert_not_called()

    def test_focus_loss_stops_before_mouse_click(self):
        target,post=self.prepare()
        with patch.object(visual_fill,'window_is_current',side_effect=[True,False]):
            ok,_=visual_fill.write_text('hello',target,Mock())
        self.assertFalse(ok)
        post.assert_not_called()

    def test_verified_fill_and_repeat_protection(self):
        target,post=self.prepare()
        app=Mock()
        app.processIdentifier.return_value=10
        ok,reason=visual_fill.write_text('hello',target,app)
        self.assertTrue(ok)
        self.assertIn('未发送',reason)
        count=post.call_count
        ok,reason=visual_fill.write_text('hello',target,app)
        self.assertFalse(ok)
        self.assertIn('避免重复',reason)
        self.assertEqual(post.call_count,count)

    def test_uncertain_readback_is_not_success(self):
        target,post=self.prepare(after='unrelated')
        app=Mock()
        app.processIdentifier.return_value=10
        ok,reason=visual_fill.write_text('hello',target,app)
        self.assertFalse(ok)
        self.assertIn('未能确认',reason)
        self.assertTrue(post.called)

    def test_all_line_separators_are_flattened(self):
        self.assertEqual(visual_fill.plain_text('a\u2028b\u2029c\x85d'),'a b c d')
