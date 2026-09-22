"""One-click 「填入」: put a candidate reply into WeChat's input box.

The AX path below remains preferred. When WeChat exposes no input control, an
explicit Fill click can use visual_fill's checked keyboard fallback. It does not
use the clipboard or Return; uncertain readback is reported, never retried.

Preferred mechanism — the Accessibility API:
    find WeChat's input box in the accessibility tree (the AXTextArea inside the window
    titled 微信), write the text into it, then read it back and only report success if the
    text is verifiably there.

Why not the obvious pasteboard + synthesized Cmd+V, which is what this file used to do:
  * a paste only reaches the *frontmost* app, so WeChat has to be brought forward first —
    and a non-active accessory app cannot reliably do that on current macOS (measured:
    NSRunningApplication activation and AXFrontmost both report success while the app stays
    inactive). When that step fails, the keystroke lands in whatever app IS frontmost, i.e.
    the reply gets typed into someone else's window;
  * it overwrites the general pasteboard, destroying whatever the user had copied;
  * "the events were posted" is not "the text arrived", so the result could not be checked
    — the panel would claim 已填入 over an input box that was still empty, and the user,
    seeing nothing, would click again.
The accessibility write avoids all three: it needs no frontmost window, never touches the
clipboard, and can be read back to prove it worked.

macOS only lets a process touch another app's accessibility tree when that process holds
the Accessibility permission (System Settings -> Privacy & Security -> Accessibility).
Without it the AX calls fail, which is why fill_text() checks first and returns
(False, "未授予辅助功能权限") rather than pretending it worked. request_accessibility()
triggers the system dialog that grants it.

Standalone use (handy for granting the permission before the HUD ever needs it):
    uv run python src/fill.py             # report permission + WeChat process state only
    uv run python src/fill.py "好的，马上"  # actually fill (needs the grant)
"""

from __future__ import annotations

import threading
import time

import AppKit
import ApplicationServices

WECHAT_BUNDLE_ID = "com.tencent.xinWeChat"
WECHAT_NAMES = ("微信", "WeChat")

# WeChat owns two windows: a small untitled one and the chat window. The input box lives in
# the latter, so it is searched first and the untitled window is only a fallback.
CHAT_WINDOW_TITLE = "微信"
INPUT_ROLE = "AXTextArea"
# The chat window holds TWO text areas, and picking the wrong one writes the reply into
# WeChat's sidebar search field (measured: search 127x23, message box 643x129). Size is the
# only thing that separates them reliably, so the largest wins and anything smaller than
# this floor is refused outright rather than guessed at — the two differ by ~24x, so the
# floor is not a close call. (Area is in points^2.)
MIN_INPUT_AREA = 10000.0
# One node per visible message means a busy chat window is a large tree; this ceiling keeps
# a miss cheap instead of walking thousands of nodes.
MAX_NODES = 2000

# Reason strings are shown by the HUD in its status line, so they read as sentences.
REASON_EMPTY = "没有可填入的内容"
REASON_NO_ACCESS = "未授予辅助功能权限"
REASON_NO_WECHAT = "没找到微信应用"
REASON_NO_INPUT = "未取得可用的微信输入控件"
REASON_WRITE_FAILED = "写入输入框失败"
REASON_NOT_VERIFIED = "写入后没读到内容，可能没填进去"
REASON_BUSY = "上一次填入还没结束"
REASON_DUPLICATE = "刚填入过同样的内容，已忽略这次重复点击"


def has_accessibility() -> bool:
    """True when this process may read and drive other apps' accessibility trees."""
    try:
        return bool(ApplicationServices.AXIsProcessTrusted())
    except Exception:
        return False


def request_accessibility() -> bool:
    """Ask macOS to show the Accessibility grant dialog (no-op if already granted).

    Returns the permission state before the user answers — the dialog is asynchronous,
    so a False here means "the user still has to flip the switch".
    """
    try:
        opts = {ApplicationServices.kAXTrustedCheckOptionPrompt: True}
        return bool(ApplicationServices.AXIsProcessTrustedWithOptions(opts))
    except Exception:
        return has_accessibility()


