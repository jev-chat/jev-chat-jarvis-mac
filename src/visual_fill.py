"""Explicit-click fallback for WeChat versions without an AX text control.

Never sends Return, never touches the clipboard, and never clears existing drafts.
Only used after the user clicks Fill, with a fresh visual target and window checks.
"""
import time
import unicodedata
import AppKit
import ApplicationServices as A
import Quartz as Q
from input_region import locate_visual_input

_LAST_ATTEMPT = None


def plain_text(text):
    # No control characters can become Enter/Tab shortcuts. Preserve visible text.
    return ''.join(' ' if c in '\r\n\t\u0085\u2028\u2029' else c for c in text
                   if c in '\r\n\t\u0085\u2028\u2029' or unicodedata.category(c) not in ('Cc', 'Cs'))


def window_is_current(win, app, require_front=False):
    import fill
    from perception import find_wechat_window
    current = find_wechat_window(win['wid'])
    if current is None or current.wid != win['wid'] or current.pid != app.processIdentifier():
        return False
    if not fill._same_rect(tuple(getattr(current,k) for k in ('x','y','w','h')),
                           tuple(win[k] for k in ('x','y','w','h'))):
        return False
    if require_front:
        front=AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if front is None or front.processIdentifier()!=app.processIdentifier():
            return False
        root=A.AXUIElementCreateApplication(app.processIdentifier())
        focused=fill._ax_attr(root,A.kAXFocusedWindowAttribute)
        if focused is None or not fill._same_rect(fill._ax_rect(focused),tuple(win[k] for k in ('x','y','w','h'))):
            return False
    return True


def chat_signature(win, rect):
    from perception import capture_image
    import ctypes
    image=capture_image(win['wid'])
    if image is None:
        return None
    iw,ih=Q.CGImageGetWidth(image),Q.CGImageGetHeight(image)
    crop=Q.CGImageCreateWithImageInRect(image,Q.CGRectMake(
        (rect[0]-win['x'])*iw/win['w'],0,rect[2]*iw/win['w'],min(55,win['h'])*ih/win['h']))
    buf=ctypes.create_string_buffer(256*24)
    ctx=Q.CGBitmapContextCreate(buf,256,24,8,256,Q.CGColorSpaceCreateDeviceGray(),Q.kCGImageAlphaNone)
    Q.CGContextDrawImage(ctx,Q.CGRectMake(0,0,256,24),crop)
    return buf.raw


def same_signature(a, b):
    return a is not None and b is not None and len(a)==len(b) and sum(abs(x-y)>=12 for x,y in zip(a,b))<8


def input_text(win, rect):
    from perception import capture_image, ocr_image
    image=capture_image(win['wid'])
    if image is None:
        return None
    iw, ih=Q.CGImageGetWidth(image),Q.CGImageGetHeight(image)
    x,y,w,h=rect
    crop=Q.CGImageCreateWithImageInRect(image,Q.CGRectMake(
        (x-win['x'])*iw/win['w'],(y-win['y'])*ih/win['h'],w*iw/win['w'],h*ih/win['h']))
    blocks=ocr_image(crop,chat_only=False)
    # Exclude the toolbar at the very bottom, but retain the entire draft above it.
    blocks=[b for b in blocks if b.y > .18]
    blocks.sort(key=lambda b:(-round(b.y,2),b.x))
    return ''.join(b.text for b in blocks if b.text.replace(' ', '') not in ('按住鼠标语音输入文字','发送'))


def _key(pid, code, flags=0):
    for down in (True,False):
        event=Q.CGEventCreateKeyboardEvent(None,code,down)
        Q.CGEventSetFlags(event,flags)
        Q.CGEventPost(Q.kCGHIDEventTap,event)


def write_text(text, target, app):
    import fill
    global _LAST_ATTEMPT
    win=target['window']
    if not window_is_current(win,app):
        return False,'微信窗口已变化，请等检测框更新后重试'
    rect=locate_visual_input(win)
    if not fill._same_rect(rect,target.get('visual_rect')):
        return False,'输入区已变化，请等检测框更新后重试'
    signature=chat_signature(win,rect)
    if not same_signature(signature,target.get('chat_signature')):
        return False,'会话已变化，请等待识别更新后重试'
    text=plain_text(text)
    if not text.strip():
        return False,'没有可填入的内容'
    stamp=(win['wid'],text)
    if _LAST_ATTEMPT and _LAST_ATTEMPT[0]==stamp and time.monotonic()-_LAST_ATTEMPT[1]<3:
        return False,'刚尝试过填入，请先检查输入框，避免重复'
    before=input_text(win,rect)
    if before is None:
        return False,'无法读取输入区，已停止填入'
    if before.strip():
        return False, '输入区已有草稿；请使用复制手动插入，避免改动现有内容'
    # A physical click raises the target window; never type on activation intent alone.
    x,y,w,h=rect
    point=(x+min(80,w/4),y+min(h*.5,max(20,h-60)))
    app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
    time.sleep(.15)
    if not window_is_current(win,app,require_front=True):
        return False,'微信没有获得焦点，请先点微信输入区再重试'
    for event_type in (Q.kCGEventLeftMouseDown,Q.kCGEventLeftMouseUp):
        event=Q.CGEventCreateMouseEvent(None,event_type,point,Q.kCGMouseButtonLeft)
        Q.CGEventSetFlags(event, 0)
        Q.CGEventSetIntegerValueField(event, Q.kCGMouseEventClickState, 1)
        Q.CGEventPost(Q.kCGHIDEventTap,event)
    time.sleep(.15)
    if not window_is_current(win,app,require_front=True):
        return False,'焦点发生变化，已停止填入'
    if not same_signature(chat_signature(win,rect),signature):
        return False,'会话已变化，已停止填入'
    _LAST_ATTEMPT=(stamp,time.monotonic())
    for offset in range(0,len(text),20):
        if (not window_is_current(win,app,require_front=True)
                or not same_signature(chat_signature(win,rect),signature)):
            return False,'窗口或会话变化，输入已中止；请检查草稿，勿重复点击'
        chunk=text[offset:offset+20]
        for down in (True,False):
            event=Q.CGEventCreateKeyboardEvent(None,0,down)
            Q.CGEventSetFlags(event,0)
            Q.CGEventKeyboardSetUnicodeString(event,len(chunk.encode('utf-16-le'))//2,chunk)
            Q.CGEventPost(Q.kCGHIDEventTap,event)
        time.sleep(.03)
    time.sleep(.25)
    after=input_text(win,rect)
    normalize=lambda s: ''.join(c for c in unicodedata.normalize('NFKC',s) if not c.isspace())
    if after is not None and normalize(text) in normalize(after) and after != before:
        return True,'已填入（视觉校验，未发送）'
    return False,'已尝试输入，画面未能确认；请检查草稿，勿重复点击'
