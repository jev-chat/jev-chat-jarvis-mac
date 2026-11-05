"""Manual calibration regression; synthetic pixels, no accounts or live WeChat."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import Quartz as Q
from calibration import Calibration
from calibrated_messages import extract
from perception import TextBlock, ocr_image
from settings_config import read_document, write_settings


def canvas(boxes):
    ctx=Q.CGBitmapContextCreate(None,600,600,8,2400,Q.CGColorSpaceCreateDeviceRGB(),Q.kCGImageAlphaPremultipliedLast)
    Q.CGContextSetRGBFillColor(ctx,.98,.98,.98,1)
    Q.CGContextFillRect(ctx,Q.CGRectMake(0,0,600,600))
    for x,y,w,h,color in boxes:
        Q.CGContextSetRGBFillColor(ctx,*color,1)
        Q.CGContextFillRect(ctx,Q.CGRectMake(x,600-y-h,w,h))
    return Q.CGBitmapContextCreateImage(ctx)


def block(text,x,y,w,h=16):
    return TextBlock(text,1,x/600,1-(y+h)/600,w/600,h/600)


class CalibrationTests(unittest.TestCase):
    def test_saved_geometry_and_window_moves(self):
        c=Calibration(600,600,120,60,450,480)
        self.assertEqual(Calibration.parse(c.serialize()),c)
        self.assertTrue(c.matches(SimpleNamespace(w=600,h=600,x=900,y=700)))
        self.assertFalse(c.matches(SimpleNamespace(w=601,h=600)))

    def test_invalid_regions(self):
        for args in [(600,600,0,0,300,200),(600,600,120,60,500,300),
                     (600,600,120,60,400,540),(600,600,float('nan'),60,100,100),
                     (600,600,120,60,450,20),(600,600,120,60,30,100)]:
            with self.subTest(args=args),self.assertRaises(ValueError):Calibration(*args)

    def test_short_input_region_is_savable(self):
        # #155: an input editor shorter than the old 60pt floor must still be savable
        from calibration import validate_input_region
        messages=Calibration(600,600,120,60,450,350)
        editor=Calibration(600,600,125,420,440,40)
        validate_input_region(messages,editor)
        self.assertEqual(Calibration.parse(editor.serialize()),editor)

    def test_message_region_minimum_still_enforced(self):
        # Only the input region's floor was relaxed; a 40pt-tall message region is
        # constructible (it may be a saved short editor) but must not pass preview.
        from calibration import validate_input_region
        messages=Calibration(600,600,120,60,450,40)
        editor=Calibration(600,600,125,120,440,100)
        with self.assertRaises(ValueError):validate_input_region(messages,editor)

    def test_short_incoming_and_outgoing_survive(self):
        image=canvas([(160,110,75,36,(.91,.91,.92)),(475,210,65,36,(.6,.94,.56))])
        msgs=extract(image,[block('嗯',170,120,16),block('好',485,220,16)],(.2,.1,.75,.8))
        self.assertEqual([(m.text,m.side) for m in msgs],[('嗯','them'),('好','me')])

    def test_adjacent_bubbles_never_merge(self):
        image=canvas([(160,110,100,34,(.91,.91,.92)),(160,148,100,34,(.91,.91,.92))])
        msgs=extract(image,[block('第一条',170,119,48),block('第二条',170,157,48)],(.2,.1,.75,.8))
        self.assertEqual([m.text for m in msgs],['第一条','第二条'])

    def test_three_lines_fold_only_within_bubble(self):
        image=canvas([(160,110,100,36,(.91,.91,.92)),(160,190,350,82,(.91,.91,.92))])
        msgs=extract(image,[block('收到',170,120,32),block('第一行',170,200,48),
                            block('第二行',170,224,48),block('第三行',170,248,48)],(.2,.1,.75,.8))
        self.assertEqual([(m.text,m.side) for m in msgs], [('收到','them'),('第一行\n第二行\n第三行','them')])

    def test_nickname_is_not_reply_target(self):
        image=canvas([(160,120,110,36,(.91,.91,.92))])
        msgs=extract(image,[block('小王',170,100,24,12),block('哎呀',170,130,32)],(.2,.1,.75,.8))
        self.assertEqual(len(msgs),1)
        self.assertEqual((msgs[0].text,msgs[0].sender),('哎呀','小王'))

    def test_unresolved_text_not_deleted_or_guessed(self):
        msgs=extract(canvas([]),[block('不知道怎么办',170,120,96)],(.2,.1,.75,.8))
        self.assertEqual([(m.text,m.side) for m in msgs],[('不知道怎么办','unknown')])

    def test_sidebar_excluded(self):
        image=canvas([(15,110,90,36,(.91,.91,.92)),(160,110,100,36,(.91,.91,.92))])
        msgs=extract(image,[block('列表摘要',25,120,64),block('消息',170,120,32)],(.2,.1,.75,.8))
        self.assertEqual([m.text for m in msgs],['消息'])

    def test_narrow_pane_uses_local_side(self):
        image=canvas([(410,110,65,36,(.91,.91,.92))])
        msgs=extract(image,[block('是的',420,120,32)],(.65,.1,.33,.8))
        self.assertEqual(msgs[0].side,'them')

    def test_dark_bubble(self):
        # Dark background built explicitly; the same relative surface logic applies.
        ctx=Q.CGBitmapContextCreate(None,600,600,8,2400,Q.CGColorSpaceCreateDeviceRGB(),Q.kCGImageAlphaPremultipliedLast)
        Q.CGContextSetRGBFillColor(ctx,.1,.1,.1,1);Q.CGContextFillRect(ctx,Q.CGRectMake(0,0,600,600))
        Q.CGContextSetRGBFillColor(ctx,.2,.2,.2,1);Q.CGContextFillRect(ctx,Q.CGRectMake(160,454,100,36))
        msgs=extract(Q.CGBitmapContextCreateImage(ctx),[block('好',170,120,16)],(.2,.1,.75,.8))
        self.assertEqual(msgs[0].side,'them')

    def test_env_preserves_comments_and_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'env';p.write_text('# 我的配置\nCUSTOM=keep\n')
            c=Calibration(600,600,120,60,450,480)
            write_settings(p,read_document(p),{'JEV_MESSAGE_REGION':c.serialize()})
            self.assertIn('# 我的配置\nCUSTOM=keep\n',p.read_text())
            self.assertEqual(p.stat().st_mode&0o777,0o600)

    def test_explicit_region_forwarded_to_vision(self):
        with patch('perception._vision_blocks',return_value=[]) as call:
            ocr_image(canvas([]),region=(.65,.2,.3,.6))
        self.assertEqual(call.call_args.args[-1],(.65,.2,.3,.6))

    def test_numeric_recovery_never_guesses_or_duplicates(self):
        from calibrated_messages import recover_numeric_bubbles
        image=canvas([(160,110,48,36,(.91,.91,.92))])
        digit=TextBlock('1',.5,.2,.3,.25,.4)
        with patch('perception.ocr_image',return_value=[digit]) as ocr:
            recovered=recover_numeric_bubbles(image,[],(.2,.1,.75,.8))
            self.assertEqual([b.text for b in recovered],['1'])
            self.assertEqual(ocr.call_count,1)
        known=[block('1',170,120,10)]
        with patch('perception.ocr_image') as ocr:
            self.assertEqual(recover_numeric_bubbles(image,known,(.2,.1,.75,.8)),known)
            ocr.assert_not_called()
        for text,confidence in [('I',1),('3',.3),('GPT3',1)]:
            with patch('perception.ocr_image',return_value=[TextBlock(text,confidence,.2,.3,.25,.4)]):
                self.assertEqual(recover_numeric_bubbles(image,[],(.2,.1,.75,.8)),[])

    def test_input_selection_must_be_below_messages(self):
        from calibration import validate_input_region
        messages=Calibration(600,600,120,60,450,350)
        editor=Calibration(600,600,125,420,440,100)
        validate_input_region(messages,editor)
        self.assertEqual(editor.screen_rect({'x':500,'y':200}),(625,620,440,100))
        for wrong in [Calibration(600,600,125,400,440,100),
                      Calibration(600,600,10,420,100,100)]:
            with self.assertRaises(ValueError):validate_input_region(messages,wrong)


class CalibrationHudTests(unittest.TestCase):
    def setUp(self):
        import test_hud_reply
        self.fixture = test_hud_reply.HudReplyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.h = self.fixture.h
        self.scope = test_hud_reply.HUD

    def test_restart_requires_confirmation_before_read(self):
        self.h._calibration_required = True
        self.h._calibration = None
        self.h._work_inner()
        self.scope['read_conversation'].assert_not_called()
        self.fixture.flush()
        self.assertIn('校准', self.h._render.call_args.args[1])

    def test_manual_empty_frame_does_not_reuse_old_messages(self):
        from perception import Message
        old = Message('旧消息','them',.3,1)
        self.h._wechat_frontmost = True
        self.h._last_full = {'messages':[old]}
        self.h._reply_key = ('旧聊天','旧消息')
        self.h._input_window = {'wid':1}
        self.h._input_next = float('inf')
        self.scope['read_conversation'].return_value = {
            'ok':True,'unchanged':False,'window':{'wid':1},'chat_title':'新聊天',
            'messages':[],'manual_calibration':True}
        self.h._work_inner()
        self.assertIsNone(self.h._reply_key)
        self.assertIsNone(self.h._prejudge_req)
        self.assertEqual(self.h._last_full['messages'],[])

    def test_unknown_blocks_never_enter_manual_context(self):
        # Unknown-side text must never reach the context store or the model context.
        # After rebasing onto the chat_context architecture (#79), that guarantee lives
        # in the tick's message filtering (filtering inside _context_text would
        # desync _observed_offset), so the test exercises the real tick path.
        from perception import Message
        self.h._wechat_frontmost = True
        unknown = Message('图片内文字', 'unknown', .3, 1)
        incoming = Message('你好', 'them', .4, 1)
        self.scope['read_conversation'].return_value = {
            'ok': True, 'unchanged': False, 'window': {'wid': 1}, 'chat_title': '新聊天',
            'messages': [unknown, incoming], 'manual_calibration': True}
        self.h._input_window = {'wid': 1}
        self.h._input_next = float('inf')
        self.h._work_inner()
        self.assertEqual([t for t, side, _ in (self.h._observed_messages or [])], ['你好'])
        self.assertNotIn('图片内文字', self.h._active_context or '')

    def test_manual_target_carries_app_stamp_and_fill_reaches_text(self):
        # #105: the fill guard rejects any target whose `app` stamp mismatches the
        # foreground adapter; the manual-calibration branch must stamp its target
        # like the AX path does, or every fill reports「输入目标属于另一应用」.
        from unittest.mock import Mock
        self.h._wechat_frontmost = True
        self.h._input_window = {'wid': 1}
        self.h._input_next = float('inf')
        self.h._input_calibration = Mock(screen_rect=Mock(return_value=(0, 400, 600, 100)))
        self.h._input_calibration_wid = 1
        self.h._calibration = Mock(screen_rect=Mock(return_value=(0, 380, 600, 120)))
        self.h._calibration_wid = 1
        self.scope['read_conversation'].return_value = {
            'ok': True, 'unchanged': False, 'window': {'wid': 1, 'x': 0, 'y': 0, 'w': 600, 'h': 400},
            'chat_title': '聊天', 'messages': [], 'manual_calibration': True}

        with patch('visual_fill.chat_signature', return_value=b'sig'):
            self.h._work_inner()

        target = self.h._input_target
        self.assertEqual(target['app'], 'wechat')
        self.assertEqual(target['reason'], '手动校准输入区')

        # End to end: a fill click with this target reaches the adapter, not the guard.
        self.h.cand_texts = ['回复']
        sender = Mock()
        sender.tag.return_value = 0
        self.h.fillCandidate_(sender)
        self.scope['FAKE_APP'].fill_text.assert_called_once_with('回复', target=target)


if __name__=='__main__':unittest.main()