def _wechat_app():
    """The running WeChat: bundle id first, window-owner name as the fallback."""
    try:
        apps = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(
            WECHAT_BUNDLE_ID)
        if apps and len(apps) > 0:
            return apps[0]
    except Exception:
        pass
    try:
        for app in AppKit.NSWorkspace.sharedWorkspace().runningApplications():
            name = app.localizedName() or ""
            if name in WECHAT_NAMES or app.bundleIdentifier() == WECHAT_BUNDLE_ID:
                return app
    except Exception:
        pass
    return None


def _ax_attr(element, name):
    """One accessibility attribute, or None. Never raises — AX reads fail routinely."""
    try:
        err, value = ApplicationServices.AXUIElementCopyAttributeValue(element, name, None)
        return value if err == 0 else None
    except Exception:
        return None


def _ax_size(element) -> tuple[float, float] | None:
    """(width, height) of an element, or None when it cannot be read."""
    try:
        raw = _ax_attr(element, ApplicationServices.kAXSizeAttribute)
        if raw is None:
            return None
        ok, size = ApplicationServices.AXValueGetValue(
            raw, ApplicationServices.kAXValueCGSizeType, None)
        return (float(size.width), float(size.height)) if ok else None
    except Exception:
        return None


def _ax_rect(element):
    size = _ax_size(element)
    raw = _ax_attr(element, ApplicationServices.kAXPositionAttribute)
    if not size or raw is None:
        return None
    try:
        ok, point = ApplicationServices.AXValueGetValue(
            raw, ApplicationServices.kAXValueCGPointType, None)
        return (float(point.x), float(point.y), *size) if ok else None
    except Exception:
        return None


def _same_rect(a, b):
    return a is not None and b is not None and all(abs(x-y) <= 3 for x, y in zip(a, b))


def locate_input(win):
    """Read-only target shared by the overlay and Fill; never request permission here."""
    result = {"box": None, "rect": None, "window": win, "reason": REASON_NO_INPUT}
    if not has_accessibility():
        result["reason"] = REASON_NO_ACCESS
        return result
    app = _wechat_app()
    if app is None:
        result["reason"] = REASON_NO_WECHAT
        return result
    bounds = tuple(win[k] for k in ("x", "y", "w", "h"))
    box = _find_input_box(app.processIdentifier(), bounds)
    if box is None:
        return result
    rect = _ax_rect(box)
    if rect is None:
        result["reason"] = "输入控件坐标不可读取"
        return result
    x, y, w, h = rect
    wx, wy, ww, wh = bounds
    if not (wx <= x and wy <= y and x+w <= wx+ww+3 and y+h <= wy+wh+3):
        result["reason"] = "输入控件不在当前微信窗口内"
        return result
    result.update(box=box, rect=rect, reason="填入目标")
    if _ax_value(box) is None:
        result["reason"] = "输入控件已定位，文字不可读取"
    return result


def _find_input_box(pid: int, window_rect=None):
    """WeChat's message input box, or None.

    Returns the LARGEST text area in the chat window, not the first one found: WeChat's
    accessibility tree contains both the sidebar search field and the message box, and the
    search field sits shallower. A first-match walk therefore finds the search field and
    writes the reply into it — a bug that a naive read-back check cannot catch, because
    writing and reading both go through the same wrong element and agree with each other.

    The walk is breadth-first and bounded: a busy chat window carries a node per visible
    message.
    """
    app_el = ApplicationServices.AXUIElementCreateApplication(pid)
    windows = _ax_attr(app_el, ApplicationServices.kAXWindowsAttribute) or []
    if not windows:
        return None
    # the chat window first; sorted() is stable, so the fallback keeps its own order
    ordered = sorted(
        windows,
        key=lambda w: _ax_attr(w, ApplicationServices.kAXTitleAttribute)
        != CHAT_WINDOW_TITLE)

    best, best_area = None, 0.0
    for window in ordered:
        if window_rect is not None and not _same_rect(_ax_rect(window), window_rect):
            continue
        queue, seen = [window], 0
        while queue and seen < MAX_NODES:
            el = queue.pop(0)
            seen += 1
            if _ax_attr(el, ApplicationServices.kAXRoleAttribute) == INPUT_ROLE:
                size = _ax_size(el)
                area = size[0] * size[1] if size else 0.0
                if area > best_area:
                    best, best_area = el, area
            queue.extend(_ax_attr(el, ApplicationServices.kAXChildrenAttribute) or [])

    if best is None or best_area < MIN_INPUT_AREA:
        return None
    return best


