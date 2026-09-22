"""Perception layer: find WeChat's window, capture it, OCR it, extract the conversation.

Validated facts this module is built on (probed 2026-09-21 on WeChat 4.1 Mac):
  * `screencapture -l <windowid>` returns real content even when WeChat is not frontmost,
    so our HUD floating above it never pollutes the capture.
  * Vision OCR reads Simplified Chinese chat text at conf 1.00 on message bodies;
    errors are rare and confined to unusual glyphs.
  * The window layout is stable: chat list occupies x < ~0.30, chat pane x > ~0.32,
    title bar above y ~0.90, input box below y ~0.09.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import Quartz

# --- layout constants (normalized 0..1 within the window; tuned on the probe data) ---
CHAT_PANE_X_MIN = 0.32
TITLE_BAR_Y_MAX = 0.90
INPUT_AREA_Y_MIN = 0.24
SIDEBAR_X_MAX = 0.30

# --- content filters ---
TIMESTAMP_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")
UI_NOISE = (r"折叠聊天", r"共\s*\d+", r"搜索", r"发送", r"拖入文件", r"按住说话",
            r"语音输入文字", r"按住鼠标", r"按住 说话", r"输入文字",
            r"^[\w\-\u4e00-\u9fa5]{2,20}[:：].*\.\.\..*[）)]>$")  # folded-chat banner
MIN_CONF = 0.30
USERNAME_H_MAX = 0.026   # sender-name lines render smaller than bubble text
MESSAGE_H_MIN = 0.028
MIN_TEXT_LEN = 1


@dataclass
class TextBlock:
    text: str
    conf: float
    x: float
    y: float
    w: float
    h: float

    @property
    def x_right(self) -> float:
        return self.x + self.w

    @property
    def x_center(self) -> float:
        return self.x + self.w / 2


@dataclass
class Message:
    text: str
    side: str          # "them" | "me" | "unknown"
    y: float           # normalized, top-origin for readability
    conf: float
    h: float = 0.0
    sender: str | None = None
    lines: list[str] = field(default_factory=list)
    # normalized bounding box, kept spanning every folded line — the YOLO overlay draws
    # one box per message, so a 3-line message must cover all 3 lines, not its first
    x: float = 0.0
    w: float = 0.0
    last_y: float = 0.0     # top of the most recent folded line; fold bookkeeping only


@dataclass
class WindowInfo:
    wid: int
    pid: int
    title: str
    x: float
    y: float
    w: float
    h: float


# --------------------------------------------------------------------------- window

# The names WeChat reports for itself and for its main chat window on macOS. Mainland
# builds report 微信; some locales and 4.x builds report WeChat or Weixin. One list,
# matched EXACTLY, because both directions of the substring test that used to live at
# the call sites were wrong: `"WeChat" in owner or "微信" in owner` missed the Weixin
# alias entirely (a 4.x build reporting that name found no window at all, and the panel
# simply disappeared), while accepting any owner that merely *contains* 微信 — 微信读书,
# 微信输入法 and 企业微信 own titled windows that clear the 600x400 gate below, so their
# pixels could be captured and OCR'd as chat text (#50). A new alias is now one edit
# here instead of one edit per call site.
WECHAT_APP_NAMES = ("微信", "WeChat", "Weixin")


def screen_capture_ok() -> bool:
    """False when macOS has not granted Screen Recording to this app.

    Worth checking explicitly: without the grant macOS silently hides every window's
    title, so find_wechat_window() would just report "not found" and the user would see
    the panel disappear for no stated reason.
    """
    try:
        return bool(Quartz.CGPreflightScreenCaptureAccess())
    except Exception:
        return True  # pre-10.15 has no such gate


def request_screen_capture() -> bool:
    """Ask the system to show the Screen Recording prompt (once per app identity)."""
    try:
        return bool(Quartz.CGRequestScreenCaptureAccess())
    except Exception:
        return False


def frontmost_app_is_wechat() -> bool | None:
    """Whether the app currently receiving user input is WeChat.

    The window-ID capture path can read an obscured WeChat window, which is useful for
    OCR but not a safe display boundary: a global floating HUD left above Chrome looks
    as if browser text were analysed. Keep foreground ownership separate from window
    discovery so returning to WeChat can force a fresh capture instead of reusing cache.
    """
    try:
        import AppKit
        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return None
        bundle = app.bundleIdentifier() or ""
        name = app.localizedName() or ""
        return bundle == "com.tencent.xinWeChat" or name in WECHAT_APP_NAMES
    except Exception:
        return None


def find_wechat_window(previous_wid: int | None = None) -> WindowInfo | None:
    """Prefer the main chat window over larger detached WeChat windows.

    Window ownership is matched exactly against WECHAT_APP_NAMES. It used to be a
    substring test, which missed the ``Weixin`` alias — a 4.x build reporting that name
    found no window at all, so the panel just disappeared — and at the same time let
    any owner containing 微信 through, including sibling apps whose own titled windows
    clear the size gate below (#50).

    This filters on the owning *app*, not on the window, so WeChat's detached
    mini-program and web windows still carry owner ``WeChat`` and stay eligible — that
    is what keeps the main-window-absent fallback working.
    """
    opts = Quartz.kCGWindowListOptionAll | Quartz.kCGWindowListExcludeDesktopElements
    wins = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID)
    best: WindowInfo | None = None
    for w in wins:
        owner = w.get("kCGWindowOwnerName") or ""
        if owner not in WECHAT_APP_NAMES:
            continue
        title = w.get("kCGWindowName") or ""
        b = dict(w.get("kCGWindowBounds") or {})
        wi = WindowInfo(
            wid=int(w.get("kCGWindowNumber") or 0),
            pid=int(w.get("kCGWindowOwnerPID") or 0),
            title=title,
            x=float(b.get("X", 0)), y=float(b.get("Y", 0)),
            w=float(b.get("Width", 0)), h=float(b.get("Height", 0)),
        )
        # main window: has a title, layer 0-ish, big, roughly window-shaped
        if not title or wi.w < 600 or wi.h < 400:
            continue
        # only a titled, window-sized window can be the main chat window. The main window
        # is titled with the app's own display name, so this priority check reads the same
        # list rather than keeping a second copy that drifts out of step (#50).
        if best is None or (wi.title in WECHAT_APP_NAMES, wi.w * wi.h, wi.wid) > (
                best.title in WECHAT_APP_NAMES, best.w * best.h, best.wid):
            best = wi

    # stick with the window we already chose: WeChat 4.x keeps several equally-sized
    # windows around, and re-picking each tick let the target jump between them.
    # Only keep that choice within the same priority; a newly available main wins.
    if previous_wid is not None and best is not None and best.wid != previous_wid:
        for w in wins:
            owner = w.get("kCGWindowOwnerName") or ""
            if owner not in WECHAT_APP_NAMES:
                continue
            if int(w.get("kCGWindowNumber") or 0) != previous_wid:
                continue
            title = w.get("kCGWindowName") or ""
            b = dict(w.get("kCGWindowBounds") or {})
            pw = float(b.get("Width", 0))
            ph = float(b.get("Height", 0))
            if (title and pw >= 600 and ph >= 400
                    and (title in WECHAT_APP_NAMES) == (best.title in WECHAT_APP_NAMES)):
                return WindowInfo(wid=previous_wid, pid=int(w.get("kCGWindowOwnerPID") or 0),
                                  title=title, x=float(b.get("X", 0)), y=float(b.get("Y", 0)),
                                  w=pw, h=ph)
    return best


def capture_window(wid: int, out: Path) -> bool:
    p = subprocess.run(["screencapture", "-x", "-o", "-l", str(wid), str(out)],
                       capture_output=True, text=True)
    return p.returncode == 0 and out.exists() and out.stat().st_size > 1000


# ----------------------------------------------------------------------------- ocr


def _vision_blocks(handler, languages, chat_only: bool, input_top=None) -> list[TextBlock]:
    """Run one Vision text request against a handler that is already built.

    Shared by the file path and the in-memory path so the request settings — the part that
    was measured and tuned — exist exactly once.
    """
    import Vision
    from Quartz import CGRectMake

    blocks: list[TextBlock] = []

    def completion(request, error):
        if error:
            return
        for obs in request.results() or []:
            cands = obs.topCandidates_(1)
            if not cands:
                continue
            c = cands[0]
            bb = obs.boundingBox()
            blocks.append(TextBlock(
                text=c.string().strip(),
                conf=float(c.confidence()),
                x=float(bb.origin.x), y=float(bb.origin.y),
                w=float(bb.size.width), h=float(bb.size.height),
            ))

    req = Vision.VNRecognizeTextRequest.alloc().initWithCompletionHandler_(completion)
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setRecognitionLanguages_(list(languages))
    req.setUsesLanguageCorrection_(True)
    roi = None
    if chat_only:
        # Vision region of interest: normalized, origin BOTTOM-LEFT. Skipping the chat
        # list roughly halves OCR time. Note Vision then reports each observation's
        # bounding box RELATIVE TO THE ROI, so we convert back to full-window space.
        bottom = INPUT_AREA_Y_MIN if input_top is None else 1.0 - input_top
        roi = (CHAT_PANE_X_MIN, bottom, 1.0 - CHAT_PANE_X_MIN, 1.0 - bottom)
        req.setRegionOfInterest_(CGRectMake(*roi))
    handler.performRequests_error_([req], None)

    if roi is not None:
        rx, ry, rw, rh = roi
        for b in blocks:
            b.x = rx + b.x * rw
            b.y = ry + b.y * rh
            b.w *= rw
            b.h *= rh
    return blocks


def ocr(path: Path, languages=("zh-Hans",), chat_only: bool = True, input_top=None) -> list[TextBlock]:
    """Vision OCR over the chat pane, from a PNG on disk.

    zh-Hans alone: adding "en-US" bought nothing and cost time — on one screenshot the two
    settings returned text identical *block for block* at 433 ms vs 303 ms, i.e. ~30% of the
    OCR budget for no change in output. The zh-Hans model reads the Latin words that turn up
    inside Chinese chat text (product names, URLs, "gpt"/"glm-4-fl") by itself.

    Language correction stays ON (it costs ~30 ms more): it is what repairs ordinary OCR
    slips such as 记亿力 for 记忆力, and one wrong character changes what the judge reads.

    This is the fallback path; read_conversation() prefers the in-memory one.
    """
    import Vision
    from Foundation import NSURL

    url = NSURL.fileURLWithPath_(str(path))
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(url, None)
    return _vision_blocks(handler, languages, chat_only, input_top)


def capture_image(wid: int, nominal: bool = True):
    """The window's pixels as a CGImage, without leaving the process. None when refused.

    Against `screencapture -l <wid>` writing a PNG, this is 4–29 ms versus 128–270 ms for the
    same window with identical recognition results (10 blocks, same text) — the difference
    being a subprocess spawn plus PNG encoding plus reading it back off disk. The capture
    runs every second, so when it works the saving is continuous.

    nominal=True captures at 1x instead of the default retina 2x: Vision's cost scales
    with pixel count, and chat text at 1x is still ~15 px tall — measured on rendered
    Chinese lines, recognition is identical block-for-block while OCR time roughly halves.
    The layout constants are all normalized, so nothing downstream notices the resolution.
    If a macOS update ever refuses the flag and returns NULL, read_conversation() falls
    back to the subprocess route and the log says so — degraded to the old behaviour,
    never broken.

    It does NOT always work: the same call returns NULL once the display is asleep, while
    `screencapture` keeps producing images. So callers must treat None as "use the slow
    route" rather than an error — read_conversation() does exactly that, and reports which
    route it took so a permanent fallback is visible instead of just feeling slow.
    """
    import Quartz
    try:
        opts = Quartz.kCGWindowImageBoundsIgnoreFraming
        if nominal:
            opts |= Quartz.kCGWindowImageNominalResolution
        return Quartz.CGWindowListCreateImage(
            Quartz.CGRectNull, Quartz.kCGWindowListOptionIncludingWindow, wid, opts)
    except Exception:
        return None


def ocr_image(image, languages=("zh-Hans",), chat_only: bool = True, input_top=None) -> list[TextBlock]:
    """Same request as ocr(), fed a CGImage directly — no PNG encode, no temp file."""
    import Vision
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None)
    return _vision_blocks(handler, languages, chat_only, input_top)


def warm_ocr() -> float:
    """Pay Vision's one-off recognition-model load on a blank canvas, not a real read.

    The first text recognition in a process costs ~2x steady state (~0.7 s vs ~250 ms)
    while Vision loads its recognition model. Running that first request on a small white
    image needs no WeChat window at all — it works even when WeChat starts after this
    app — so the read loop's first real read finds the framework already paid for.
    Returns the elapsed milliseconds, or -1.0 when the request itself failed.
    """
    cs = Quartz.CGColorSpaceCreateDeviceRGB()
    ctx = Quartz.CGBitmapContextCreate(
        None, 64, 64, 8, 64 * 4, cs, Quartz.kCGImageAlphaPremultipliedLast)
    Quartz.CGContextSetRGBFillColor(ctx, 1.0, 1.0, 1.0, 1.0)
    Quartz.CGContextFillRect(ctx, Quartz.CGRectMake(0, 0, 64, 64))
    image = Quartz.CGBitmapContextCreateImage(ctx)
    t0 = time.perf_counter()
    try:
        ocr_image(image, chat_only=False)
    except Exception:
        return -1.0
    return (time.perf_counter() - t0) * 1000


# ----------------------------------------------------------------- fingerprint

# Fixed grid, independent of window size: a resize re-fingerprints as "different" instead
# of aliasing onto a match. At 128x224 a single chat character still covers a handful of
# cells, so the smallest change anyone could send shifts far more bytes than noise.
_FP_W, _FP_H = 128, 224


def _fingerprint(image, input_top=None) -> bytes | None:
    """The chat pane (title band down to just above the input box) as a small grayscale
    thumbnail; None when anything in the pipeline refuses.

    read_conversation() compares this between ticks: the same picture means the pixels
    did not move, so OCR cannot have anything new to report and its ~300 ms can be
    skipped. The input box is excluded on purpose — the caret blinks there, and it would
    keep a quiet screen looking busy forever. The chat list is excluded for the same
    reason (unread badges), which is also why the crop starts at CHAT_PANE_X_MIN.
    """
    import ctypes

    try:
        w = Quartz.CGImageGetWidth(image)
        h = Quartz.CGImageGetHeight(image)
        # Layout constants here are bottom-origin (Vision's convention); CGImage cropping
        # is top-origin, so the band "input-area top edge .. window top" becomes
        # y=0 .. (1 - INPUT_AREA_Y_MIN) from the top.
        crop = Quartz.CGImageCreateWithImageInRect(
            image,
            Quartz.CGRectMake(int(CHAT_PANE_X_MIN * w), 0,
                              int((1.0 - CHAT_PANE_X_MIN) * w),
                              int((1.0 - INPUT_AREA_Y_MIN if input_top is None else input_top) * h)))
        cs = Quartz.CGColorSpaceCreateDeviceGray()
        buf = ctypes.create_string_buffer(_FP_W * _FP_H)
        ctx = Quartz.CGBitmapContextCreate(
            buf, _FP_W, _FP_H, 8, _FP_W, cs, Quartz.kCGImageAlphaNone)
        Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, _FP_W, _FP_H), crop)
        return buf.raw
    except Exception:
        return None


def _same_frame(a: bytes | None, b: bytes | None) -> bool:
    """True when two fingerprints are the same picture.

    Exact equality covers the static case at C speed. When it fails, a tolerant count
    decides: a few small byte deltas is rendering noise (treat as same, skip OCR),
    anything a person sent rewrites whole glyph cells (treat as changed). Missing a real
    change would mean missing a message, so the threshold sits well under what one
    character produces.
    """
    if a is None or b is None:
        return False
    if a == b:
        return True
    return sum(1 for x, y in zip(a, b) if abs(x - y) >= 8) < 6


# ---------------------------------------------------------------------- extraction


def _is_noise(b: TextBlock) -> bool:
    if b.conf < MIN_CONF or len(b.text) < MIN_TEXT_LEN:
        return True
    if TIMESTAMP_RE.match(b.text):
        return True
    return any(re.search(pat, b.text) for pat in UI_NOISE)


def extract_chat_title(blocks: list[TextBlock]) -> str:
    """Read the conversation name from the chat pane's header band.

    Two rows live up there: the title itself and (when a chat is collapsed) a
    "folded chats" banner. We take the topmost readable band and drop the banner.
    """
    cands = [b for b in blocks
             if b.x >= CHAT_PANE_X_MIN and b.y > TITLE_BAR_Y_MAX
             and not _is_noise(b)]
    # ponytail: WeChat titles are left-aligned; centered headers need layout detection.
    # Locate the title before considering the call/menu glyphs on the right. A real
    # contact name can be just one character, so length cannot distinguish the two.
    starts = [b for b in cands if b.x < (CHAT_PANE_X_MIN + 1) / 2]
    if not starts:
        return ""
    top_y = max(b.y for b in starts)
    band = sorted((b for b in cands if abs(top_y - b.y) < 0.03), key=lambda b: b.x)
    keep = [band[0]]
    for b in band[1:]:
        # Join adjacent OCR fragments, including a short suffix; stop before controls.
        if b.x - keep[-1].x_right > 1.5 * max(b.h, keep[-1].h):
            break
        keep.append(b)
    return " ".join(b.text for b in keep).strip()


def message_side(x: float, width: float) -> str:
    """Conservative text geometry: a center alone cannot identify a wide bubble.

    Only accept text anchored clearly to one side of the calibrated chat pane.
    Wide text spanning both anchors and isolated central fragments are ambiguous;
    keep them for context/overlay, but never treat them as an incoming reply target.
    """
    right = x + width
    if x >= 0.66 or (x >= 0.50 and right >= 0.80):
        return "me"
    if x <= 0.50 and right < 0.80:
        return "them"
    return "unknown"


def extract_messages(blocks: list[TextBlock], max_messages: int = 12, input_top=None) -> list[Message]:
    """Turn raw OCR blocks into an ordered list of chat messages (bottom = newest)."""
    chat = [b for b in blocks
            if b.x >= CHAT_PANE_X_MIN
            and (INPUT_AREA_Y_MIN if input_top is None else 1.0 - input_top) < b.y < TITLE_BAR_Y_MAX
            and not _is_noise(b)]
    if not chat:
        return []

    # Vision y is bottom-origin and bb.origin.y is the box's BOTTOM edge. Convert to a
    # top-origin TOP edge (1 - y - h) so the stored y is literal: "distance from the
    # pane's top to where this box starts". The old 1 - y stored the bottom edge's
    # distance from the top — orderings and gap thresholds did not care (the transform
    # is monotonic), but the YOLO overlay draws y as the top edge and every box sank by
    # one box-height. Sorting and the fold/group logic below are unchanged either way.
    for b in chat:
        b.y = 1.0 - b.y - b.h
    chat.sort(key=lambda b: b.y)

    # group blocks that sit on the same visual line
    lines: list[list[TextBlock]] = []
    for b in chat:
        if lines and abs(b.y - lines[-1][0].y) < 0.012:
            lines[-1].append(b)
        else:
            lines.append([b])

    merged = []
    for group in lines:
        group.sort(key=lambda b: b.x)
        text = " ".join(b.text for b in group)
        merged.append(TextBlock(text=text, conf=min(b.conf for b in group),
                                x=min(b.x for b in group), y=group[0].y,
                                w=max(b.x_right for b in group) - min(b.x for b in group),
                                h=max(b.h for b in group)))

    # fold continuation lines (same side, tight vertical gap, no new sender header)
    per_line = sorted(merged, key=lambda b: b.y)
    # Classify sender headers BEFORE folding; otherwise a tight nickname/body pair
    # becomes one multiline message and the old post-fold name detector misses it.
    # Compare adjacent font heights rather than a fraction of the window height.
    headers = set()
    for i, b in enumerate(per_line[:-1]):
        nxt = per_line[i + 1]
        if (message_side(b.x, b.w) == message_side(nxt.x, nxt.w) == "them"
                and len(b.text) <= 32 and b.h <= nxt.h * .88
                and abs(b.x - nxt.x) < .03
                and b.h <= nxt.y - b.y <= max(.16, b.h * 4)):
            headers.add(i)
    messages: list[Message] = []
    pending_sender = None
    for i, b in enumerate(per_line):
        if i in headers:
            pending_sender = b.text.strip().rstrip("：:")
            continue
        side = message_side(b.x, b.w)
        # fold against the LAST folded line, not the message's first: comparing against
        # the first line made every line from the third on measure ≥2 line-pitches away,
        # so any 3+ line message was split into ≤2-line chunks — the judge then only ever
        # saw the tail chunk, and the overlay drew a box per chunk
        gap = (b.y - messages[-1].last_y) if messages else 1.0
        # OCR can shift a short continuation's left edge slightly. Group by alignment,
        # then classify the full bounds rather than inheriting a possibly wrong first line.
        aligned = messages and abs(b.x - messages[-1].x) < 0.02
        compatible = messages and (side == messages[-1].side
                                   or "unknown" in (side, messages[-1].side))
        if pending_sender is None and aligned and compatible and 0 <= gap < 0.045:
            messages[-1].lines.append(b.text)
            messages[-1].text = "\n".join(messages[-1].lines)
            messages[-1].conf = min(messages[-1].conf, b.conf)
            # grow the bounding box to cover the folded line (m.y stays the top line's)
            m = messages[-1]
            bottom = max(m.y + m.h, b.y + b.h)
            right = max(m.x + m.w, b.x_right)
            m.x = min(m.x, b.x)
            m.w = right - m.x
            m.side = message_side(m.x, m.w)
            m.h = bottom - m.y
            m.last_y = b.y
        else:
            messages.append(Message(text=b.text, side=side, y=b.y, conf=b.conf,
                                    h=b.h, lines=[b.text], x=b.x, w=b.w,
                                    last_y=b.y, sender=pending_sender))
            pending_sender = None

    return messages[-max_messages:]


def looks_like_sender_name(msg: Message, following: Message | None) -> bool:
    """Heuristic: group chats render the sender name as a short line above the bubble."""
    if following is None:
        return False
    t = msg.text.strip()
    if len(t) > 16 or "\n" in t:
        return False
    gap = following.y - msg.y
    return gap > 0.045


# ---------------------------------------------------------------------- public API


def read_conversation(max_messages: int = 12, previous_wid: int | None = None,
                      prev_fingerprint: bytes | None = None, prev_layout=None) -> dict:
    """One-shot read: find window -> capture -> OCR -> messages.

    Pass the previous call's "fingerprint" and an unchanged chat pane short-circuits
    before OCR: ok=True with "unchanged": True, empty messages, and the window's current
    geometry — the caller reuses what it last read and keeps positioning from fresh
    coordinates. The layout key must also match: resizing the window or input panel
    always forces fresh extraction. Both capture paths use the same image for the
    input boundary, fingerprint and OCR; an unresolved boundary yields no messages.
    """
    t0 = time.perf_counter()
    win = find_wechat_window(previous_wid)
    if win is None:
        return {"ok": False, "error": "WeChat main window not found", "messages": []}

    # In-process capture + OCR off the CGImage is the fast path (~250 ms for the pair).
    # The subprocess + PNG route stays as the fallback: it is ~150 ms slower, but it is the
    # one that still worked when CGWindowListCreateImage had nothing to give.
    image = capture_image(win.wid)
    capture_path = "memory" if image is not None else "subprocess"
    window = {"wid": win.wid, "title": win.title, "w": win.w, "h": win.h,
              "x": win.x, "y": win.y}
    # Use the same captured pixels for the input boundary and OCR. Never reuse a
    # previous window's boundary after resizing or switching windows.
    if image is None:
        with tempfile.TemporaryDirectory() as td:
            png = Path(td) / "wechat.png"
            if capture_window(win.wid, png):
                from Foundation import NSURL
                source = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(str(png)), None)
                image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None) if source else None
    from input_region import input_outline
    try:
        outline = input_outline(image) if image is not None else None
    except Exception:
        outline = None
    # AX is a fallback for themes with no visible separator. Its text-area top is
    # sufficient to exclude drafts, even if the toolbar above it remains visible.
    if outline is None:
        import fill
        target = fill.locate_input(window)
        rect = target.get("rect")
        if rect:
            x, y, w, h = rect
            outline = ((x-win.x)/win.w, (y-win.y)/win.h, w/win.w, h/win.h)
    input_top = outline[1] if outline else None
    if input_top is not None and not .2 < input_top < .95:
        outline = None
        input_top = None
    layout = (win.wid, win.w, win.h, input_top)
    visual_rect = ((win.x + outline[0]*win.w, win.y + outline[1]*win.h,
                    outline[2]*win.w, outline[3]*win.h) if outline else None)
    fingerprint = _fingerprint(image, input_top) if image is not None and outline else None
    if layout == prev_layout and _same_frame(fingerprint, prev_fingerprint):
        total = (time.perf_counter() - t0) * 1000
        return {"ok": True, "unchanged": True, "messages": [], "fingerprint": fingerprint,
                "chat_title": "", "window": window, "n_blocks": 0,
                "layout": layout, "input_rect": visual_rect,
                "timing_ms": {"capture": total, "ocr": 0.0, "total": total,
                              "capture_path": capture_path}}

    t_cap = time.perf_counter()
    if image is None:
        return {"ok": False, "error": "capture failed", "messages": []}
    blocks = ocr_image(image, input_top=input_top)
    t_ocr = time.perf_counter()

    chat_title = extract_chat_title(blocks)
    msgs = (extract_messages(blocks, max_messages=max_messages, input_top=input_top)
            if outline else [])
    return {
        "ok": True,
        "unchanged": False,
        "layout": layout,
        "input_rect": visual_rect,
        "input_unresolved": outline is None,
        "chat_title": chat_title,
        "window": window,
        "messages": msgs,
        "timing_ms": {"capture": (t_cap - t0) * 1000, "ocr": (t_ocr - t_cap) * 1000,
                      "total": (t_ocr - t0) * 1000, "capture_path": capture_path},
        "n_blocks": len(blocks),
        "fingerprint": fingerprint,
    }


if __name__ == "__main__":
    import json

    res = read_conversation()
    if not res["ok"]:
        print("ERROR:", res["error"])
        raise SystemExit(1)
    print(f"window {res['window']['w']:.0f}x{res['window']['h']} "
          f"capture={res['timing_ms']['capture']:.0f}ms ocr={res['timing_ms']['ocr']:.0f}ms "
          f"blocks={res['n_blocks']}")
    print("--- messages (top to bottom) ---")
    for m in res["messages"]:
        print(f"  [{m.side:4s}] y={m.y:.3f} conf={m.conf:.2f} | {m.text}")