def _ax_value(box) -> str | None:
    """The input box's current text, or None when it cannot be read as a string."""
    value = _ax_attr(box, ApplicationServices.kAXValueAttribute)
    return value if isinstance(value, str) else None


def _ax_set_value(box, text: str) -> bool:
    """Write text into the box. False means the AX call refused or raised."""
    try:
        err = ApplicationServices.AXUIElementSetAttributeValue(
            box, ApplicationServices.kAXValueAttribute, text)
        return err == 0
    except Exception:
        return False


_FILL_LOCK = threading.Lock()
_LAST_FILL: tuple[str, str, float] | None = None   # (text, box content after fill, ts)
# Short on purpose: it only has to swallow a double click. The content check below is what
# decides, so a deliberate retry a moment later always goes through.
DUPLICATE_WINDOW_S = 0.4


def _duplicate_blocked(text: str, current: str,
                       last: tuple[str, str, float] | None, now: float) -> bool:
    """True when this call is the second half of one double click.

    Keyed on the input box's *content* rather than the clock alone: the same text is
    refused only while the box still holds exactly what our last fill left in it (so nobody
    removed the text in between) and only inside DUPLICATE_WINDOW_S. Filling appends, so
    without this a double click would put the reply in the box twice.

    Comparing `current` — not the text we are about to write — is what makes this work:
    every successful fill grows the box, so a comparison against the new value could never
    match, and the guard would silently never fire.
    Taking every input as an argument keeps the rule testable without a live WeChat.
    """
    if last is None:
        return False
    last_text, last_content, last_ts = last
    return (text == last_text
            and current == last_content
            and (now - last_ts) < DUPLICATE_WINDOW_S)


def fill_text(text: str, target=None) -> tuple[bool, str]:
    """Write `text` into WeChat's input box, appended to whatever is already typed there.

    Returns (ok, reason). Appending keeps this equivalent to the paste it replaces: a paste
    lands at the caret, which is the end of the box once the user has been typing. Success
    is never reported without reading the text back.
    """
    global _LAST_FILL
    text = (text or "").strip()
    if not text:
        return False, REASON_EMPTY
    if not _FILL_LOCK.acquire(blocking=False):
        # a fill is still running; a second write now would double the text
        return False, REASON_BUSY
    try:
        if not has_accessibility():
            return False, REASON_NO_ACCESS

        app = _wechat_app()
        if app is None:
            return False, REASON_NO_WECHAT

        if target is not None and target['box'] is None and target.get('visual_rect'):
            from visual_fill import write_text
            try:
                return write_text(text, target, app)
            except Exception:
                return False, '输入过程异常，请先检查草稿，勿重复点击'
        if target is not None:
            fresh = locate_input(target["window"])
            box = fresh["box"]
            if (box is None or target["box"] is None or box != target["box"]
                    or not _same_rect(fresh["rect"], target["rect"])):
                return False, "输入目标已变化，请等检测框更新后重试"
        else:
            box = _find_input_box(app.processIdentifier())
        if box is None:
            return False, REASON_NO_INPUT

        current = _ax_value(box)
        if current is None:
            return False, REASON_NO_INPUT

        # checked before the write, so a refused repeat leaves the box untouched
        if _duplicate_blocked(text, current, _LAST_FILL, time.monotonic()):
            return False, REASON_DUPLICATE

        if not _ax_set_value(box, current + text):
            return False, REASON_WRITE_FAILED

        # read it back: a write that reports success but never appears is exactly the silent
        # failure this mechanism replaced, so it is never assumed away
        landed = _ax_value(box)
        if landed is None or not landed.endswith(text):
            return False, REASON_NOT_VERIFIED
        _LAST_FILL = (text, landed, time.monotonic())
        return True, "已填入"
    finally:
        _FILL_LOCK.release()


if __name__ == "__main__":
    import sys

    print(f"辅助功能权限: {'已授予' if has_accessibility() else '未授予'}")
    _app = _wechat_app()
    if _app is None:
        print("微信进程: 未找到")
    else:
        print(f"微信进程: {_app.localizedName()} ({_app.bundleIdentifier()})")
        if has_accessibility():
            _box = _find_input_box(_app.processIdentifier())
            print(f"输入框: {'已找到（可以填入）' if _box is not None else '没找到'}")
    if len(sys.argv) > 1:
        print(f"填入结果: {fill_text(sys.argv[1])}")
