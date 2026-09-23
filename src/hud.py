"""Floating HUD: a non-activating panel beside WeChat showing intent, risk and ranked replies.

Design notes
  * NSWindowStyleMaskNonactivatingPanel + floating level: the panel never steals focus
    from WeChat, and window-ID capture means it never appears in our own screenshots.
  * Poll loop: read the chat, hash the newest message, judge only when it changes.
  * The moment a new message is SEEN, both halves start (local pre-judge and the paid
    generation run concurrently, latest-wins); the settle gate then spends the finished
    verdict, waits out whatever generation is still missing, and only the local ranking
    (~0.5 s) is left after it. Candidates display before ranking finishes ("排序中")
    and are re-ordered in place when it lands.
  * The panel positions itself against WeChat's window each tick, so it follows moves,
    resizes and monitor changes without any window-server hooks.
  * The HUD uses native macOS vibrancy with semantic WeChat green/amber/red accents. The
    Appearance stays pinned to Aqua so labels and controls keep the same tested contrast.
  * 「填入」 is dispatched through the per-app adapter layer (src/apps/) into the current
    chat app's input box (WeChat: Accessibility writes via src/fill.py; QQ: AX value-set
    with a keyboard-events fallback). No clipboard, and nothing is ever sent. It needs
    the Accessibility permission; when that is missing the HUD asks for it and reports
    the failure.
"""

from __future__ import annotations

import objc
import os
import subprocess
import threading
import time
from pathlib import Path

import AppKit
import Quartz
import sys

from AppKit import (
    NSAppearance,
    NSAttributedString,
    NSBackingStoreBuffered,
    NSBackgroundColorAttributeName,
    NSBezierPath,
    NSButton,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSPanel,
    NSPasteboard,
    NSPasteboardTypeString,
    NSPopUpButton,
    NSScreen,
    NSTextField,
    NSView,
    NSWindowMiniaturizeButton,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskNonactivatingPanel,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
    NSWindowZoomButton,
    NSWindowCloseButton,
)
from Foundation import (NSMutableAttributedString, NSMakeRange, NSMakeRect,
                        NSMakeSize, NSObject, NSTimer)

sys.path.insert(0, str(Path(__file__).parent))
import userconfig  # noqa: E402

userconfig.load()   # ~/.config/jev-jarvis/env -> os.environ (Finder apps inherit none)

from perception import (  # noqa: E402
    find_wechat_window,
    screen_capture_ok,
    request_screen_capture,
    reply_span,
    reply_text,
)
from apps.registry import APPS, UNKNOWN, frontmost_app  # noqa: E402  按前台 App 分发（微信 / QQ）
import judge  # noqa: E402  (model_cached / model_disk_usage: the #38 onboarding + settings)
from judge import LowMemoryError, ModelNotDownloadedError, make_judge  # noqa: E402
from generate import BUILTIN_SOURCE, Generator, load_credentials  # noqa: E402
import styles  # noqa: E402
import fill  # noqa: E402
import ui_style  # noqa: E402
import chat_context
import settings_config  # noqa: E402

PANEL_W, PANEL_H = 360, 614   # initial size; the user can resize both dimensions
PANEL_MIN_W, PANEL_MIN_H = 320, 260
COLLAPSED_H = 96              # height when the panel is rolled up
# The tick timer fires at FAST_TICK; a read only runs when due. A quiet screen (fingerprint
# match ⇒ no OCR) re-checks every FAST_TICK — a new message surfaces within 0.25 s instead
# of within 1 s. A read that found a change (full capture+OCR paid) first keeps a SHORT
# cadence for a few reads (a burst's next message is noticed in ~0.45 s, not after a full
# SLOW_TICK) and only settles back to SLOW_TICK if the pane keeps moving — that is the
# cadence the old fixed poll had, kept as the CPU guard for a continuously moving screen.
FAST_TICK = 0.25         # re-check cadence while the chat pane is quiet
BURST_TICK = 0.45        # short cadence right after a change: catch the burst's next message
BURST_READS = 3          # how many reads stay on BURST_TICK before falling back to SLOW_TICK
SLOW_TICK = 1.0          # re-check cadence while the chat pane keeps moving
READ_FAILURE_HIDE_S = 2.0  # do not flicker on a transient capture/window miss
EMPTY_FRAME_REUSE_S = READ_FAILURE_HIDE_S  # #58: how long an empty-OCR streak may
                                           # reuse the last read before giving up
SETTLE_S = 1.2           # upper bound on the settle wait (anti-flood; unchanged by design)
EARLY_SETTLE_S = 0.70    # the gate may open this early …
STABLE_READS = 3         # … but only after this many consecutive unchanged reads
MIN_GAP_S = 2.0          # never restart analysis faster than this
IDLE_STATUS = "等待微信 / QQ 消息…"   # the resting status line (also set at build time)
WARM_STATUS = "判断模型加载中…（首次需下载，可能数分钟）"  # shown while judge warm-up runs


# Shared with the settings window so both surfaces keep one visual vocabulary.
PALETTE = ui_style.PALETTE
_rgb = ui_style.rgb

# Reply cards: probability rail, full-width wrapped text, then adjustment/copy/fill actions.
# Only the minimum is fixed. _relayout() measures each candidate and grows the row as needed.
CAND_ROW_X, CAND_ROW_W, CAND_ROW_MIN_H = 20, PANEL_W - 40, 62
CAND_PROB_X, CAND_PROB_W = 30, 44
CAND_TEXT_X, CAND_TEXT_W = 82, PANEL_W - 112
CAND_BTN_W, CAND_BTN_H, CAND_BTN_GAP = 48, 22, 4
CAND_BTN_X = PANEL_W - 26 - (2 * CAND_BTN_W + CAND_BTN_GAP)
CAND_ROW_GAP = 4

# 话术 groups. Each group is headed by its dropdown; its candidates sit under it. The panel
# is only as tall as the groups in use, so nothing is reserved for a tone that is switched
# off (that reservation is what used to leave a dead gap in the middle).
TONE_DD_X, TONE_DD_W, TONE_DD_H, TONE_DD_GAP = 20, PANEL_W - 40, 24, 5
TONE_DD_INSET = 8         # the popup sits this far inside its field, like text in an input box
TONE_DD_FONT = 12         # compact but still the clearest interactive label in each group
                          # thing on the panel the user is meant to click
GROUP_PAD_Y = 5           # breathing room above the selector and below the final reply
GROUP_GAP = 10            # between one group's rows and the next group's dropdown
BOTTOM_PAD = 14           # below the last group


def _candidate_geometry(panel_w: float) -> dict[str, float]:
    """Horizontal candidate geometry for a live panel width.

    Actions sit below the text. The probability rail retains its usable width, and
    the full remaining card width goes to the reply text at every panel size.
    Kept pure so resize behaviour can be regression-tested without starting AppKit.
    """
    width = max(PANEL_MIN_W, float(panel_w))
    button_x = width - 26 - (2 * CAND_BTN_W + CAND_BTN_GAP)
    text_w = max(88.0, width - 30 - CAND_TEXT_X)
    return {
        "row_x": CAND_ROW_X, "row_w": width - 40,
        "prob_x": CAND_PROB_X, "prob_w": CAND_PROB_W,
        "text_x": CAND_TEXT_X, "text_w": text_w,
        "button_x": button_x,
        "tone_x": TONE_DD_X, "tone_w": width - 40,
        "group_x": 14, "group_w": width - 28,
    }


def _responsive_xw(rule: tuple | None, panel_w: float,
                   default_x: float, default_w: float) -> tuple[float, float]:
    """Resolve one fixed header/summary control against the current window width."""
    if not rule:
        return default_x, default_w
    width = max(PANEL_MIN_W, float(panel_w))
    div1 = width * (150 / PANEL_W)
    div2 = width * (260 / PANEL_W)
    kind, *args = rule
    if kind == "stretch":
        left, right = args
        return left, max(1, width - left - right)
    if kind == "right":
        right, fixed_w = args
        return width - right - fixed_w, fixed_w
    if kind == "divider":
        ratio, fixed_w = args
        return width * ratio, fixed_w
    if kind == "segment1":
        left, right = args
        return left, max(1, div1 - left - right)
    if kind == "segment2":
        left, right = args
        return div1 + left, max(1, div2 - div1 - left - right)
    if kind == "segment3":
        left, right = args
        return div2 + left, max(1, width - div2 - left - right)
    if kind == "third_center":
        offset, fixed_w = args
        center = (div2 + width) / 2 - 6 + offset
        return center - fixed_w / 2, fixed_w
    return default_x, default_w


def _viewport_fit(natural_h: float, viewport_h: float, message_h: float,
                  message_document_h: float, preserve_window: bool
                  ) -> tuple[float, float, bool]:
    """Fit natural content into a user-sized viewport.

    Spare height expands the read-result area first.  A shorter viewport keeps the full
    document height so the outer scroll view can expose every control.  Before the first
    manual resize, the legacy auto-sized window remains content-driven.
    """
    if not preserve_window:
        return 0.0, natural_h, False
    expandable = max(0.0, min(180.0, message_document_h) - message_h)
    message_extra = min(expandable, max(0.0, viewport_h - natural_h))
    fitted_h = natural_h + message_extra
    content_h = max(fitted_h, viewport_h)
    return message_extra, content_h, fitted_h > viewport_h + .5


LOG_PATH = Path.home() / "Library" / "Logs" / "jev-jarvis.log"


def _log(msg: str) -> None:
    """One line per stage: to stdout, and into ~/Library/Logs/jev-jarvis.log.

    "It feels slow" is not actionable on its own, so every analysis prints what each stage
    cost; that is the whole point of this function. Deliberately **no message text and no
    candidate text**: this file is meant to be pasted into an issue, and the app's premise
    is that chat content stays on the machine.

    Both destinations on purpose: the .app launcher already redirects stdout into this same
    file, while `./start.command` only shows a terminal — so which place held the evidence
    depended on how the user happened to launch it. The inode check stops the .app case
    from writing every line twice.
    """
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        if os.fstat(sys.stdout.fileno()).st_ino == LOG_PATH.stat().st_ino:
            return                       # stdout already IS that file (the .app case)
    except Exception:
        pass
    try:
        with open(LOG_PATH, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass                             # a log we cannot write is not worth breaking over


class _FlippedView(NSView):
    """Top-origin scroll document so layout coordinates stay readable."""

    def isFlipped(self):
        return True


class _BoxesView(NSView):
    """The YOLO overlay's canvas: paints whatever `boxes` last held.

    boxes: [(NSRect, NSColor, line_width, NSAttributedString chip), ...] in view
    coordinates, set from the main thread and followed by setNeedsDisplay_. The view
    owns no data — it only renders the controller's most recent read, which is what
    keeps the overlay honest: what you see boxed is exactly what the pipeline read.
    """

    def drawRect_(self, rect):
        for box in getattr(self, "boxes", None) or []:
            r, color, lw, chip = box[:4]
            color.set()
            NSBezierPath.setDefaultLineWidth_(lw)
            if len(box) > 4 and box[4]:
                path = NSBezierPath.bezierPathWithRect_(r)
                path.setLineWidth_(lw)
                path.setLineDash_count_phase_([6.0, 4.0], 2, 0)
                path.stroke()
            else:
                NSBezierPath.strokeRect_(r)
            chip.drawAtPoint_((r.origin.x, r.origin.y + r.size.height + 2))


class HudController(NSObject):
    def init(self):
        self = objc.super(HudController, self).init()
        if self is None:
            return None
        self._input_calibration = None
        self._input_calibration_wid = None
        self._input_calibration_saved = userconfig.get("JEV_INPUT_REGION")
        self._calibration = None
        self._calibration_wid = None
        self._calibration_saved = userconfig.get("JEV_MESSAGE_REGION")
        self._calibration_required = bool(self._calibration_saved)
        self._calibrating = False
        self.last_seen = None          # newest message text observed
        self._reply_key = None         # (conversation, incoming text), never an outgoing message
        self._reply_epoch = 0          # invalidate even if the same text reappears later
        self._reply_worker = threading.local()
        self._active_context = None
        self._context_lock = threading.RLock()
        self._context_version = 0
        self.history_enabled = userconfig.get("JEV_HISTORY") == "1"
        try:
            self.context_limit = chat_context.message_limit(userconfig.get("JEV_CONTEXT_MESSAGES") or "20")
        except ValueError:
            self.context_limit = 20
            _log("上下文条数配置无效，使用默认值 20")
        self._observed_messages = None
        self._observed_offset = 0
        self.conversations = None
        try:
            self.conversations = chat_context.Conversations()
        except (OSError, ValueError):
            _log("本地会话数据读取失败，历史和背景暂不可用")
        self.last_change_ts = 0.0      # when it last changed (burst detection)
        self.last_analyze_ts = 0.0     # rate limit for analysis starts
        self.analyzed_text = None      # what the panel currently shows
        self._analyzed_body = None     # current words without any quoted preview
        self._judged_once = False      # first judge call includes the local model load
        self._read_once = False        # first OCR call includes Vision's own load
        self._last_skip_reason = None
        self.judge = make_judge()
        self._judgment_enabled = getattr(self.judge, "enabled", True)
        self._normal_status = (IDLE_STATUS, PALETTE["muted"])
        self._model_status = None
        self.generator = Generator()
        self.generation_context_turns = settings_config.context_turns(
            "GENERATION_CONTEXT_TURNS")
        self.judge_context_turns = settings_config.context_turns("JUDGE_CONTEXT_TURNS")
        self.candidate_count = settings_config.candidate_count()
        self.auto_hide = settings_config.bool_setting("JEV_AUTO_HIDE")
        self.auto_dock = settings_config.bool_setting("JEV_AUTO_DOCK")
        self.background_capture = settings_config.bool_setting("JEV_BACKGROUND_CAPTURE")
        self.panel_always_on_top = settings_config.bool_setting(
            "JEV_PANEL_ALWAYS_ON_TOP")
        # 话术: per-slot tone selection. A slot on 不用 contributes no request and no rows,
        # so the panel is exactly as tall as the groups actually in use.
        self.slot_tones = settings_config.default_tones(
            styles.PRESETS, styles.NONE_LABEL, styles.MAX_SLOTS)
        self._dds: list = []
        self._dd_boxes: list = []       # the flat fields the dropdowns are drawn into
        self._group_boxes: list = []    # translucent surfaces behind active tone groups
        self._rows: list = []
        self._appearance_surfaces = []
        self._appearance_buttons = []
        self._message_expanded = False
        self._message_text = ""
        self._read_result_text = ""
        self._read_result_count = 0
        self._layout_key = None
        self._fixed: list = []          # (control, x, dy_from_top, w, h) — the rows above
        self._responsive_rules: dict[int, tuple] = {}  # fixed control -> width rule
        self._detail_views: list = []   # non-data chrome hidden with the expanded details
        self._judgment_views: list = [] # intent/risk/action chrome, absent when not configured
        self._risk_dots: list = []      # low / medium / high indicators, presentation only
        self._group_top = 0             # where the first group starts, from the top
        self._title_h = 28              # measured right after the panel is built
        self.cand_texts: list[str | None] = [None] * (styles.MAX_SLOTS * styles.PER_TONE)
        self._candidate_sources: list[str | None] = [None] * len(self.cand_texts)
        self._candidate_overrides: dict[tuple[int, str], str] = {}
        self._adjust_seq: dict[tuple[int, str], int] = {}
        self._last_intent = ""          # kept so a tone change can re-rank without re-judging
        # streaming candidates: each generation run bumps this epoch at its start and its
        # streamed lines carry the value, so a late line from a run a tone change or a new
        # message superseded is dropped instead of written into the new run's rows
        self._gen_epoch = 0
        self._stream_rows: dict[int, int] = {}   # slot -> lines already shown, per run

        self._busy = False
        self._next_read_ts = 0.0    # reads before this timestamp are skipped (quiet screen)
        self._fingerprint = None    # last chat-pane fingerprint; equal ⇒ skip OCR entirely
        self._last_full = None      # last OCR'd result, reused while the pane is unchanged
        self._analyzing = False     # judge+generate runs off the tick path
        self._regenerating = False  # explicit candidate refresh; does not re-read/judge
        # Pre-judgment: the local judge starts the moment a new message is seen, and the
        # settle gate consumes the verdict if the text is unchanged — intent/risk land on
        # screen ~1 s earlier and only the (paid) generation half still waits. Single-slot
        # request = latest-wins: a newer text overwrites the slot and retires the verdict.
        self._model_lock = threading.Lock()   # never two local forwards (judge/rank) at once
        self._prejudge_req = None             # (text, context, sender, prev, reply epoch)
        self._prejudge_result = None          # (text, verdict, sender, prev, reply epoch)
        self._prejudging = False              # a pre-judge forward is running right now
        self._prejudge_event = threading.Event()
        threading.Thread(target=self._prejudge_loop, daemon=True).start()
        # Early generation: the paid half starts the moment a message is seen too, with the
        # same latest-wins slot discipline. The settle window (~1 s) then hides the whole
        # generation latency, and only the local ranking is left after the gate opens.
        # Cost: a burst's intermediate messages each fire one discarded API call — cheap at
        # glm-4-flash-class pricing, and superseded results are never consumed.
        self._pregen_req = None              # (text, context, tones tuple, reply epoch)
        self._pregen_result = None           # (text, tones, gen dict, reply epoch)
        self._pregen_running = False         # a pre-generation request is in flight
        self._pregen_event = threading.Event()
        threading.Thread(target=self._pregen_loop, daemon=True).start()
        self._burst_left = BURST_READS       # short-cadence reads left after a change
        self._stable_n = 0                   # consecutive unchanged reads since last change
        self._collapsed = False
        self._expanded_h = None       # full height, captured the first time we collapse
        self._expanded_size = (PANEL_W, PANEL_H)
        self._user_sized = False      # once true, content updates preserve the user's frame
        self._layout_resizing = False # distinguish our setFrame from a drag resize
        self._paused = False
        self._always_on_top = self.panel_always_on_top
        # YOLO overlay default: JEV_BOXES=1 (or true/yes/on) in the env file starts it on;
        # either way the menu-bar item flips it at runtime
        self._show_boxes = userconfig.get("JEV_BOXES").strip().lower() in (
            "1", "true", "yes", "on")
        self._last_risk = 0.0         # newest verdict's risk, for the overlay's highlight
        self._chat_title = ""
        self._asked_permission = False
        self._win_wid = None          # sticky chat window id (per app)
        self._app = None              # 当前前台聊天应用适配器；None = 不在任何聊天应用前台
        self._asked_accessibility = False   # QQ 路径的辅助功能授权只弹一次
        self._wechat_frontmost = None # any supported chat app foreground; background WeChat is opt-in
        self._foreground_epoch = 0    # catches leave+return while one capture is in flight
        self._read_fail_since = None  # debounce transient foreground capture failures
        self._read_fail_hidden = False
        self._empty_frame_since = None  # empty-OCR streak start (#58): reuse the last
                                        # read, give up after EMPTY_FRAME_REUSE_S
        self._last_origin = None      # last applied panel origin
        self._pending_origin = None   # candidate origin awaiting confirmation
        self._build_panel()
        self._build_overlay()
        self._expanded_h = self.panel.frame().size.height
        self._expanded_size = (self.panel.frame().size.width,
                               self.panel.frame().size.height)
        return self

    # ------------------------------------------------------------------ ui
    @objc.python_method
    def _build_panel(self):
        # Closable/Miniaturizable are what actually CREATE the standard window buttons;
        # NonactivatingPanel alone gives a title bar with no controls at all.
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable
                 | NSWindowStyleMaskNonactivatingPanel)
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, PANEL_W, PANEL_H), style, NSBackingStoreBuffered, False)
        self.panel.setLevel_(AppKit.NSFloatingWindowLevel if self._always_on_top
                             else AppKit.NSNormalWindowLevel)
        self.panel.setOpaque_(False)
        self.panel.setAlphaValue_(1.0)
        self.panel.setHasShadow_(True)
        # The title bar and button bezels are drawn from the appearance, not from the
        # background colour, so pin Aqua: a dark-mode system would otherwise give a dark
        # title bar above a white panel.
        self.panel.setAppearance_(NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameAqua))
        self.panel.setBackgroundColor_(NSColor.clearColor())
        self.panel.setTitle_("jev-jarvis")
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setBecomesKeyOnlyIfNeeded_(True)
        self.panel.setMinSize_(NSMakeSize(PANEL_MIN_W, PANEL_MIN_H))
        self.panel.setDelegate_(self)

        # NSVisualEffectView is the native implementation of the reference's light frosted
        # material. The tint keeps text readable when the wallpaper behind it is busy.
        root = AppKit.NSVisualEffectView.alloc().initWithFrame_(
            NSMakeRect(0, 0, PANEL_W, PANEL_H))
        root.setMaterial_(getattr(
            AppKit, "NSVisualEffectMaterialSidebar",
            getattr(AppKit, "NSVisualEffectMaterialLight", 1)))
        root.setBlendingMode_(AppKit.NSVisualEffectBlendingModeBehindWindow)
        root.setState_(AppKit.NSVisualEffectStateActive)
        root.setWantsLayer_(True)
        root.layer().setBackgroundColor_(PALETTE["bg"].CGColor())
        # A tint subview sits above the system material. Setting the effect view's
        # backing-layer color alone can be covered by macOS's accessibility fallback.
        self._solid_backdrop = ui_style.make_surface(0, NSColor.clearColor())
        self._solid_backdrop.setFrame_(root.bounds())
        self._solid_backdrop.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
        root.addSubview_(self._solid_backdrop)

        # A full-panel document scroll makes a user-chosen short window useful rather than
        # clipping controls.  A flipped document keeps every existing `top` coordinate
        # literal and naturally reveals more rows as the window grows.
        scroll = AppKit.NSScrollView.alloc().initWithFrame_(root.bounds())
        scroll.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
        scroll.setDrawsBackground_(False)
        scroll.setHasHorizontalScroller_(False)
        scroll.setHasVerticalScroller_(False)
        scroll.setAutohidesScrollers_(True)
        scroll.setScrollerStyle_(AppKit.NSScrollerStyleOverlay)
        view = _FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, PANEL_W, PANEL_H))
        scroll.setDocumentView_(view)
        root.addSubview_(scroll)
        self._content_scroll = scroll
        self._content_view = view
        self.rows: dict[str, NSTextField] = {}

        def add_fixed(ctrl, x, top, w, h, rule=None):
            self._fixed.append((ctrl, x, top, w, h))
            if rule is not None:
                self._responsive_rules[id(ctrl)] = rule

        # The latest master adds model settings to this same header. Keep it as a quiet,
        # standalone icon so the new control does not collide with the chat title.
        self.settings_button = self._make_button(PANEL_W - 44, 0, 32, 32,
                                                 "", "openSettings:", 0)
        settings_icon = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "gearshape", "设置")
        symbol_config = AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_(
            12, AppKit.NSFontWeightRegular)
        settings_icon = settings_icon.imageWithSymbolConfiguration_(symbol_config)
        self.settings_button.setImage_(settings_icon)
        self.settings_button.setImagePosition_(AppKit.NSImageOnly)
        self.settings_button.setImageScaling_(AppKit.NSImageScaleNone)
        self.settings_button.setBordered_(False)
        self.settings_button.setContentTintColor_(PALETTE["muted"])
        self.settings_button.layer().setBackgroundColor_(NSColor.clearColor().CGColor())
        self.settings_button.layer().setBorderWidth_(0.0)
        self.settings_button.setToolTip_("设置")
        self.settings_button.setAccessibilityLabel_("设置")
        self.settings_button.setHidden_(False)
        view.addSubview_(self.settings_button)
        add_fixed(self.settings_button, PANEL_W - 44, 4, 32, 32,
                  ("right", 12, 32))

        self.calibration_button = self._make_button(PANEL_W - 80, 0, 32, 32,
                                                    "", "calibrateMessages:", 0)
        icon = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "viewfinder", "校准消息区和输入区")
        self.calibration_button.setImage_(icon.imageWithSymbolConfiguration_(symbol_config))
        self.calibration_button.setImagePosition_(AppKit.NSImageOnly)
        self.calibration_button.setImageScaling_(AppKit.NSImageScaleNone)
        self.calibration_button.setBordered_(False)
        self.calibration_button.setContentTintColor_(PALETTE["muted"])
        self.calibration_button.layer().setBackgroundColor_(NSColor.clearColor().CGColor())
        self.calibration_button.layer().setBorderWidth_(0.0)
        self.calibration_button.setToolTip_("校准消息区和输入区")
        self.calibration_button.setAccessibilityLabel_("校准消息区和输入区")
        self.calibration_button.setHidden_(False)
        view.addSubview_(self.calibration_button)
        self._fixed.append((self.calibration_button, PANEL_W - 80, 4, 32, 32))

        # Decorative surfaces are fixed; every string still comes from the existing rows.
        for surface, x, top, w, h in (
            (self._make_surface(12, PALETTE["surface"]), 14, 54, PANEL_W - 28, 62),
            (self._make_surface(12, PALETTE["surface"]), 14, 124, PANEL_W - 28, 72),
            (self._make_surface(10, PALETTE["surface"]), 14, 204, PANEL_W - 28, 34),
        ):
            if top == 54:
                self._message_surface = surface
            view.addSubview_(surface)
            add_fixed(surface, x, top, w, h, ("stretch", 14, 14))
            self._detail_views.append(surface)
            if top != 54:
                self._judgment_views.append(surface)

        # Summary separators and static labels carry no model data; they only make the
        # existing intent/risk/action fields scan like the approved design.
        for x, ratio in ((150, 150 / PANEL_W), (260, 260 / PANEL_W)):
            divider = self._make_surface(0, PALETTE["edge"])
            view.addSubview_(divider)
            add_fixed(divider, x, 136, 1, 46, ("divider", ratio, 1))
            self._detail_views.append(divider)
            self._judgment_views.append(divider)

        action_label = self._make_label(0, 0, 58, 16, size=11,
                                        color=PALETTE["text"], bold=True)
        action_label.setStringValue_("具体行动")
        view.addSubview_(action_label)
        add_fixed(action_label, 26, 213, 58, 16)
        self._detail_views.append(action_label)
        self._judgment_views.append(action_label)

        risk_title = self._make_label(0, 0, 64, 14, size=9, color=PALETTE["muted"])
        risk_title.setStringValue_("风险等级")
        view.addSubview_(risk_title)
        add_fixed(risk_title, 272, 132, 64, 14, ("segment3", 12, 12))
        self._detail_views.append(risk_title)
        self._judgment_views.append(risk_title)
        for i, (title, color) in enumerate((
            ("低", PALETTE["green"]), ("中", PALETTE["amber"]), ("高", PALETTE["red"]))):
            center_x = 278 + i * 26
            dot = self._make_surface(4, color.colorWithAlphaComponent_(0.68))
            view.addSubview_(dot)
            add_fixed(dot, center_x - 4, 152, 8, 8,
                      ("third_center", (i - 1) * 26, 8))
            self._detail_views.append(dot)
            self._judgment_views.append(dot)
            self._risk_dots.append(dot)
            label = self._make_label(0, 0, 20, 14, size=9, color=PALETTE["muted"])
            label.setAlignment_(AppKit.NSTextAlignmentCenter)
            label.setStringValue_(title)
            view.addSubview_(label)
            add_fixed(label, center_x - 10, 164, 20, 14,
                      ("third_center", (i - 1) * 26, 20))
            self._detail_views.append(label)
            self._judgment_views.append(label)

        row_rules = {
            "chat": ("stretch", 20, 56),
            "status": ("stretch", 20, 20),
            "message": ("stretch", 22, 22),
            "sender": ("stretch", 22, 90),
            "intent": ("segment1", 26, 8),
            "confidence": ("segment1", 26, 8),
            "risk": ("segment2", 14, 4),
            "actions": ("stretch", 94, 30),
        }
        for key, x, top, w, h, size, color, bold in (
            ("chat", 20, 14, PANEL_W - 112, 20, 15, PALETTE["accent"], True),
            ("status", 20, 36, PANEL_W - 40, 14, 10, PALETTE["muted"], False),
            ("message", 22, 82, PANEL_W - 44, 38, 14, PALETTE["text"], False),
            ("sender", 22, 62, PANEL_W - 112, 14, 10, PALETTE["muted"], False),
            ("intent", 26, 136, 116, 26, 20, PALETTE["text"], True),
            ("confidence", 26, 166, 116, 16, 11, PALETTE["muted"], False),
            ("risk", 164, 137, 92, 24, 14, PALETTE["green"], True),
            ("actions", 94, 213, 236, 16, 11, PALETTE["text"], False),
        ):
            tf = self._make_label(x, 0, w, h, size=size, color=color, bold=bold)
            if key in {"message", "actions"}:
                tf.cell().setWraps_(True)
            self.rows[key] = tf
            if key == "message":
                tf.cell().setScrollable_(False)
                tf.cell().setUsesSingleLineMode_(False)
                tf.cell().setLineBreakMode_(AppKit.NSLineBreakByWordWrapping)
                tf.setMaximumNumberOfLines_(2)
                scroll = AppKit.NSScrollView.alloc().initWithFrame_(NSMakeRect(x, 0, w, h))
                scroll.setDrawsBackground_(False)
                scroll.setHasVerticalScroller_(False)
                scroll.setAutohidesScrollers_(True)
                scroll.setScrollerStyle_(AppKit.NSScrollerStyleOverlay)
                scroll.setDocumentView_(tf)
                self._message_scroll = scroll
                view.addSubview_(scroll)
                add_fixed(scroll, x, top, w, h, row_rules[key])
                self._detail_views.append(scroll)
            else:
                view.addSubview_(tf)
                add_fixed(tf, x, top, w, h, row_rules[key])
            if key not in {"chat", "status"}:
                self._detail_views.append(tf)
            if key in {"intent", "confidence", "risk", "actions"}:
                self._judgment_views.append(tf)

        self._message_toggle = self._make_button(PANEL_W - 82, 0, 60, 18,
                                                  "展开 ▾", "toggleMessage:", 0)
        self._message_toggle.setAccessibilityLabel_("展开或收起完整消息")
        view.addSubview_(self._message_toggle)
        add_fixed(self._message_toggle, PANEL_W - 82, 60, 60, 18,
                  ("right", 22, 60))
        self._detail_views.append(self._message_toggle)

        header = self._make_label(18, 0, PANEL_W - 36, 18,
                                  size=12, color=PALETTE["text"], bold=True)
        view.addSubview_(header)
        self.rows["cand_header"] = header
        self._set_candidate_header("候选回复（按合适度排序）" if self._judgment_enabled
                                   else "候选回复（生成顺序）")
        header_top = 250 if self._judgment_enabled else 132
        add_fixed(header, 18, header_top, PANEL_W - 36, 18,
                  ("stretch", 18, 18))
        self._detail_views.append(header)
        self._group_top = 274 if self._judgment_enabled else 156

        # ---- 话术 groups: each dropdown heads a group and its candidates sit underneath,
        # so the tone is labelled by the thing that selects it. Every group's controls exist
        # from the start; _relayout() decides which are on screen. The button tags are slot
        # arithmetic (slot * PER_TONE + row) so they never shift when a group's results are
        # still in flight.
        tone_items = styles.labels() + [styles.NONE_LABEL]
        for slot in range(styles.MAX_SLOTS):
            group_box = self._make_surface(12, PALETTE["row"], PALETTE["edge"])
            view.addSubview_(group_box)
            self._group_boxes.append(group_box)

            box = self._make_surface(8, PALETTE["field"], PALETTE["edge"])
            view.addSubview_(box)
            self._dd_boxes.append(box)

            pop = NSPopUpButton.alloc().initWithFrame_pullsDown_(
                NSMakeRect(0, 0, TONE_DD_W - 2 * TONE_DD_INSET, TONE_DD_H), False)
            pop.setBordered_(False)          # <- no bezel, no accent-coloured chevron
            # the one discoverability aid the flat field gets: grey-on-grey reads as text,
            # a tooltip costs nothing visually and answers "can I click this?"
            pop.setToolTip_(f"点这里换沟通类型（每种各出 {self.candidate_count} 条）")
            pop.setFont_(NSFont.boldSystemFontOfSize_(TONE_DD_FONT))
            pop.setContentTintColor_(PALETTE["text"])
            pop.addItemsWithTitles_(tone_items)
            pop.selectItemWithTitle_(self.slot_tones[slot])
            pop.setTarget_(self)
            pop.setAction_("toneChanged:")
            view.addSubview_(pop)
            self._dds.append(pop)

            slot_rows = []
            for row in range(styles.PER_TONE):
                tag = slot * styles.PER_TONE + row
                row_box = self._make_surface(8, PALETTE["row"], PALETTE["edge"])
                row_box.setHidden_(True)
                view.addSubview_(row_box)
                prob = self._make_label(CAND_PROB_X, 0, CAND_PROB_W, 32,
                                        size=10, color=PALETTE["green"], bold=True)
                prob.cell().setWraps_(True)
                text = self._make_label(CAND_TEXT_X, 0, CAND_TEXT_W, 18,
                                        size=11, color=PALETTE["text"])
                text.cell().setWraps_(True)
                text.cell().setLineBreakMode_(AppKit.NSLineBreakByWordWrapping)
                if hasattr(text.cell(), "setMaximumNumberOfLines_"):
                    text.cell().setMaximumNumberOfLines_(0)
                copy_btn = self._make_button(CAND_BTN_X, 0, CAND_BTN_W, CAND_BTN_H,
                                             "复制", "copyCandidate:", tag)
                fill_btn = self._make_button(CAND_BTN_X + CAND_BTN_W + CAND_BTN_GAP, 0,
                                             CAND_BTN_W, CAND_BTN_H, "填入", "fillCandidate:", tag)
                adjust = NSPopUpButton.alloc().initWithFrame_pullsDown_(
                    NSMakeRect(CAND_TEXT_X, 0, 90, CAND_BTN_H), True)
                adjust.addItemsWithTitles_(["微调…", "缩短", "更自然", "更委婉"])
                adjust.setTarget_(self)
                adjust.setAction_("adjustCandidate:")
                adjust.setTag_(tag)
                adjust.setFont_(NSFont.systemFontOfSize_(10))
                adjust.setToolTip_("仅修改这一条候选回复")
                adjust.setAccessibilityLabel_("微调这条候选回复")
                track = self._make_surface(2, PALETTE["track"])
                fill_bar = self._make_surface(2, PALETTE["green"])
                track.setHidden_(True)
                fill_bar.setHidden_(True)
                for c in (prob, text, copy_btn, fill_btn, adjust, track, fill_bar):
                    view.addSubview_(c)
                slot_rows.append({"box": row_box, "prob": prob, "text": text,
                                  "btn": copy_btn, "fill_btn": fill_btn,
                                  "adjust": adjust,
                                  "track": track, "fill": fill_bar})
            self._rows.append(slot_rows)

        self.reanalyze_button = self._make_button(
            14, 0, 96, 28, "立刻分析", "reanalyze:", 0)
        self.reanalyze_button.setAccessibilityLabel_("立刻分析")
        self.reanalyze_button.setToolTip_("重新读取微信并执行完整分析")
        self.reanalyze_button.setHidden_(False)
        view.addSubview_(self.reanalyze_button)
        self._detail_views.append(self.reanalyze_button)

        self.regenerate_button = self._make_button(
            118, 0, PANEL_W - 132, 28, "重新生成推荐回答", "regenerateReply:", 0)
        self.regenerate_button.setAccessibilityLabel_("重新生成推荐回答")
        self.regenerate_button.setToolTip_("沿用当前消息和判断结果，只重新生成候选回答")
        self.regenerate_button.setHidden_(False)
        view.addSubview_(self.regenerate_button)
        self._detail_views.append(self.regenerate_button)

        self.panel.setContentView_(root)
        self._title_h = self.panel.frame().size.height - PANEL_H   # measured, not assumed
        self._relayout()
        self.rows["status"].setStringValue_(IDLE_STATUS)
        self._wire_window_controls()
        self._install_status_item()
        AppKit.NSWorkspace.sharedWorkspace().notificationCenter().addObserver_selector_name_object_(
            self, "accessibilityDisplayChanged:",
            AppKit.NSWorkspaceAccessibilityDisplayOptionsDidChangeNotification, None)
        self.applyAccessibilityAppearance_(None)

    @objc.python_method
    def _build_overlay(self):
        """A transparent, click-through window aligned to WeChat: the YOLO-style view.

        Pure visualization of what perception already returns — every message's bounding
        box and its real OCR confidence, the judged one carrying intent+risk on its chip.
        Three properties keep it safe: it is OFF by default (menu-bar toggle); clicks pass
        through (`ignoresMouseEvents`), so WeChat never gets blocked; and perception
        captures by window ID, so this window can never pollute our own OCR.
        Coordinate mapping is pure normalized geometry × window point size, so it is
        independent of the capture's pixel resolution (the old 1x-nominal assumption went
        away with #83's subprocess capture).
        """
        self._ov_panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 200, 200), NSWindowStyleMaskBorderless,
            NSBackingStoreBuffered, False)
        self._ov_panel.setLevel_(AppKit.NSFloatingWindowLevel)
        self._ov_panel.setOpaque_(False)
        self._ov_panel.setHasShadow_(False)
        self._ov_panel.setIgnoresMouseEvents_(True)   # never steal a click meant for WeChat
        self._ov_panel.setHidesOnDeactivate_(False)
        self._ov_panel.setBackgroundColor_(NSColor.clearColor())
        view = _BoxesView.alloc().init()
        view.boxes = []
        self._ov_panel.setContentView_(view)

    @objc.python_method
    def _slot_active(self, slot: int) -> bool:
        return self.slot_tones[slot] in styles.PRESETS

    @objc.python_method
    def _relayout(self):
        """Reflow every control for the live width and scroll within the live height.

        Before the user resizes, content keeps the historical auto-height behaviour. After
        a drag, their frame becomes authoritative: content grows inside the document and a
        vertical scroller appears when needed. Extra height first reveals more read-result
        text; extra width is given to message and candidate text columns.
        """
        frame = self.panel.frame()
        panel_w = max(PANEL_MIN_W, float(frame.size.width or PANEL_W))
        geometry = _candidate_geometry(panel_w)
        message_h, document_h, overflow, full_message_h = self._message_metrics(panel_w - 50)
        base_message_h = message_h
        message_delta = message_h - 26  # sender row now precedes the body
        dy = self._group_top + message_delta
        placements = []          # (control, x, dy_from_top, w, h)
        for slot in range(styles.MAX_SLOTS):
            active = self._slot_active(slot)
            self._group_boxes[slot].setHidden_(not active)
            group_top = dy
            selector_top = dy + (GROUP_PAD_Y if active else 0)
            placements.append((self._dd_boxes[slot], geometry["tone_x"], selector_top,
                               geometry["tone_w"], TONE_DD_H))
            placements.append((self._dds[slot], geometry["tone_x"] + TONE_DD_INSET,
                               selector_top,
                               geometry["tone_w"] - 2 * TONE_DD_INSET, TONE_DD_H))
            dy = selector_top + TONE_DD_H
            if active:
                dy += TONE_DD_GAP
            for row in range(styles.PER_TONE):
                r = self._rows[slot][row]
                controls = self._row_controls(slot, row)
                if (active and row < self.candidate_count
                        and self.cand_texts[slot * styles.PER_TONE + row]):
                    # The candidate decides its own height. Short replies keep the compact
                    # minimum; longer localized text grows without truncation.
                    text_h = self._candidate_text_height(r["text"], geometry["text_w"])
                    button_top = dy + max(34, text_h + 14)
                    row_h = max(CAND_ROW_MIN_H, button_top - dy + CAND_BTN_H + 6)
                    text_top = dy + 7
                    metric_top = dy + (row_h - 40) / 2
                    progress_w = max(0.0, min(36.0, r["fill"].frame().size.width))
                    placements += [
                        (r["box"], geometry["row_x"], dy, geometry["row_w"], row_h),
                        (r["text"], geometry["text_x"], text_top,
                         geometry["text_w"], text_h),
                        (r["prob"], geometry["prob_x"], metric_top,
                         geometry["prob_w"], 32),
                        (r["btn"], geometry["button_x"], button_top,
                         CAND_BTN_W, CAND_BTN_H),
                        (r["fill_btn"], geometry["button_x"] + CAND_BTN_W + CAND_BTN_GAP,
                         button_top,
                         CAND_BTN_W, CAND_BTN_H),
                        (r["adjust"], geometry["text_x"], button_top, 90, CAND_BTN_H),
                        (r["track"], geometry["prob_x"] + 4, metric_top + 36, 36, 4),
                        (r["fill"], geometry["prob_x"] + 4, metric_top + 36,
                         progress_w, 4),
                    ]
                    dy += row_h
                    if row < self.candidate_count - 1:
                        dy += CAND_ROW_GAP
                else:
                    for c in controls:
                        c.setHidden_(True)
            if active:
                dy += GROUP_PAD_Y
                placements.append((self._group_boxes[slot], geometry["group_x"], group_top,
                                   geometry["group_w"], dy - group_top))
            if slot < styles.MAX_SLOTS - 1:
                dy += GROUP_GAP

        # no setHidden_(False) here: the collapsed panel keeps _detail_views hidden, and
        # _render_groups() reaches this method without a collapsed guard
        placements.append((self.reanalyze_button, 14, dy, 96, 28))
        placements.append((self.regenerate_button, 118, dy, panel_w - 132, 28))
        dy += 28 + BOTTOM_PAD
        natural_h = dy
        viewport_h = float(self._content_scroll.contentSize().height or natural_h)
        auto_message_extra, content_h, outer_scroll = _viewport_fit(
            natural_h, viewport_h, message_h, full_message_h,
            self._user_sized and not self._collapsed)
        if auto_message_extra:
            message_h += auto_message_extra
            message_delta += auto_message_extra
            placements = [(ctrl, x, top + auto_message_extra, w, h)
                          for ctrl, x, top, w, h in placements]
            natural_h += auto_message_extra

        render_document_h = full_message_h if auto_message_extra else document_h
        self._message_toggle.setHidden_(not overflow or full_message_h <= message_h + .5)
        self._message_toggle.setTitle_("收起 ▴" if self._message_expanded else "展开 ▾")
        self._message_scroll.setHasVerticalScroller_(render_document_h > message_h + .5)
        field = self.rows["message"]
        field.setMaximumNumberOfLines_(
            0 if self._message_expanded or auto_message_extra else 2)
        field.setFrame_(NSMakeRect(0, 0, panel_w - 44, render_document_h))

        self._content_view.setFrameSize_(NSMakeSize(panel_w, content_h))
        self._content_scroll.setHasVerticalScroller_(outer_scroll)
        fixed = []
        for ctrl, x, top, w, h in self._fixed:
            if ctrl is self._message_surface:
                h += message_delta
            elif ctrl is self._message_scroll:
                h = message_h
            elif top >= 124:
                top += message_delta
            x, w = _responsive_xw(self._responsive_rules.get(id(ctrl)), panel_w, x, w)
            fixed.append((ctrl, x, top, w, h))
        for ctrl, x, top, w, h in placements + fixed:
            ctrl.setFrame_(NSMakeRect(x, top, w, h))
        for ctrl in self._judgment_views:
            ctrl.setHidden_(not self._judgment_enabled)

        if not self._user_sized and not self._collapsed:
            # Automatic layout before the first manual resize retains the old top-pinned
            # behaviour. Programmatic resize callbacks must not mark this as user-sized.
            top = frame.origin.y + frame.size.height
            frame_h = natural_h + self._title_h
            self._layout_resizing = True
            try:
                self.panel.setFrame_display_(
                    NSMakeRect(frame.origin.x, top - frame_h, panel_w, frame_h), True)
            finally:
                self._layout_resizing = False
            self._expanded_h = frame_h
            self._expanded_size = (panel_w, frame_h)
        elif not self._collapsed:
            self._expanded_h = frame.size.height
            self._expanded_size = (frame.size.width, frame.size.height)

    def windowDidResize_(self, notification):
        """Reflow live while the user drags either window edge."""
        if self._layout_resizing or not hasattr(self, "_content_scroll"):
            return
        frame = self.panel.frame()
        if self._collapsed:
            self._expanded_size = (frame.size.width, self._expanded_size[1])
        else:
            self._user_sized = True
            self._expanded_h = frame.size.height
            self._expanded_size = (frame.size.width, frame.size.height)
        self._relayout()

    def windowDidEndLiveResize_(self, notification):
        self._last_origin = None

    @objc.python_method
    def _wire_window_controls(self):
        """Native traffic lights, mapped to this app's actions.

        red    -> quit. A hidden panel would otherwise be unreachable: LSUIElement apps
                  have no Dock icon, so a plain order-out looks like a crash.
        yellow -> roll the panel up instead of miniaturizing, for the same reason.
        green  -> native zoom/restore for the now-resizable HUD.
        """
        close = self.panel.standardWindowButton_(NSWindowCloseButton)
        mini = self.panel.standardWindowButton_(NSWindowMiniaturizeButton)
        zoom = self.panel.standardWindowButton_(NSWindowZoomButton)
        if close:
            close.setTarget_(self)
            close.setAction_("quitApp:")
            close.setToolTip_("退出 jev-jarvis")
        if mini:
            mini.setTarget_(self)
            mini.setAction_("collapsePanel:")
            mini.setToolTip_("收起 / 展开面板")
        if zoom:
            zoom.setHidden_(False)
            zoom.setToolTip_("放大 / 恢复面板")

    @objc.python_method
    def _install_status_item(self):
        """Menu-bar item — the standard place for a background helper's controls."""
        bar = AppKit.NSStatusBar.systemStatusBar()
        self.status_item = bar.statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        self.status_item.button().setTitle_("J")
        self.status_item.button().setToolTip_("jev-jarvis · 微信 / QQ 意图助手")

        menu = AppKit.NSMenu.alloc().init()
        for title, action, key in (
            ("显示 / 收起面板", "collapsePanel:", ""),
            ("暂停读屏", "togglePause:", ""),
            ("YOLO 检测框", "toggleBoxes:", ""),
            ("固定在最前面", "toggleAlwaysOnTop:", ""),
            ("立即重新分析", "reanalyze:", ""),
            ("设置…", "openSettings:", ","),
            ("校准区域…", "calibrateMessages:", ""),
            ("恢复自动识别区域", "clearCalibration:", ""),
        ):
            menu.addItemWithTitle_action_keyEquivalent_(title, action, key)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        menu.addItemWithTitle_action_keyEquivalent_("退出 jev-jarvis", "quitApp:", "q")
        for item in menu.itemArray():
            item.setTarget_(self)
        self.pause_item = menu.itemArray()[1]
        self.boxes_item = menu.itemArray()[2]
        self.always_on_top_item = menu.itemArray()[3]
        self.boxes_item.setState_(
            AppKit.NSOnState if self._show_boxes else AppKit.NSOffState)
        self.always_on_top_item.setState_(
            AppKit.NSOnState if self._always_on_top else AppKit.NSOffState)
        self.status_item.setMenu_(menu)

    @objc.python_method
    def _retire_calibration_results(self):
        self._foreground_epoch += 1
        self._reply_epoch += 1
        self._gen_epoch += 1
        self._reply_key = None
        self.last_seen = self.analyzed_text = None
        self._prejudge_req = self._prejudge_result = None
        self._pregen_req = self._pregen_result = None
        self._fingerprint = self._last_full = self._layout_key = None
        self._empty_frame_since = None
        self._input_target = self._input_window = None
        self._stable_n = 0
        self._next_read_ts = 0

    def calibrateMessages_(self, sender):
        if self._calibrating:
            self.calibration_controller.window.makeKeyAndOrderFront_(None)
            return
        from calibration_ui import CalibrationController
        self._calibrating = True
        self._retire_calibration_results()
        self.applyWaiting_("正在校准消息区和输入区…")
        self.panel.orderOut_(None)
        self._ov_panel.orderOut_(None)
        try:
            self.calibration_controller = CalibrationController.alloc().init().build(
                self._calibration_finished, self._calibration_saved,
                saved_input=self._input_calibration_saved)
        except (ValueError,OSError) as e:
            self._calibrating = False
            self._show()
            self._render("status", str(e), PALETTE["red"])

    @objc.python_method
    def _calibration_finished(self, calibration, wid):
        if calibration is not None:
            message, editor = calibration
            self._input_calibration = editor
            self._input_calibration_wid = wid
            self._input_calibration_saved = editor.serialize()
            self._calibration = message
            self._calibration_wid = wid
            self._calibration_saved = message.serialize()
            self._calibration_required = True
        self._calibrating = False
        self._retire_calibration_results()
        self._show()
        self.applyWaiting_("校准完成；请回到微信。调整分栏后请重新校准。"
                           if calibration else "已取消本次校准")

    def clearCalibration_(self, sender):
        if self._calibrating: return
        from settings_config import read_document, write_settings
        path = userconfig.env_files()[0]
        try:
            write_settings(path,read_document(path),{"JEV_MESSAGE_REGION":"", "JEV_INPUT_REGION":""})
        except (ValueError,OSError) as e:
            self._render("status",str(e),PALETTE["red"])
            return
        self._calibration = None
        self._calibration_wid = None
        self._input_calibration = None
        self._input_calibration_wid = None
        self._input_calibration_saved = ""
        self._calibration_saved = ""
        self._calibration_required = False
        self._retire_calibration_results()
        self.applyWaiting_("已恢复自动识别区域")

    def openSettings_(self, sender):
        from settings import SettingsController
        if getattr(self, "settings_controller", None) and self.settings_controller.window.isVisible():
            self.settings_controller.show()
            return
        try:
            self.settings_controller = SettingsController.alloc().init().build(self)
            self.settings_controller.show()
        except OSError:
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_("无法读取配置文件，请检查文件权限。")
            alert.runModal()

    @objc.python_method
    def _make_surface(self, radius: float, color: NSColor,
                      border: NSColor | None = None) -> NSView:
        surface = ui_style.make_surface(radius, color, border)
        self._appearance_surfaces.append((surface, radius, color, border))
        return surface

    def accessibilityDisplayChanged_(self, notification):
        # Workspace notifications are independent of the polling/model workers.
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "applyAccessibilityAppearance:", None, False)

    def applyAccessibilityAppearance_(self, notification):
        reduced = AppKit.NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceTransparency()
        self._apply_accessibility_palette(bool(reduced))

    @objc.python_method
    def _apply_accessibility_palette(self, reduced):
        def adapted(color):
            if color is None or not reduced:
                return color
            for key, replacement in ui_style.SOLID_PALETTE.items():
                if color.isEqual_(PALETTE[key]):
                    return replacement
            return color  # risk/status colors retain their semantic meaning

        background = ui_style.SOLID_PALETTE["bg"] if reduced else NSColor.clearColor()
        self._solid_backdrop.layer().setBackgroundColor_(background.CGColor())
        for surface, radius, color, original_border in self._appearance_surfaces:
            layer = surface.layer()
            layer.setBackgroundColor_(adapted(color).CGColor())
            border = adapted(original_border)
            if reduced and color.isEqual_(PALETTE["surface"]):
                border = ui_style.SOLID_PALETTE["edge"]
            layer.setBorderWidth_(0.75 if border is not None else 0)
            if border is not None:
                layer.setBorderColor_(border.CGColor())
        for button in self._appearance_buttons:
            if button is self.settings_button or button is self.calibration_button:
                continue  # the gear stays an unboxed icon
            button.layer().setBackgroundColor_(adapted(PALETTE["row"]).CGColor())
            button.layer().setBorderColor_(adapted(PALETTE["edge"]).CGColor())
        self.panel.contentView().setNeedsDisplay_(True)

    @objc.python_method
    def _make_label(self, x, y, w, h, size=13, color=None, bold=False):
        return ui_style.make_label("", x, y, w, h, size, color, bold, selectable=True)

    @objc.python_method
    def _make_button(self, x, y, w, h, title, action, tag):
        """Compact native action with a light outline over the vibrancy material."""
        btn = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
        btn.setTitle_(title)
        ui_style.style_button(btn, font_size=10, radius=CAND_BTN_H / 2)
        btn.setTarget_(self)
        btn.setAction_(action)
        btn.setTag_(tag)
        btn.setHidden_(True)
        self._appearance_buttons.append(btn)
        return btn

    @objc.python_method
    def _show(self, force=False):
        # Background capture updates the same panel model while it is hidden. Do not let
        # an incoming message or a late model callback raise a global floating window over
        # another app unless the user explicitly disabled automatic hiding. Model download
        # progress may pass force=True to preserve its existing always-visible behaviour.
        if (not force and self._wechat_frontmost is False
                and getattr(self, "auto_hide", True)):
            return
        if not self.panel.isVisible():
            self.panel.orderFrontRegardless()

    def applySettings_(self, changes):
        """Apply a persisted settings save to the running HUD immediately."""
        if not changes:
            return
        background_capture_was = getattr(self, "background_capture", False)
        self.generation_context_turns = settings_config.context_turns(
            "GENERATION_CONTEXT_TURNS")
        self.judge_context_turns = settings_config.context_turns("JUDGE_CONTEXT_TURNS")
        self.auto_hide = settings_config.bool_setting("JEV_AUTO_HIDE")
        self.auto_dock = settings_config.bool_setting("JEV_AUTO_DOCK")
        self.background_capture = settings_config.bool_setting("JEV_BACKGROUND_CAPTURE")
        self.panel_always_on_top = settings_config.bool_setting(
            "JEV_PANEL_ALWAYS_ON_TOP")

        tone_keys = {f"JEV_DEFAULT_TONE_{index}"
                     for index in range(1, styles.MAX_SLOTS + 1)}
        count_keys = {"JEV_CANDIDATES_PER_TONE"}
        reply_profile_keys = tone_keys | count_keys | set(settings_config.CONTEXT_KEYS)
        provider_changed = any(key.startswith(("OPENAI_", "ANTHROPIC_"))
                               for key in changes)
        judge_changed = any(key.startswith("TYPESAFE_") or key == "JUDGE_BACKEND"
                            for key in changes)
        tone_changed = bool(set(changes) & tone_keys)
        count_changed = bool(set(changes) & count_keys)
        reply_profile_changed = bool(set(changes) & reply_profile_keys)

        if provider_changed:
            self.generator = Generator()
        if judge_changed:
            self.judge = make_judge()
            enabled = getattr(self.judge, "enabled", True)
            if enabled != self._judgment_enabled:
                self._judgment_enabled = enabled
                self._group_top = 274 if enabled else 156
                header_top = 250 if enabled else 132
                self._fixed = [
                    (ctrl, x, header_top if ctrl is self.rows["cand_header"] else top,
                     w, h)
                    for ctrl, x, top, w, h in self._fixed
                ]
                for view in self._judgment_views:
                    view.setHidden_(not enabled or self._collapsed)
            threading.Thread(target=self._warm, daemon=True).start()

        if count_changed:
            self.candidate_count = settings_config.candidate_count()
        if tone_changed:
            wanted = settings_config.default_tones(
                styles.PRESETS, styles.NONE_LABEL, styles.MAX_SLOTS)
            self.slot_tones = wanted
            for index, popup in enumerate(self._dds):
                popup.selectItemWithTitle_(wanted[index])
        if count_changed:
            for popup in self._dds:
                popup.setToolTip_(
                    f"点这里换沟通类型（每种各出 {self.candidate_count} 条）")

        reply_changed = provider_changed or judge_changed or reply_profile_changed
        if reply_changed:
            self._reply_epoch += 1
            self._prejudge_req = self._prejudge_result = None
            self._pregen_req = self._pregen_result = None
            self._gen_epoch += 1
            self._clear_candidates()
            self._stream_rows = {}
            self._relayout()
        if "JEV_AUTO_DOCK" in changes:
            self._last_origin = None
            self._pending_origin = None
        if "JEV_PANEL_ALWAYS_ON_TOP" in changes:
            self._always_on_top = self.panel_always_on_top
            self.panel.setLevel_(AppKit.NSFloatingWindowLevel
                                 if self._always_on_top
                                 else AppKit.NSNormalWindowLevel)
        if "JEV_BACKGROUND_CAPTURE" in changes and self._wechat_frontmost is False:
            if background_capture_was and not self.background_capture:
                # Re-enter the original hard foreground boundary immediately: retire any
                # background generation already in flight and clear its cached frame.
                self._set_foreground_state(None)
            elif self.background_capture:
                self._next_read_ts = 0

        if not self.auto_hide:
            self._show()
        elif self._wechat_frontmost is False and self.panel.isVisible():
            self.panel.orderOut_(None)
        if reply_changed and self.analyzed_text:
            self._regenerate()

    @objc.python_method
    def _context_line(self, sender, prev: str) -> str:
        parts = []
        if sender:
            parts.append(f"来自 {sender}")
        if prev:
            parts.append(f"上文：{prev[:26]}")
        return " · ".join(parts)

    @objc.python_method
    def _format_read_result(self, messages) -> str:
        """Human-readable OCR output shown verbatim in the latest-message card."""
        labels = {"them": "对方", "me": "我", "public": "公共信息",
                  "unknown": "方向未确认"}
        lines = []
        for message in messages:
            text = " / ".join(part.strip() for part in message.text.splitlines() if part.strip())
            if getattr(message, "visual_only", False):
                text = "⚠ " + text
            label = labels.get(message.side, "方向未确认")
            # A group member's display name is part of the attribution, not part of
            # the message body.  Showing it beside “对方” keeps similarly worded
            # messages from different members distinguishable without feeding a
            # duplicated nickname to the judge/generator.
            if message.side == "them" and message.sender:
                label += f"（{message.sender}）"
            lines.append(f"{label}｜{text}")
            if message.quote_text:
                lines.append(f"  ↳ 引用（{message.quote_sender or '对方'}）｜"
                             f"{' / '.join(message.quote_text.splitlines())}")
        return "\n".join(lines) if lines else "（未读到聊天消息）"

    @objc.python_method
    def _target_text(self, message) -> str:
        return getattr(message, "analysis_text", None) or message.text

    @objc.python_method
    def _judge_text(self, message) -> str:
        return getattr(message, "analysis_body", None) or message.text

    @objc.python_method
    def _read_result_meta(self, count: int, suffix: str = "") -> str:
        return f"读屏结果 · {count} 条" + (f" · {suffix}" if suffix else "")

    @objc.python_method
    def _render(self, key: str, text: str, color: NSColor | None = None):
        if key == "status":
            self._normal_status = (text, color)
            status = self.judge.load_status
            # A red error line stays visible: a concurrent progress status must not
            # repaint over it, and _normal_status keeps it after the load ends.
            if status and color is not PALETTE["red"]:
                text, color = status, PALETTE["amber"]
        tf = self.rows[key]
        if key == "message" and text != self._message_text:
            self._message_expanded = False
            self._message_text = text
        tf.setStringValue_(text)
        if key == "message" and not self._collapsed:
            self._relayout()
            tf.scrollRectToVisible_(NSMakeRect(0, max(0, tf.frame().size.height - 1), 1, 1))
        if color is not None:
            tf.setTextColor_(color)

    @objc.python_method
    def _message_metrics(self, text_width: float | None = None):
        field = self.rows["message"]
        width = max(80.0, float(text_width or (self.panel.frame().size.width - 50)))
        attributed = NSAttributedString.alloc().initWithString_attributes_(
            field.stringValue() or " ", {NSFontAttributeName: field.font()})
        bounds = attributed.boundingRectWithSize_options_(
            NSMakeSize(width, 100000),
            AppKit.NSStringDrawingUsesLineFragmentOrigin | AppKit.NSStringDrawingUsesFontLeading)
        two_lines = NSAttributedString.alloc().initWithString_attributes_(
            "国\n国", {NSFontAttributeName: field.font()})
        two_bounds = two_lines.boundingRectWithSize_options_(
            NSMakeSize(width, 100000),
            AppKit.NSStringDrawingUsesLineFragmentOrigin | AppKit.NSStringDrawingUsesFontLeading)
        collapsed = float(int(two_bounds.size.height + 5.999))
        full = max(collapsed, float(int(bounds.size.height + 5.999)))
        overflow = full > collapsed
        document = full if self._message_expanded else collapsed
        return min(180, document), document, overflow, full

    def toggleMessage_(self, sender):
        self._message_expanded = not self._message_expanded
        self._relayout()
        field = self.rows["message"]
        field.scrollRectToVisible_(NSMakeRect(0, max(0, field.frame().size.height - 1), 1, 1))

    @objc.python_method
    def _set_candidate_header(self, text: str):
        """Keep the section title strong while treating its live status as metadata."""
        value = NSMutableAttributedString.alloc().initWithString_(text)
        value.addAttributes_range_({
            NSFontAttributeName: NSFont.systemFontOfSize_(12),
            NSForegroundColorAttributeName: PALETTE["muted"],
        }, NSMakeRange(0, len(text)))
        title_len = min(len("候选回复"), len(text))
        value.addAttributes_range_({
            NSFontAttributeName: NSFont.boldSystemFontOfSize_(12),
            NSForegroundColorAttributeName: PALETTE["text"],
        }, NSMakeRange(0, title_len))
        self.rows["cand_header"].setAttributedStringValue_(value)

    @objc.python_method
    def _set_probability_label(self, field: NSTextField, row: int, probability: str):
        """Match the reference hierarchy: quiet rank, vivid bold probability."""
        rank = f"#{row + 1}"
        text = f"{rank}\n{probability}"
        value = NSMutableAttributedString.alloc().initWithString_(text)
        value.addAttributes_range_({
            NSFontAttributeName: NSFont.systemFontOfSize_(9),
            NSForegroundColorAttributeName: PALETTE["muted"],
        }, NSMakeRange(0, len(text)))
        value.addAttributes_range_({
            NSFontAttributeName: NSFont.boldSystemFontOfSize_(11),
            NSForegroundColorAttributeName: (
                PALETTE["muted"] if probability == "排序中" else PALETTE["green"]),
        }, NSMakeRange(len(rank) + 1, len(probability)))
        field.setAttributedStringValue_(value)

    @objc.python_method
    def _candidate_text_height(self, field: NSTextField,
                               text_width: float | None = None) -> float:
        """Measure the full rendered reply so layout never relies on a character cutoff."""
        text = field.stringValue()
        if not text:
            return 18
        attributed = NSAttributedString.alloc().initWithString_attributes_(
            text, {NSFontAttributeName: field.font()})
        options = (AppKit.NSStringDrawingUsesLineFragmentOrigin
                   | AppKit.NSStringDrawingUsesFontLeading)
        bounds = attributed.boundingRectWithSize_options_(
            NSMakeSize(max(40.0, float(text_width or CAND_TEXT_W)), 10_000), options)
        return max(18, float(int(bounds.size.height + 4.999)))

    @objc.python_method
    def _set_progress(self, slot: int, row: int, value: float | None):
        """Paint the existing rank probability; the model payload is never changed."""
        r = self._rows[slot][row]
        progress = max(0.0, min(1.0, float(value or 0.0)))
        frame = r["fill"].frame()
        r["fill"].setFrameSize_(NSMakeSize(36 * progress, frame.size.height or 4))

    @objc.python_method
    def _set_risk_scale(self, risk: int | None):
        selected = None if risk is None else (0 if risk <= 3 else (1 if risk <= 6 else 2))
        colors = (PALETTE["green"], PALETTE["amber"], PALETTE["red"])
        for i, dot in enumerate(self._risk_dots):
            layer = dot.layer()
            layer.removeAnimationForKey_("risk-breathe")
            alpha = 1.0 if i == selected else 0.68
            layer.setBackgroundColor_(colors[i].colorWithAlphaComponent_(alpha).CGColor())
            layer.setShadowOpacity_(0.0)
            if i == selected:
                layer.setShadowColor_(colors[i].CGColor())
                layer.setShadowOffset_(NSMakeSize(0, 0))
                layer.setShadowRadius_(4.0)
                layer.setShadowOpacity_(0.55)
                # Keep the dot itself fully saturated; only its halo breathes.
                pulse = Quartz.CABasicAnimation.animationWithKeyPath_("shadowOpacity")
                pulse.setFromValue_(0.24)
                pulse.setToValue_(0.72)
                pulse.setDuration_(1.2)
                pulse.setAutoreverses_(True)
                pulse.setRepeatCount_(float("inf"))
                pulse.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_(
                    Quartz.kCAMediaTimingFunctionEaseInEaseOut))
                layer.addAnimation_forKey_(pulse, "risk-breathe")

    @objc.python_method
    def _row_controls(self, slot: int, row: int):
        r = self._rows[slot][row]
        return (r["box"], r["prob"], r["text"], r["btn"], r["fill_btn"],
                r["adjust"],
                r["track"], r["fill"])

    @objc.python_method
    def _render_groups(self, payload: list):
        """payload: [(slot, tone, [{"text","prob"}, ...]), ...] — one entry per active tone.

        Rows the model did not fill are emptied and their buttons hidden. Every active tone
        still reserves its two minimum rows, while a returned long reply expands only its own
        row so the complete text remains visible.
        """
        wanted = set()
        for slot, _tone, items in payload:
            for row in range(self.candidate_count):
                if row < len(items):
                    it = items[row]
                    wanted.add((slot, row))
                    r = self._rows[slot][row]
                    source = it["text"]
                    shown = self._candidate_overrides.get((slot, source), source)
                    prob = ("已微调" if shown != source else
                            "原序" if not self._judgment_enabled else
                            "排序中" if it["prob"] is None else f"{it['prob'] * 100:.0f}%")
                    self._set_probability_label(r["prob"], row, prob)
                    r["text"].setStringValue_(shown)
                    self._set_progress(slot, row, None if shown != source else it["prob"])
                    for c in self._row_controls(slot, row):
                        c.setHidden_(not self._slot_active(slot))
                    if not self._judgment_enabled or shown != source:
                        r["track"].setHidden_(True)
                        r["fill"].setHidden_(True)
                    tag = slot * styles.PER_TONE + row
                    self.cand_texts[tag] = shown
                    self._candidate_sources[tag] = source
        for slot in range(styles.MAX_SLOTS):
            for row in range(styles.PER_TONE):
                if (slot, row) not in wanted:
                    r = self._rows[slot][row]
                    r["prob"].setStringValue_("")
                    r["text"].setStringValue_("")
                    self._set_progress(slot, row, None)
                    for c in self._row_controls(slot, row):
                        c.setHidden_(True)
                    self.cand_texts[slot * styles.PER_TONE + row] = None
                    self._candidate_sources[slot * styles.PER_TONE + row] = None
        self._relayout()

    @objc.python_method
    def _clear_candidates(self):
        self._candidate_overrides = {}
        self._adjust_seq = {}
        for slot in range(styles.MAX_SLOTS):
            for row in range(styles.PER_TONE):
                r = self._rows[slot][row]
                r["prob"].setStringValue_("")
                r["text"].setStringValue_("")
                self._set_progress(slot, row, None)
                for c in self._row_controls(slot, row):
                    c.setHidden_(True)
                self.cand_texts[slot * styles.PER_TONE + row] = None
                self._candidate_sources[slot * styles.PER_TONE + row] = None

    @objc.python_method
    def _display_height(self) -> float:
        """Height of the display whose origin is (0,0) — the Quartz<->Cocoa flip constant.

        Taking this from the *target* screen is wrong on multi-display setups: a screen
        placed above the main one has origin.y > 0 and the flip must still use the
        primary display's height.
        """
        for scr in NSScreen.screens():
            f = scr.frame()
            if f.origin.x == 0 and f.origin.y == 0:
                return f.size.height
        return NSScreen.mainScreen().frame().size.height

    @objc.python_method
    def _position_near(self, win: dict | None):
        """Dock the panel beside WeChat, on the screen WeChat is actually on.

        Uses global Cocoa coordinates throughout. NSScreen.mainScreen() must NOT be used:
        it follows whichever display holds the key window, so relying on it made the panel
        hop ~1369 px between displays a few times a minute.
        """
        if not getattr(self, "auto_dock", True):
            return
        flip = self._display_height()
        panel_h = self.panel.frame().size.height or PANEL_H
        panel_w = self.panel.frame().size.width or PANEL_W
        screens = list(NSScreen.screens())
        primary = next((s for s in screens
                        if s.frame().origin.x == 0 and s.frame().origin.y == 0), screens[0])

        if win:
            # CGWindow bounds are top-left origin global pixels -> Cocoa bottom-left
            wx, wy = win["x"], win["y"]
            ww, wh = win["w"], win["h"]
            cx_win = wx + ww / 2.0
            cyan = flip - (wy + wh / 2.0)
            host = next((s for s in screens
                         if s.frame().origin.x <= cx_win <= s.frame().origin.x + s.frame().size.width
                         and s.frame().origin.y <= cyan <= s.frame().origin.y + s.frame().size.height),
                        primary)
            sf = host.frame()
            # dock right of WeChat if it fits on that screen, else left, else its right edge
            x = wx + ww + 8
            if x + panel_w > sf.origin.x + sf.size.width:
                x = wx - panel_w - 8
            if x < sf.origin.x:
                x = sf.origin.x + sf.size.width - panel_w - 12
            y = flip - wy - panel_h
            y = max(sf.origin.y + 40, min(y, sf.origin.y + sf.size.height - panel_h - 40))
        else:
            sf = primary.frame()
            x = sf.size.width - panel_w - 12
            y = sf.size.height - panel_h - 60

        # dead-band: ignore sub-2pt corrections and one-off blips, so WeChat's own window
        # animations (and our own numeric noise) stop nudging the panel around
        target = (round(x), round(y))
        last = self._last_origin
        if last is None:                    # first placement: apply without debounce
            self._last_origin = target
            self._pending_origin = target
            self.panel.setFrameOrigin_(target)
            return
        if abs(target[0] - last[0]) <= 2 and abs(target[1] - last[1]) <= 2:
            return
        if target != self._pending_origin:
            self._pending_origin = target
            return  # require the same target on two consecutive ticks before moving
        self._last_origin = target
        self.panel.setFrameOrigin_(target)

    # ------------------------------------------------------------ actions
    def copyCandidate_(self, sender):
        text = self.cand_texts[sender.tag()] if 0 <= sender.tag() < len(self.cand_texts) else None
        if not text:
            return
        if getattr(self, "_wechat_frontmost", None) is not True:
            self._render("status", "填入失败：微信 / QQ 不在前台", PALETTE["red"])
            return
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(text, NSPasteboardTypeString)
        self._render("status", "已复制", PALETTE["green"])

    def adjustCandidate_(self, sender):
        direction = sender.titleOfSelectedItem()
        if direction not in ("缩短", "更自然", "更委婉"):
            return
        tag = sender.tag()
        if not 0 <= tag < len(self.cand_texts):
            return
        original = self._candidate_sources[tag]
        shown = self.cand_texts[tag]
        if not original or not shown or not self.analyzed_text:
            return
        slot = tag // styles.PER_TONE
        key = (slot, original)
        sequence = self._adjust_seq.get(key, 0) + 1
        self._adjust_seq[key] = sequence
        self._render("status", f"正在{direction}这条候选…", PALETTE["muted"])
        threading.Thread(target=self._reply_task,
                         args=(self._reply_epoch, self._adjust_work,
                               self.analyzed_text, shown, direction, slot,
                               original, sequence, self.slot_tones[slot]),
                         daemon=True).start()

    @objc.python_method
    def _adjust_work(self, message, shown, direction, slot, original, sequence, tone):
        try:
            revised = self.generator.refine_candidate(message, shown, direction)
            self._push("applyAdjusted:",
                       (slot, original, shown, revised, sequence, tone))
        except Exception as exc:
            self._push("applyError:", f"微调失败：{str(exc)[:50]}")

    def applyAdjusted_(self, payload):
        slot, original, shown, revised, sequence, tone = payload
        key = (slot, original)
        if (self._adjust_seq.get(key) != sequence
                or self.slot_tones[slot] != tone):
            return
        for row in range(styles.PER_TONE):
            tag = slot * styles.PER_TONE + row
            if self._candidate_sources[tag] != original or self.cand_texts[tag] != shown:
                continue
            self._candidate_overrides[key] = revised
            self.cand_texts[tag] = revised
            r = self._rows[slot][row]
            r["text"].setStringValue_(revised)
            self._set_probability_label(r["prob"], row, "已微调")
            self._set_progress(slot, row, None)
            r["track"].setHidden_(True)
            r["fill"].setHidden_(True)
            self._relayout()
            self._render("status", "已微调这条候选", PALETTE["green"])
            return

    def fillCandidate_(self, sender):
        """Write the candidate into the current chat app's input box (via self._app)."""
        if getattr(self,"_calibration_required",False) and self._input_calibration is None:
            self._render("status", "请点击右上角校准图标，确认消息区和输入区。", PALETTE["amber"])
            return
        idx = sender.tag()
        text = self.cand_texts[idx] if 0 <= idx < len(self.cand_texts) else None
        if not text:
            return
        # The status line is painted before the call because writing into the chat app
        # takes a beat; the click should look instant even though the write has not
        # happened yet.
        self._render("status", "填入中…", PALETTE["muted"])
        self.panel.displayIfNeeded()
        app = self._app
        if app is None:
            self._render("status", "填入失败：微信 / QQ 不在前台", PALETTE["red"])
            return
        if not fill.has_accessibility():
            # First click is the moment to ask: the system dialog is the only way in.
            fill.request_accessibility()
        target = getattr(self, "_input_target", None)
        if target is None or (target["box"] is None and not target.get("visual_rect")):
            self._render("status", "填入失败：" + (target["reason"] if target else "等待输入框定位"), PALETTE["red"])
            return
        if target.get("app") != app.key:
            # 适配器刚切换、检测框还没更新：目标矩形仍属于上一个 App，写进去会
            # 填错应用——拒绝并让下一轮 locate_input 刷新目标。
            self._render("status", "填入失败：输入目标属于另一应用，请等检测框更新后重试", PALETTE["red"])
            return
        ok, reason = app.fill_text(text, target=target)
        if ok:
            self._render("status", reason, PALETTE["green"])
        else:
            self._render("status", f"填入失败：{reason}", PALETTE["red"])

    def toneChanged_(self, sender):
        """A 话术 dropdown moved: the verdict is still valid, only the writing changes."""
        picked = [p.titleOfSelectedItem() or styles.NONE_LABEL for p in self._dds]
        if picked == self.slot_tones:
            return
        self.slot_tones = picked
        self._reply_epoch += 1
        self._gen_epoch += 1
        self._prejudge_req = self._prejudge_result = None
        self._pregen_req = self._pregen_result = None
        if not self.analyzed_text:
            self.last_seen = None
        # the panel is sized by how many slots are in use, so re-lay-out *before* the new
        # candidates arrive: the empty rows appear at once and nothing jumps later
        self._clear_candidates()
        self._stream_rows = {}     # the run _regenerate starts streams into fresh rows
        self._regenerate()

    @objc.python_method
    def _regenerate(self):
        """Re-run just the generation half for the message on screen.

        No re-judging and no re-reading of the screen: the intent and risk do not depend on
        the tone, and re-running them would make a dropdown click feel like a new analysis.
        """
        self._relayout()
        text = self.analyzed_text
        if not text:
            self._render("status", "话术已选 · 下条消息生效", PALETTE["muted"])
            return
        active = [t for t in self.slot_tones if t in styles.PRESETS]
        if not active:
            self._render("status", "没选话术 · 至少选一个", PALETTE["amber"])
            return
        self._render("status", f"换话术中…（{'、'.join(active)}）", PALETTE["muted"])
        self._set_candidate_header("候选回复 · 生成中…")
        threading.Thread(target=self._reply_task,
                         args=(self._reply_epoch, self._regen_work,
                               text, self._last_intent, list(self.slot_tones),
                               self._analyzed_body or text),
                         daemon=True).start()

    @objc.python_method
    def _payload_from_gen(self, gen: dict):
        """Generation result -> unranked [(slot, tone, items)] (prob=None ⇒ 待排序).

        None when nothing usable came back — the caller shows gen's error then.
        """
        groups = [g for g in (gen.get("groups") or []) if g.get("texts")]
        if not groups:
            return None
        return [(g["slot"], g["tone"], [{"text": t, "prob": None} for t in g["texts"]])
                for g in groups]

    @objc.python_method
    def _rank_payload(self, payload: list, message: str, intent: str) -> list:
        """Score and reorder each group's candidates. One ranking pass covers every
        candidate the requests produced, so `#1`/`#2` inside a group means "the better of
        these two", not "whichever line the model wrote first" — one forward pass, not one
        per tone. Ranking failure leaves probabilities at 0 rather than dropping rows.
        """
        texts = [it["text"] for _s, _t, items in payload for it in items]
        scores: dict[str, float] = {}
        if intent and texts:
            try:
                with self._model_lock:   # never two local forwards at once
                    if not self._reply_current():
                        return payload
                    context = getattr(self._reply_worker, "context", self._active_context)
                    ranked = self.judge.rank_candidates(
                        chat_context.model_message(message, context), intent, texts, context=context)
                scores = {r["text"]: r["prob"] for r in ranked}
            except Exception:
                scores = {}
        out = []
        for slot, tone, items in payload:
            scored = [{"text": it["text"], "prob": scores.get(it["text"], 0.0)}
                      for it in items]
            scored.sort(key=lambda x: -x["prob"])
            out.append((slot, tone, scored))
        return out

    @objc.python_method
    def _stream_hook(self, t0: float, label: str = ""):
        """The on_candidate callback for the generation run starting now.

        Shared by all three run starters (_analyze, _run_generation, _regen_work) so the
        streaming lines follow one epoch/rows discipline no matter which path produced
        them. The callback runs on the run's worker thread; it hops to the main thread for
        every UI touch, and the first line it sees logs the latency that streaming is here
        for. The epoch check inside applyStreamLine_ is what makes a superseded run's late
        lines harmless.
        """
        reply_epoch = getattr(self._reply_worker, "epoch", self._reply_epoch)
        self._gen_epoch += 1
        epoch = self._gen_epoch
        prefix = f"{label} " if label else ""
        first_line = {"shown": False}

        def on_candidate(slot: int, _tone: str, text: str) -> None:
            if not first_line["shown"]:
                first_line["shown"] = True
                _log(f"{prefix}首条候选上屏 {(time.perf_counter() - t0) * 1000:.0f}ms（未排序）")
            self._push_reply("applyStreamLine:", (epoch, slot, text), reply_epoch)
        return on_candidate

    @objc.python_method
    def _regen_work(self, text: str, intent: str, slot_tones: list[str],
                    rank_text: str | None = None):
        t0 = time.perf_counter()
        try:
            if not self._reply_current():
                return
            context = getattr(self._reply_worker, "context", self._active_context)
            gen = self.generator.generate(
                chat_context.model_message(text, context), intent, slot_tones, context,
                self._stream_hook(t0, "换话术"),
                candidate_count=self.candidate_count)
            groups = gen.get("groups") or []
            failed = [str(g["slot"]) for g in groups if g.get("error")]
            _log(f"换话术 生成 {gen.get('elapsed_s', 0) * 1000:.0f}ms · {len(groups)} 个话术"
                 + (f" · 失败: {'; '.join(failed)}" if failed else ""))
            payload = self._payload_from_gen(gen)
            if payload is None:
                err = "服务未返回可用候选，请检查模型设置"
                _log(f"换话术无可用候选: {err}")
                self._push("applyError:", f"候选生成失败: {err}")
                return
            # streamed endpoints already showed the lines; this push only matters for the
            # non-streaming shape (anthropic), which has no applyStreamLine_ at all
            self._push("applyTones:", payload)
            ranked = self._rank_payload(payload, rank_text or text, intent) if intent else payload
            _log(f"换话术 端到端 {(time.perf_counter() - t0) * 1000:.0f}ms")
            self._push("applyTones:", ranked)
        except Exception as e:
            _log(f"换话术失败 {type(e).__name__}")
            self._push("applyError:", f"换话术失败: {type(e).__name__}")

    @objc.python_method
    def _regenerate_work(self, text: str, intent: str, slot_tones: list[str]):
        """Refresh candidate replies atomically, without re-reading or re-judging."""
        t0 = time.perf_counter()
        try:
            if not self._reply_current():
                return
            context = getattr(self._reply_worker, "context", self._active_context)
            gen = self.generator.generate(
                chat_context.model_message(text, context), intent, slot_tones, context,
                candidate_count=self.candidate_count)
            payload = self._payload_from_gen(gen)
            if payload is None:
                err = "服务未返回可用候选，请检查模型设置"
                _log(f"重新生成无可用候选: {err}")
                self._push("applyError:", f"重新生成失败: {err}")
                return
            ranked = self._rank_payload(payload, text, intent)
            _log(f"重新生成推荐回答 {(time.perf_counter() - t0) * 1000:.0f}ms · "
                 f"{sum(len(items) for _s, _t, items in ranked)} 条候选")
            self._push("applyRegenerated:", ranked)
        except Exception as e:
            _log(f"重新生成失败 {type(e).__name__}")
            self._push("applyError:", f"重新生成失败: {type(e).__name__}")
        finally:
            self._regenerating = False

    @objc.python_method
    def _payload_current(self, payload) -> bool:
        """False when the tone selection moved on — a late result must not repaint it.

        Generation+ranking now pushes twice (unranked, then ranked); a dropdown click
        between the two would otherwise bring back the tone the user just switched away
        from. Same guard for a 换话术 result racing a second click.
        """
        return all(self.slot_tones[slot] == tone for slot, tone, _items in payload)

    @objc.python_method
    def _cand_header(self, payload) -> str:
        if not self._judgment_enabled:
            return "候选回复（生成顺序）"
        pending = any(it["prob"] is None for _s, _t, items in payload for it in items)
        return "候选回复 · 排序中…" if pending else "候选回复（按合适度排序）"

    def applyTones_(self, payload):
        if not self._payload_current(payload):
            return
        self._set_candidate_header(self._cand_header(payload))
        total = sum(len(items) for _s, _t, items in payload)
        self._render("status", f"已换话术 · {total} 条", PALETTE["muted"])
        self._render_groups(payload)

    def applyRegenerated_(self, payload):
        if not self._payload_current(payload):
            return
        self._set_candidate_header(self._cand_header(payload))
        total = sum(len(items) for _s, _t, items in payload)
        self._render_groups(payload)
        self._render("status", f"已重新生成 · {total} 条", PALETTE["muted"])

    # ------------------------------------------------------------ controls
    def regenerateReply_(self, sender):
        """Regenerate candidates for the current message without re-reading or judging."""
        if self._paused:
            self._render("status", "已暂停 · 请先继续读屏", PALETTE["amber"])
            return
        if self._app is None:
            # The panel floats over every app: clicked from elsewhere this run would be
            # discarded by _reply_current() with no status update to say so — ask here.
            self._render("status", "微信 / QQ 不在前台 · 回到聊天窗口再试", PALETTE["amber"])
            return
        if self._regenerating:
            self._render("status", "推荐回答生成中…", PALETTE["muted"])
            return
        text = self.analyzed_text
        messages = (self._last_full or {}).get("messages") or []
        newest = next((m for m in reversed(messages)
                       if m.side == "them" and m.text == text), None)
        active = [tone for tone in self.slot_tones if tone in styles.PRESETS]
        if not text or newest is None or self._reply_key is None:
            self._render("status", "当前没有可重新生成的推荐回答", PALETTE["amber"])
            return
        if not active:
            self._render("status", "没选话术 · 至少选一个", PALETTE["amber"])
            return
        self._regenerating = True
        self._set_candidate_header("候选回复 · 重新生成中…")
        self._render("status", "重新生成推荐回答…", PALETTE["muted"])
        _log("重新生成推荐回答 · 已请求")
        threading.Thread(target=self._reply_task,
                         args=(self._reply_epoch, self._regenerate_work,
                               text, self._last_intent, list(self.slot_tones)),
                         daemon=True).start()

    def collapsePanel_(self, sender):
        self._set_collapsed(not self._collapsed)

    def togglePause_(self, sender):
        self._paused = not self._paused
        self.pause_item.setTitle_("继续读屏" if self._paused else "暂停读屏")
        if self._paused:
            self._prejudge_req = None        # a paused app judges nothing further
            self._prejudge_result = None
            self._pregen_req = None          # …and generates nothing further
            self._pregen_result = None
            if self._ov_panel.isVisible():   # frozen boxes would lie about "realtime"
                self._ov_panel.orderOut_(None)
            self._render("status", "已暂停 · 不再读屏", PALETTE["amber"])
            self._render("message", "", PALETTE["text"])
            self._render("sender", "", PALETTE["muted"])
            self._render("intent", "—", PALETTE["muted"])
            self._render("confidence", "", PALETTE["muted"])
            self._render("risk", "", PALETTE["muted"])
            if hasattr(self, "_risk_dots"):
                self._set_risk_scale(None)
            self._render("actions", "", PALETTE["text"])
            self.rows["cand_header"].setStringValue_("")
            self._clear_candidates()
        else:
            self._prejudge_result = None
            self.last_seen = None      # force a fresh read of whatever is on screen
            self.analyzed_text = None
            self._analyzed_body = None
            self._render("status", "已恢复 · 读屏中", PALETTE["muted"])

    def toggleAlwaysOnTop_(self, sender):
        """Toggle only the HUD's window level; the menu action takes effect immediately."""
        self._always_on_top = not self._always_on_top
        self.panel.setLevel_(AppKit.NSFloatingWindowLevel if self._always_on_top
                             else AppKit.NSNormalWindowLevel)
        self.always_on_top_item.setState_(
            AppKit.NSOnState if self._always_on_top else AppKit.NSOffState)
        if self._always_on_top:
            # Raise the already-visible panel without making it key or stealing WeChat focus.
            self.panel.orderFrontRegardless()

    def reanalyze_(self, sender):
        if self._paused:
            self._render("status", "已暂停 · 请先继续读屏", PALETTE["amber"])
            return
        self._reply_epoch += 1
        self._gen_epoch += 1
        self._prejudge_req = None      # "re-analyze" means re-run, not reuse the pre-judge
        self._prejudge_result = None
        self._pregen_req = None        # …and not reuse the early generation either
        self._pregen_result = None
        self.last_seen = None
        self.analyzed_text = None
        self._stream_rows = {}
        self._fingerprint = None
        self._stable_n = 0
        self._next_read_ts = 0
        self._render("status", "重新分析中…", PALETTE["muted"])
        if not self._paused and not self._busy:
            self._busy = True
            threading.Thread(target=self._work, daemon=True).start()

    def quitApp_(self, sender):
        AppKit.NSApplication.sharedApplication().terminate_(None)

    @objc.python_method
    def _set_collapsed(self, collapsed: bool):
        """Roll the panel up to a title+status strip, or back to full height."""
        rect = self.panel.frame()
        if collapsed and not self._collapsed:
            self._expanded_size = (rect.size.width, rect.size.height)
            self._expanded_h = rect.size.height
        self._collapsed = collapsed
        controlled = ["message", "sender", "intent", "confidence", "risk", "actions",
                      "cand_header"]   # "chat" and "status" survive collapsing
        for key in controlled:
            self.rows[key].setHidden_(collapsed)
        for view in self._detail_views:
            view.setHidden_(collapsed)
        if not collapsed and not self._judgment_enabled:
            for view in self._judgment_views:
                view.setHidden_(True)
        for slot in range(styles.MAX_SLOTS):
            self._dds[slot].setHidden_(collapsed)
            self._dd_boxes[slot].setHidden_(collapsed)
            self._group_boxes[slot].setHidden_(collapsed or not self._slot_active(slot))
            for row in range(styles.PER_TONE):
                has = (row < self.candidate_count and
                       self.cand_texts[slot * styles.PER_TONE + row] is not None)
                for c in self._row_controls(slot, row):
                    c.setHidden_(collapsed or not has)
        if not collapsed:
            # Restore the exact user width/height before reflowing. The top edge remains
            # pinned, matching both auto-layout and the collapsed transition.
            target_w, target_h = self._expanded_size
            self.panel.setMinSize_(NSMakeSize(PANEL_MIN_W, PANEL_MIN_H))
            self._layout_resizing = True
            try:
                self.panel.setFrame_display_(
                    NSMakeRect(rect.origin.x,
                               rect.origin.y + rect.size.height - target_h,
                               target_w, target_h), True)
            finally:
                self._layout_resizing = False
            self._relayout()
            self._last_origin = None      # let the next tick re-dock cleanly
            return

        self.panel.setMinSize_(NSMakeSize(PANEL_MIN_W, COLLAPSED_H))
        self._layout_resizing = True
        try:
            self.panel.setFrame_display_(
                NSMakeRect(rect.origin.x, rect.origin.y + rect.size.height - COLLAPSED_H,
                           rect.size.width, COLLAPSED_H), True)
        finally:
            self._layout_resizing = False
        self._last_origin = None      # let the next tick re-dock cleanly

    # --------------------------------------------------------------- loop
    @objc.python_method
    def _refresh_model_status(self):
        """Repaint the status line while a load/progress status is live (this PR).

        Runs before the paused/busy/read gates so progress stays visible while OCR is
        in flight; `_normal_status` decides what the line falls back to.
        """
        status = self.judge.load_status
        if status != self._model_status:
            was_live = self._model_status is not None
            self._model_status = status
            if status:
                self._show(force=True)
            elif was_live and (self._app is None or self._wechat_frontmost is not True
                               or self._read_fail_hidden):
                # The load just finished; applyHidden_ kept the panel up while it ran,
                # so a WeChat that left in the meantime is hidden only now (review #41).
                # The read-failure latch counts too: the panel was kept past the grace
                # period only for the download's sake — and `_app is None` also covers
                # the pre-first-poll None, where foreground was never established.
                if getattr(self, "auto_hide", True) and self.panel.isVisible():
                    self.panel.orderOut_(None)
            self._render("status", *self._normal_status)

    def _set_foreground_state(self, app):
        """Track supported-app focus and keep optional background WeChat reads isolated.

        `app` is an adapter, None (some other app is frontmost — a real leave), or UNKNOWN
        (the query failed — not evidence of anything, so nothing changes).
        """
        if app is UNKNOWN:
            return False

        prev = self._app
        was_frontmost = getattr(self, "_wechat_frontmost", None)
        if app is prev and app is not None and was_frontmost is True:
            return False

        # Only the screenshot-based WeChat adapter can keep reading by window id while
        # another app is frontmost. QQ's AX tree is intentionally foreground-only.
        if (app is None and prev is not None and prev.key == "wechat"
                and getattr(self, "background_capture", False)):
            if was_frontmost is False:
                return False
            self._wechat_frontmost = False
            self._foreground_epoch += 1
            _log("前台切换 · 微信离开前台，继续后台抓取")
            self._push("applyHidden:", "微信不在前台 · 后台抓取中")
            return True

        if app is prev and app is not None and was_frontmost is False:
            self._wechat_frontmost = True
            self._next_read_ts = 0
            _log("前台切换 · 微信回到前台，展示后台分析结果")
            self._push("applyForegroundShown:", None)
            return True

        self._wechat_frontmost = app is not None
        self._foreground_epoch += 1
        self._reply_epoch += 1
        self._reply_key = None
        self.last_seen = None
        self.analyzed_text = None
        self._prejudge_req = self._prejudge_result = None
        self._pregen_req = self._pregen_result = None
        self._gen_epoch += 1
        self._fingerprint = None
        self._last_full = None
        self._win_wid = None
        self._input_target = None
        self._input_window = None
        self._input_next = 0
        self._stable_n = 0
        self._burst_left = BURST_READS
        self._last_skip_reason = None
        self._read_fail_since = None
        self._read_fail_hidden = False
        self._empty_frame_since = None
        self._app = app

        if app is None:
            _log("前台切换 · 聊天应用离开前台，隐藏面板并清空旧结果")
            self._push("applyForegroundHidden:", "微信 / QQ 不在前台")
        else:
            self._next_read_ts = 0
            if prev is not None:
                # 适配器→适配器（微信→QQ 等）与离开一样是一次硬边界：旧会话与旧候选
                # 必须下屏，否则点「填入」会把给前一个 App 写的回复填进当前 App。
                _log(f"前台切换 · 切换到{app.display_name}，清空面板并强制重新读屏")
                self._push("applyForegroundHidden:", f"已切换到{app.display_name}")
            else:
                _log(f"前台切换 · {app.display_name}回到前台，强制重新读屏")
        return True

    @objc.python_method
    def _capture_allowed(self):
        frontmost = getattr(self, "_wechat_frontmost", None)
        return (self._app is not None
                and (frontmost is True
                     or (frontmost is False
                         and self._app.key == "wechat"
                         and getattr(self, "background_capture", False))))

    def tick_(self, timer):
        # Progress/status refresh first: a live download must stay visible even while
        # WeChat is gone or the read loop is gated (applyHidden_ keeps the panel up).
        if getattr(self, "_calibrating", False):
            return
        self._refresh_model_status()
        # Check activation before pause/busy/read-cadence gates.  The timer keeps
        # firing while OCR is in flight, so a quick WeChat -> Chrome -> WeChat
        # round trip still advances _foreground_epoch and retires that capture.
        app = frontmost_app()
        if app is UNKNOWN:
            return
        self._set_foreground_state(app)
        if not self._capture_allowed():
            return
        app = self._app
        # Follow the window from cheap metadata every tick, not from read results (#93):
        # in manual-calibration mode one read is a full multi-second OCR, and positioning
        # used to wait out two whole read cycles (the two-tick debounce) after a drag.
        # Metadata-only enumeration, ~1-5 ms. Screen-capture apps only — QQ's panel
        # follows its AX read path. The read path's applyPosition stays as a backstop;
        # when both agree the dead-band absorbs the duplicate.
        if not self._paused and getattr(app, "needs_screen_capture", False):
            win = find_wechat_window(previous_wid=getattr(self, "_win_wid", None))
            if win is not None:
                self._position_near({"wid": win.wid, "x": win.x, "y": win.y,
                                     "w": win.w, "h": win.h})
        if self._paused or self._busy or time.time() < self._next_read_ts:
            return  # paused, a previous read is still running, or not due yet
        self._busy = True
        threading.Thread(target=self._work, daemon=True).start()

    @objc.python_method
    def _work(self):
        try:
            self._work_inner()
        finally:
            self._busy = False

    @objc.python_method
    def _work_inner(self):
        self.reload_conversations()
        # The panel is a global floating window. Showing it over Chrome while continuing
        # to reuse the last WeChat frame makes stale text look like browser OCR. Treat app
        # activation as a hard display/capture boundary before even checking permissions:
        # a missing screen grant must not keep an error panel floating over other apps.
        app = frontmost_app()
        if app is UNKNOWN:
            # A transient NSWorkspace failure is not proof that the user left the chat app.
            # Freeze both reads and UI updates for one short tick without cancelling a
            # valid in-flight reply or manufacturing a leave/return transition.
            self._next_read_ts = time.time() + FAST_TICK
            return
        self._set_foreground_state(app)
        if not self._capture_allowed():
            self._next_read_ts = time.time() + FAST_TICK
            return
        app = self._app
        if app.needs_screen_capture:
            if not screen_capture_ok():
                if not self._asked_permission:
                    self._asked_permission = True
                    request_screen_capture()      # opens the system prompt
                self._push("applyError:", "需要屏幕录制权限 · 系统设置 › 隐私与安全性")
                self._next_read_ts = time.time() + SLOW_TICK
                return
        elif not fill.has_accessibility():
            # QQ reads the accessibility tree: without the grant there is nothing to read.
            if not self._asked_accessibility:
                self._asked_accessibility = True
                fill.request_accessibility()
            self._push("applyError:", "需要辅助功能权限 · 系统设置 › 隐私与安全性")
            self._next_read_ts = time.time() + SLOW_TICK
            return
        if getattr(self, "_calibrating", False):
            return
        if (app.needs_screen_capture
                and getattr(self, "_calibration_required", False)
                and self._calibration is None):
            # 校准只约束微信 OCR 路径；QQ 读无障碍树，无消息区可校准。
            self._push("applyWaiting:", "请点击右上角校准图标，确认消息区和输入区。")
            self._next_read_ts = time.time() + SLOW_TICK
            return
        capture_foreground_epoch = self._foreground_epoch
        capture_context_version = self._context_version
        try:
            # calibration 只被微信 OCR 路径接受；QQ 适配器签名里没有它，不传。
            extra = {}
            if app.needs_screen_capture and getattr(self, "_calibration", None):
                extra = {"calibration": self._calibration}
            res = app.read_conversation(previous_wid=self._win_wid,
                                        prev_fingerprint=self._fingerprint,
                                        prev_layout=getattr(self, "_layout_key", None), **extra)
        except Exception as e:
            self._push("applyError:", f"读取失败: {type(e).__name__}")
            self._next_read_ts = time.time() + SLOW_TICK
            return
        if capture_foreground_epoch != self._foreground_epoch:
            return
        if getattr(self, "_calibration", None) and (res.get("calibration_error")
                or (res.get("window") and res["window"]["wid"] != self._calibration_wid)):
            self._calibration = None
            self._input_calibration = None
            self._input_calibration_wid = None
            self._retire_calibration_results()
            self._push("applyWaiting:", "微信窗口已改变，请重新校准消息区域。")
            return
        # Re-check after the blocking capture/OCR.  tick_ may have observed a
        # complete leave+return while this worker was busy; in that case even a
        # currently-frontmost chat app does not make this old snapshot current.
        after = frontmost_app()
        if after is UNKNOWN:
            self._next_read_ts = time.time() + FAST_TICK
            return
        self._set_foreground_state(after)
        if (self._app is not app or not self._capture_allowed()
                or capture_foreground_epoch != self._foreground_epoch):
            self._next_read_ts = time.time() + FAST_TICK
            return
        if not res["ok"]:
            # Window enumeration/capture can miss one frame while the chat app redraws.
            # Keep the already-current HUD stable for a short grace period, then
            # hide and force rediscovery if the failure really persists.
            now_mono = time.monotonic()
            if self._read_fail_since is None:
                self._read_fail_since = now_mono
            if (not self._read_fail_hidden
                    and now_mono - self._read_fail_since >= READ_FAILURE_HIDE_S):
                self._read_fail_hidden = True
                # A persistent miss is no longer a harmless one-frame redraw.
                # Retire every reply callback before hiding; otherwise an in-flight
                # generation from the last visible frame could call _show() again.
                self._reply_epoch += 1
                self._reply_key = None
                self.last_seen = None
                self.analyzed_text = None
                self._prejudge_req = self._prejudge_result = None
                self._pregen_req = self._pregen_result = None
                self._gen_epoch += 1
                self._fingerprint = None
                self._last_full = None
                self._win_wid = None
                self._push("applyForegroundHidden:", res["error"])
            self._next_read_ts = time.time() + FAST_TICK
            return

        self._read_fail_since = None
        self._read_fail_hidden = False

        # Same fingerprint ⇒ same pixels ⇒ the messages are exactly what we last read.
        # Cadence follows the screen: quiet checks back in FAST_TICK (capture+hash only,
        # ~30 ms); a change first keeps BURST_TICK for a few reads so the burst's NEXT
        # message is noticed quickly (this also feeds _stable_n, the early-settle signal),
        # and only a pane that keeps moving settles back to SLOW_TICK like the old poll.
        self._fingerprint = res.get("fingerprint")
        self._layout_key = res.get("layout")
        if res["unchanged"]:
            self._stable_n += 1
            self._burst_left = BURST_READS
            self._next_read_ts = time.time() + FAST_TICK
        elif self._burst_left > 0:
            self._burst_left -= 1
            self._stable_n = 0
            self._next_read_ts = time.time() + BURST_TICK
        else:
            self._stable_n = 0
            self._next_read_ts = time.time() + SLOW_TICK

        # Position from the read result as a backstop only — tick_ now drives
        # positioning from cheap metadata every tick (#93). This keeps the panel
        # correct when the window moved mid-read; a same-target push is absorbed
        # by the dead-band.
        live_window = res["window"]
        live_input_rect = res.get("input_rect")
        self._win_wid = res["window"]["wid"]
        self._push("applyPosition:", res["window"])
        fresh_frame = not res["unchanged"]
        if res["unchanged"] and self._last_full is not None:
            # the settle/analyze gate below still runs every read; an unchanged frame
            # just skips re-deriving the messages it would act on
            res = self._last_full
        elif (not res.get("manual_calibration") and not res["messages"] and self._last_full is not None
              and self._last_full.get("messages")):
            # Transient empty frame (#58): the window is still enumerated and the capture
            # succeeded, but OCR returned 0 blocks (WeChat 4.x redraw glitch). Reuse the
            # last good read so the settle gate keeps its target and timer. Entering the
            # streak retires in-flight workers once (a candidate computed for a vanished
            # message must never surface) but keeps the reply target and re-arms both
            # prework halves at the new epoch, so the settle path stays the fast one.
            now_mono = time.monotonic()
            if (self._empty_frame_since is not None
                    and now_mono - self._empty_frame_since > EMPTY_FRAME_REUSE_S):
                # A persistently empty read is no longer a one-frame glitch: give up on
                # reuse and fall through as a real empty read (same grace as a capture
                # miss); the key change below then clears the stale target.
                _log(f"读屏为空已持续 {now_mono - self._empty_frame_since:.1f}s"
                     f" · 放弃沿用上一帧")
                self._empty_frame_since = None
                self._last_full = None
            else:
                if self._empty_frame_since is None:
                    self._empty_frame_since = now_mono
                    _log("读屏为空 · 沿用上一帧继续分析")
                    last_msgs = self._last_full.get("messages") or []
                    last_thems = [m for m in last_msgs if m.side == "them"]
                    if last_thems:
                        self._reply_epoch += 1
                        self.analyzed_text = None   # let the settle gate re-open
                        self._enqueue_prework(
                            last_thems[-1], self._active_context,
                            last_thems[-2].text if len(last_thems) > 1 else "",
                            self._context_text(last_msgs, last_thems[-1],
                                               self.judge_context_turns))
                res = self._last_full
                fresh_frame = False
        else:
            self._empty_frame_since = None
            self._last_full = res
            # a picked-over window list was invisible in the logs and cost a whole
            # misdiagnosis (#91): say which window the reads moved to, geometry only
            if res["window"].get("wid") != getattr(self, "_read_wid", None):
                self._read_wid = res["window"].get("wid")
                _log(f"读屏窗口切换 wid={res['window'].get('wid')} "
                     f"{res['window'].get('w', 0):.0f}x{res['window'].get('h', 0):.0f}")
            self._push("applyChat:", res.get("chat_title") or "")

        res = dict(res, window=live_window, input_rect=live_input_rect)
        if res.get("manual_calibration"):
            self._input_target = None
            editor = getattr(self, "_input_calibration", None)
            if editor is not None and self._input_calibration_wid == res['window']['wid']:
                from visual_fill import chat_signature
                rect = editor.screen_rect(res['window'])
                signature_rect = self._calibration.screen_rect(res['window'])
                self._input_target = dict(box=None,rect=None,window=dict(res['window']),
                    visual_rect=rect,manual_region=editor,signature_rect=signature_rect,
                    chat_signature=chat_signature(res['window'],signature_rect),
                    app=app.key,   # 填入前复核：手动校准目标同样必须带归属标记（#105）
                    reason="手动校准输入区")
            self._input_window = dict(res["window"])
            self._input_next = float("inf")
            if not res.get("chat_title"):
                res = dict(res, messages=[])
        # AX traversal stays on the read worker, never the Cocoa drawing thread.
        now_input = time.monotonic()
        if (res["window"] != getattr(self, "_input_window", None)
                or now_input >= getattr(self, "_input_next", 0)):
            target = app.locate_input(res["window"])
            # AX can transiently return None while the chat app rebuilds its tree during a
            # foreground/window transition. Keep this frame readable and let the next
            # scheduled read retry; never let a missing target abort the read worker.
            target = dict(target) if isinstance(target, dict) else {
                "box": None, "rect": None, "window": dict(res["window"]),
                "reason": "输入框暂时不可用",
            }
            target["app"] = app.key   # 填入前复核：目标必须属于当前 App
            if target["box"] is None and app.needs_screen_capture:
                # 视觉后备要截图/OCR，只有走屏幕采集的 App（微信）才允许进入；
                # QQ 的 AX 路径绝不截图——box 为 None 就让它保持 None（填入按钮报原因）。
                from input_region import locate_visual_input
                target["visual_rect"] = (res.get("input_rect")
                                         or locate_visual_input(res["window"]))
                if target["visual_rect"]:
                    from visual_fill import chat_signature
                    target["chat_signature"] = chat_signature(res["window"], target["visual_rect"])
            if capture_foreground_epoch != self._foreground_epoch:
                self._next_read_ts = time.time() + FAST_TICK
                return
            self._input_target = target
            self._input_window = dict(res["window"])
            self._input_next = now_input + 1.0
        with self._context_lock:
            self.reload_conversations()
            if capture_context_version != self._context_version:
                self._fingerprint = None
                self._last_full = None
                return
            msgs = res["messages"]
            # 手动校准模式下，无法确认归属的文字不进入会话历史与模型上下文。
            # 必须在 visible/observe 之前过滤——事后过滤会让 _observed_offset 的
            # 索引错位；检测框仍画原始列表（含「未确认」框），见下方 applyBoxes。
            overlay_msgs = None
            if res.get("manual_calibration"):
                overlay_msgs = msgs
                msgs = [m for m in msgs if m.side != "unknown"]
            visible = [(m.text, m.side, m.sender or "") for m in msgs]
            self._observed_messages, self._observed_offset = visible, 0
            if self.history_enabled and self.conversations and not self.conversations.error:
                try:
                    self._observed_messages, self._observed_offset = self.conversations.observe(
                        res.get("chat_title"), visible, record=fresh_frame)
                except (OSError, ValueError):
                    _log("保存会话历史失败，使用当前画面继续分析")
                    self._push("applyError:", "会话历史保存失败，请检查磁盘空间及权限")
            read_text = self._format_read_result(msgs)
            read_count = len(msgs)
            self._read_result_text = read_text
            self._read_result_count = read_count
            thems = [m for m in msgs if m.side == "them"]
            newest = thems[-1] if thems else None
            span = reply_span(msgs, newest) if newest else []
            if newest:
                newest.analysis_text = reply_text(span)
                newest.analysis_body = "\n".join(m.text for m in span)
                newest.analysis_parts = tuple(id(m) for m in span)
            target_text = self._target_text(newest) if newest else None
            prior_thems = ([m for m in thems if id(m) not in newest.analysis_parts]
                           if newest else [])
            prev_text = prior_thems[-1].text if prior_thems else ""

            context = (self._context_text(msgs, newest, self.generation_context_turns)
                       if newest else None)
            judge_context = (self._context_text(msgs, newest, self.judge_context_turns)
                             if newest else None)
            newest_identity = (f"{target_text}@{newest.y:.3f}"
                               if newest and getattr(newest, "visual_only", False)
                               else target_text)
            # key 带 app.key：不同 App 的同名会话 / 同文消息不得共用一条回复纪元
            key = (app.key, res.get("chat_title") or "", newest_identity, context,
                   tuple(visible)) if newest else None
            self._active_context = context
            if key != self._reply_key:
                self._reply_epoch += 1
                self._reply_key = key
                self.last_seen = None
                self.analyzed_text = None
                self._analyzed_body = None
                self._prejudge_req = self._prejudge_result = None
                self._pregen_req = self._pregen_result = None
                self._gen_epoch += 1

            # YOLO overlay: repaint whenever a read produced geometry — unchanged reads reuse
            # the cached messages, so the boxes stay up even while the pane is quiet
            if self._show_boxes:
                self._push("applyBoxes:", (res["window"], overlay_msgs or msgs,
                                           target_text, newest))
            if newest is None:
                self._push("applyWaiting:", {
                    "reason": ("暂未确认输入区边界，暂停分析"
                               if res.get("input_unresolved") else None),
                    "read_text": read_text, "count": read_count})
                return
            if getattr(newest, "visual_only", False):
                if newest_identity != self.last_seen:
                    self.last_seen = newest_identity
                    self.analyzed_text = newest_identity
                    self._prejudge_req = self._prejudge_result = None
                    self._pregen_req = self._pregen_result = None
                    self._push("applyUnreadable:", (read_text, read_count))
                return
            now = time.time()

            # --- anti-flood: track arrivals, never analyze mid-burst
            if target_text != self.last_seen:
                self.last_seen = target_text
                self.last_change_ts = now
                # only on arrival: this function runs every second, and a per-tick line would
                # bury the timing that matters
                t = res.get("timing_ms") or {}
                first_read = not self._read_once
                self._read_once = True
                # Vision loads on the first call and costs ~2x steady state; saying so keeps a
                # one-off from being read as a regression (same reason the judge line does it)
                note = "（首次，含 Vision 加载）" if first_read and t.get("ocr", 0) > 400 else ""
                _log(f"读屏 抓取 {t.get('capture', 0):.0f}ms + OCR {t.get('ocr', 0):.0f}ms"
                     f" = {t.get('total', 0):.0f}ms · 读到 {len(msgs)} 条（对方 {len(thems)} 条）"
                     f"{note}")
                lead = "预判+生成" if self._judgment_enabled else "生成"
                _log(f"新消息 · {lead}先跑，停稳 {SETTLE_S}s（连续 {STABLE_READS} 跳不变最早 "
                     f"{EARLY_SETTLE_S}s）后上屏（两次完整分析最小间隔 {MIN_GAP_S}s）")
                # latest-wins: overwrite the slot, retire the old verdict — only the newest
                # text's judgment can ever be consumed, and only by the settle gate below
                self._prejudge_result = None
                if self._judgment_enabled:
                    self._prejudge_req = (
                        target_text, judge_context, newest.sender, prev_text,
                        self._reply_epoch, self._judge_text(newest))
                    self._prejudge_event.set()
                else:
                    self._prejudge_req = None
                self._pregen_req = (
                    target_text, context, tuple(self.slot_tones),
                    self.candidate_count, self._reply_epoch)
                self._pregen_result = None
                self._pregen_event.set()
                # keep the previous verdict readable; just badge that something new landed
                self._push("applyIncoming:", (target_text, newest.sender, prev_text,
                                               read_text, read_count))

            # Anti-flood, two signals: the blind wait (SETTLE_S, unchanged upper bound) or a
            # content-stability early open — the pane went quiet for STABLE_READS consecutive
            # reads spanning at least EARLY_SETTLE_S, which is itself evidence the burst is
            # over. A burst keeps resetting _stable_n, so mid-burst opens cannot happen.
            elapsed = now - self.last_change_ts
            settled = elapsed >= SETTLE_S or (elapsed >= EARLY_SETTLE_S
                                              and self._stable_n >= STABLE_READS)
            cooled = (now - self.last_analyze_ts) >= MIN_GAP_S
            pr = self._prejudge_result
            pre_hit = pr is not None and pr[0] == target_text and pr[4] == self._reply_epoch
            # A pre-judged verdict needs no cooling: its cost was already paid per arrival.
            # Only the full path (no usable pre-judgment) still waits MIN_GAP_S out.
            if (target_text != self.analyzed_text and settled and not self._analyzing
                    and not self._prejudging and (pre_hit or cooled)):
                self.last_analyze_ts = now
                self.analyzed_text = target_text
                self._analyzed_body = self._judge_text(newest)
                self._prejudge_result = None      # spent: a verdict is shown exactly once
                self._analyzing = True
                if pre_hit:
                    # Judgment already ran inside the settle window; go straight to the
                    # verdict on screen and start only the generation half.
                    _log(f"停稳 · 用预判结论上屏 · 这条消息出现到现在 {now - self.last_change_ts:.1f}s")
                    self._push("applyJudgment:", (pr[1], pr[2], pr[3],
                                                   read_text, read_count))
                    threading.Thread(target=self._reply_task,
                                     args=(self._reply_epoch, self._run_generation,
                                           newest, msgs, pr[1]), daemon=True).start()
                else:
                    _log(f"开始分析 · 这条消息出现到现在 {now - self.last_change_ts:.1f}s")
                    self._push("applyPending:", (target_text, newest.sender, prev_text,
                                                  read_text, read_count))
                    # off the tick path on purpose: judge+generate+rank takes over a second, and
                    # while it runs the loop must keep reading — a message landing mid-analysis
                    # used to wait the whole analysis out before anyone even saw it
                    threading.Thread(target=self._reply_task,
                                     args=(self._reply_epoch, self._run_analysis,
                                           newest, msgs, prev_text, read_text, read_count),
                                     daemon=True).start()
            elif target_text != self.analyzed_text:
                # the wait is deliberate; say so once per arrival change so "it feels slow" can
                # be told apart from "it is still waiting out the burst window"
                why = ("消息还在变" if not settled else
                       "上一条还在分析" if self._analyzing else
                       "预判还在跑" if self._prejudging else
                       f"距上次分析不足 {MIN_GAP_S}s")
                if self._last_skip_reason != why:
                    self._last_skip_reason = why
                    _log(f"暂不分析（{why}）")
            else:
                self._last_skip_reason = None

    @objc.python_method
    def _enqueue_prework(self, newest, context, prev_text, judge_context=None):
        """Restart both speculative model passes after a transient capture gap."""
        text = self._target_text(newest)
        self._prejudge_result = None
        if self._judgment_enabled:
            self._prejudge_req = (text, judge_context, newest.sender, prev_text,
                                  self._reply_epoch, self._judge_text(newest))
            self._prejudge_event.set()
        else:
            self._prejudge_req = None
        self._pregen_req = (text, context, tuple(self.slot_tones),
                            self.candidate_count, self._reply_epoch)
        self._pregen_result = None
        self._pregen_event.set()


    @objc.python_method
    def _run_analysis(self, newest, msgs, prev_text: str,
                      read_text: str = "", read_count: int = 0):
        try:
            if not self._reply_current():
                return
            self._analyze(newest, msgs, prev_text, read_text, read_count)
        except Exception as e:
            _log(f"分析失败 {type(e).__name__}")
            self._push("applyError:", f"分析失败: {type(e).__name__}")
        finally:
            self._analyzing = False

    @objc.python_method
    def _prejudge_loop(self):
        """Judge a message the moment it is seen, so the settle gate can skip the wait.

        One resident worker serializes the passes (a forward takes ~1 s). The request slot
        holds only the newest text, so a burst queues one judgment, not one per tick, and a
        verdict survives only if its text is still the newest when the pass ends
        (latest-wins — checked before and after). Nothing is drawn here: the settle gate in
        _work_inner is the only place a verdict reaches the panel, so a stale conclusion
        cannot be shown no matter how the timing lands.
        """
        while True:
            try:
                self._prejudge_event.wait()
                self._prejudge_event.clear()
                req = self._prejudge_req
                self._prejudge_req = None
                if req is None:
                    continue
                text, context, sender, prev, epoch, *judge_body = req
                self.reload_conversations()
                if self._paused or text != self.last_seen or epoch != self._reply_epoch:
                    continue          # superseded while queued: only the newest text counts
                self._prejudging = True
                try:
                    t0 = time.perf_counter()
                    with self._model_lock:
                        self.reload_conversations()
                        if epoch != self._reply_epoch or self._paused:
                            continue
                        body = judge_body[0] if judge_body else text
                        verdict = self.judge.judge(chat_context.model_message(body, context),
                                                   context=context)
                    ms = (time.perf_counter() - t0) * 1000
                    first = not self._judged_once
                    self._judged_once = True
                    note = "（首次，含本地模型加载）" if first else ""
                    _log(f"预判 {ms:.0f}ms → {verdict.get('intent', '?')}"
                         f" 把握 {verdict.get('confidence', 0):.0%}"
                         f" 风险 {verdict.get('risk', '?')}{note}（待停稳上屏）")
                except Exception as e:
                    _log(f"预判失败 {type(e).__name__}")
                    verdict = None
                finally:
                    self._prejudging = False
                self.reload_conversations()
                if (verdict is not None and not self._paused and text == self.last_seen
                        and epoch == self._reply_epoch):
                    self._prejudge_result = (text, verdict, sender, prev, epoch)
            except Exception:
                pass                  # a resident worker must not die on one bad request

    @objc.python_method
    def _pregen_loop(self):
        """Generate the moment a message is seen — the paid half of the pre-judge trick.

        Same resident-worker, latest-wins shape as _prejudge_loop. Generation is a network
        call, so it holds no _model_lock and truly overlaps the local judge. A burst each
        time overwrites the slot, so one generation per arrival, not one per tick, and a
        result survives only if its text is still the newest when the call returns.
        """
        while True:
            try:
                self._pregen_event.wait()
                self._pregen_event.clear()
                req = self._pregen_req
                self._pregen_req = None
                if req is None:
                    continue
                if len(req) == 4:  # compatibility with queued work from an older session
                    text, context, tones, epoch = req
                    candidate_count = self.candidate_count
                else:
                    text, context, tones, candidate_count, epoch = req
                self.reload_conversations()
                if self._paused or text != self.last_seen or epoch != self._reply_epoch:
                    continue          # superseded while queued: only the newest text counts
                self._pregen_running = True
                gen = None
                try:
                    gen = self.generator.generate(
                        chat_context.model_message(text, context), "", list(tones), context,
                        candidate_count=candidate_count)
                except Exception:
                    gen = None        # a failed early run just means the settle path regenerates
                # store BEFORE clearing _pregen_running, so _take_pregen never observes
                # "not running" without the result already visible
                self.reload_conversations()
                if (gen is not None and not self._paused and text == self.last_seen
                        and epoch == self._reply_epoch):
                    self._pregen_result = (text, tones, candidate_count, gen, epoch)
                self._pregen_running = False
            except Exception:
                self._pregen_running = False

    @objc.python_method
    def _take_pregen(self, text: str, tones: tuple) -> tuple[dict | None, float]:
        """Collect the early generation: (gen, waited_ms). gen=None ⇒ caller generates.

        A stored result counts only when BOTH the text and the tone selection match — the
        text because a newer message retired it, the tones because a dropdown click during
        the window changed what should be generated. While a matching request is in flight
        we wait for it (it started ~1 s ago at detection, so what is left is usually a few
        hundred ms — still cheaper than a fresh call, and free of a second TLS handshake).
        """
        t0 = time.perf_counter()
        deadline = time.time() + 30   # generation's own timeout; never wait longer
        while time.time() < deadline:
            if not self._reply_current():
                return None, (time.perf_counter() - t0) * 1000
            r = self._pregen_result
            if (r is not None and r[0] == text and r[1] == tones
                    and r[2] == self.candidate_count and r[4] == self._reply_epoch):
                self._pregen_result = None      # spent: each result is consumed exactly once
                return r[3], (time.perf_counter() - t0) * 1000
            if (not self._pregen_running
                    and (self._pregen_req is None or self._pregen_req[0] != text)):
                return None, (time.perf_counter() - t0) * 1000
            time.sleep(0.03)
        return None, (time.perf_counter() - t0) * 1000

    @objc.python_method
    def _gen_with_pregen(self, text: str, context: str | None,
                         on_candidate=None) -> dict:
        """Generate, preferring an early run already in flight or finished (full path).

        The hook only reaches the fresh call: an early-run hit already has all its lines,
        and _finish_generate paints them the moment the verdict lands.
        """
        tones = tuple(self.slot_tones)
        gen, _waited = self._take_pregen(text, tones)
        if not self._reply_current():
            return {"groups": []}
        if gen is None:
            gen = self.generator.generate(
                chat_context.model_message(text, context), "", list(tones), context,
                on_candidate,
                candidate_count=self.candidate_count)
        return gen

    @objc.python_method
    def _run_generation(self, newest, msgs, verdict: dict):
        """The pre-judged path's second half: collect generation + rank, judgment shown.

        The early run usually finished inside the settle window, so what is left here is
        the wait-remainder plus ranking. Only a miss (superseded mid-burst, tone changed
        during the window, network failure) starts a fresh call — and that one streams,
        so it gets the hook. A hit never creates a hook, so no epoch is bumped and any
        in-flight 换话术 stream keeps its slot on screen.
        """
        t0 = time.perf_counter()
        try:
            if not self._reply_current():
                return
            context = self._context_text(msgs, newest)
            gen, wait_ms = self._take_pregen(self._target_text(newest), tuple(self.slot_tones))
            if not self._reply_current():
                return
            note = f"（早跑命中，停稳后仅等 {wait_ms:.0f}ms）" if gen is not None else ""
            if gen is None:
                gen = self.generator.generate(
                    chat_context.model_message(self._target_text(newest), context), "",
                    list(self.slot_tones), context,
                    self._stream_hook(t0), candidate_count=self.candidate_count)
            self._finish_generate(gen, newest, t0, verdict, note)
        except Exception as e:
            _log(f"生成失败 {type(e).__name__}")
            self._push("applyError:", f"候选生成失败: {type(e).__name__}")
        finally:
            self._analyzing = False

    @objc.python_method
    def reload_conversations(self):
        with self._context_lock:
            store = self.conversations
            if store is not None:
                store.reload()
                if store.revision != getattr(self, '_conversation_revision', 0):
                    self._conversation_revision = store.revision
                    self._context_changed()
                    if store.error:
                        _log(store.error)
                        self._push_reply("applyError:", store.error, self._reply_epoch)
            return store

    @objc.python_method
    def _context_changed(self):
        self._context_version += 1
        self._reply_epoch += 1
        self._gen_epoch += 1
        self._reply_key = None
        self.last_seen = self.analyzed_text = None
        self._prejudge_req = self._prejudge_result = None
        self._pregen_req = self._pregen_result = None
        self._active_context = None
        self._observed_messages = None
        self._observed_offset = 0
        self._fingerprint = None
        self._next_read_ts = 0
        self._push_reply("applyWaiting:", None, self._reply_epoch)

    @objc.python_method
    def save_background(self, title, text):
        with self._context_lock:
            self.reload_conversations()
            if self.conversations is None:
                raise OSError("会话存储不可用")
            self.conversations.save_background(title, text)
            if title == (self._last_full or {}).get("chat_title"):
                self._context_changed()

    @objc.python_method
    def configure_context(self, enabled, limit):
        count = chat_context.message_limit(limit)
        with self._context_lock:
            if (self.history_enabled, self.context_limit) != (enabled, count):
                self.history_enabled, self.context_limit = enabled, count
                self._context_changed()

    @objc.python_method
    def clear_history(self, title=None):
        with self._context_lock:
            self.reload_conversations()
            if self.conversations is None:
                raise OSError("会话存储不可用")
            self.conversations.clear_history(title)
            self._context_changed()

    @objc.python_method
    def _context_text(self, msgs, newest, turns: int | None = None) -> str | None:
        """Return bounded prior turns, excluding every bubble in the reply target."""
        if turns is None and hasattr(self._reply_worker, "context"):
            return self._reply_worker.context
        if turns is None:
            turns = self.generation_context_turns
        if turns <= 0 or newest is None:
            return None
        with self._context_lock:
            store = self.reload_conversations()
            title = (self._last_full or {}).get("chat_title")
            background = store.data.get(title, {}).get('background', '') if store else ""
            excluded = set(getattr(newest, "analysis_parts", ())) or {id(newest)}
            target_index = next((i for i, message in enumerate(msgs)
                                 if id(message) in excluded),
                                next(i for i, message in enumerate(msgs)
                                     if message is newest))
            if self.history_enabled and self._observed_messages is not None:
                observed = list(self._observed_messages)
                observed_indices = {self._observed_offset + i for i, message in enumerate(msgs)
                                    if id(message) in excluded}
                current = (self._target_text(newest), newest.side, newest.sender or "")
                model_messages = [message for i, message in enumerate(observed)
                                  if i not in observed_indices]
                model_messages.append(current)
                observed_target = len(model_messages) - 1
                return chat_context.context_text(
                    model_messages, observed_target,
                    limit=min(self.context_limit, turns + 1), background=background)
        prior = [m for m in msgs if id(m) not in excluded][-turns:]
        if not prior:
            return f"会话背景：\n{background}" if background else None
        context = "\n".join(
            f"{m.sender or {'me': '我', 'them': '对方', 'public': '公共信息'}.get(m.side, '方向未确认')}: {reply_text([m])}"
            for m in prior)
        return (f"会话背景：\n{background}\n\n{context}" if background else context)

    @objc.python_method
    def _analyze(self, newest, msgs, prev_text: str = "",
                 read_text: str = "", read_count: int = 0):
        """Judge and generate in parallel, then rank. Judgment lands on screen first.

        Runs on its own thread (started by _work_inner): it takes over a second and must
        not hold the read loop hostage.
        """
        import concurrent.futures as cf

        t0 = time.perf_counter()
        context = self._context_text(msgs, newest)
        if not self._judgment_enabled:
            try:
                gen = self._gen_with_pregen(self._target_text(newest), context,
                                            self._stream_hook(t0))
            except Exception as e:
                _log(f"生成失败 {type(e).__name__}: {str(e)[:60]}")
                self._push("applyError:", f"候选生成失败: {type(e).__name__}: {str(e)[:40]}")
                return
            self._finish_generate(gen, newest, t0, None)
            return
        with cf.ThreadPoolExecutor(max_workers=2) as ex:
            # generation does not need the intent, so it runs while judging; it prefers an
            # early run that started at detection time (_gen_with_pregen) — only a miss
            # streams, and only that fresh call takes the hook
            gen_future = ex.submit(self._reply_task, self._reply_worker.epoch,
                                   self._gen_with_pregen, self._target_text(newest), context,
                                   self._stream_hook(t0))
            verdict = None
            t_judge = time.perf_counter()
            try:
                with self._model_lock:   # never two local forwards at once
                    if not self._reply_current():
                        return
                    judge_context = self._context_text(
                        msgs, newest, self.judge_context_turns)
                    verdict = self.judge.judge(
                        chat_context.model_message(self._judge_text(newest), judge_context),
                        context=judge_context)
                ms = (time.perf_counter() - t_judge) * 1000
                first = not self._judged_once
                self._judged_once = True
                # the model load happens on the first call and is seconds, not milliseconds —
                # without saying so the first verdict looks like a performance regression
                note = "（首次，含本地模型加载）" if first else ""
                _log(f"判断 {ms:.0f}ms → {verdict.get('intent', '?')}"
                     f" 把握 {verdict.get('confidence', 0):.0%}"
                     f" 风险 {verdict.get('risk', '?')}{note}")
                self._push("applyJudgment:", (verdict, newest.sender, prev_text,
                                               read_text, read_count))
            except (LowMemoryError, ModelNotDownloadedError) as e:
                # Both guards refuse with text written for the user (actual GB / the two
                # ways out, see judge.low_memory_reason and judge.download_block_reason);
                # the generic formatting below truncates at 40 chars and would cut the
                # "TYPESAFE_API_KEY" line in half — README promises the hint.
                _log(f"判断被拒 {type(e).__name__}")
                self._push("applyError:", str(e))
            except Exception as e:
                _log(f"判断失败 {type(e).__name__}")
                self._push("applyError:", f"判断失败: {type(e).__name__}")

            try:
                gen = gen_future.result()
            except Exception as e:
                _log(f"生成失败 {type(e).__name__}")
                self._push("applyError:", f"候选生成失败: {type(e).__name__}")
                return
            self._finish_generate(gen, newest, t0, verdict)

    @objc.python_method
    def _finish_generate(self, gen: dict, newest, t0: float, verdict: dict | None,
                         note: str = ""):
        """Log the generation, push candidates unranked, rank, push the ordered version.

        Shared by both analysis paths — the full one (judge ran here) and the pre-judged
        one (the verdict was computed during the settle window) — so the second half of
        the pipeline has exactly one implementation. Candidates reach the screen BEFORE
        ranking (prob reads 排序中) and are re-ordered in place when the local forward
        lands, so the ~0.5 s rank never delays first paint. On streamed endpoints the
        lines are usually already up (applyStreamLine_) and this first push just re-renders
        them; on non-streaming ones (anthropic shape) it IS the first paint. Without an
        intent (judge failed) ranking is a no-op and the early push is skipped.
        """
        if not self._reply_current():
            return
        groups = gen.get("groups") or []
        failed = [str(g["slot"]) for g in groups if g.get("error")]
        _log(f"生成 {gen.get('elapsed_s', 0) * 1000:.0f}ms{note} · {len(groups)} 个话术并发"
             f" → {sum(len(g['texts']) for g in groups)} 条候选"
             + (f" · 失败: {'; '.join(failed)}" if failed else ""))
        intent = verdict["intent"] if verdict else ""
        payload = self._payload_from_gen(gen)
        if payload is None:
            err = "服务未返回可用候选，请检查模型设置"
            _log(f"生成无可用候选: {err}")
            self._push("applyError:", f"候选生成失败: {err}")
            return
        if intent:
            self._push("applyCandidates:", payload)
        t_rank = time.perf_counter()
        ranked = self._rank_payload(payload, self._judge_text(newest), intent) if intent else payload
        rank_ms = (time.perf_counter() - t_rank) * 1000
        if intent:
            backend = getattr(self.judge, "backend_label", "本地 decider-2b，一次前向")
            _log(f"排序 {rank_ms:.0f}ms（{backend}）")
        _log(f"端到端 {(time.perf_counter() - t0) * 1000:.0f}ms"
             f" · 从分析开始到候选上屏")
        self._push("applyCandidates:", ranked)

    @objc.python_method
    def _push(self, selector: str, payload=None):
        if selector in {"applyIncoming:", "applyPending:", "applyJudgment:",
                        "applyCandidates:", "applyRegenerated:", "applyTones:",
                        "applyUnreadable:",
                        "applyAdjusted:",
                        "applyStreamLine:", "applyWaiting:", "applyError:"}:
            epoch = getattr(self._reply_worker, "epoch", self._reply_epoch)
            self._push_reply(selector, payload, epoch)
            return
        self.performSelectorOnMainThread_withObject_waitUntilDone_(selector, payload, False)

    @objc.python_method
    def _reply_task(self, epoch, callback, *args):
        self._reply_worker.epoch = epoch
        self._reply_worker.context = self._active_context
        try:
            return callback(*args)
        finally:
            del self._reply_worker.epoch
            del self._reply_worker.context

    @objc.python_method
    def _reply_current(self):
        self.reload_conversations()
        return (self._capture_allowed()
                and self._reply_key is not None and not self._paused
                and getattr(self._reply_worker, "epoch", self._reply_epoch) == self._reply_epoch)

    @objc.python_method
    def _push_reply(self, selector, payload, epoch):
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "applyReplyUpdate:", (epoch, selector, payload), False)

    def applyReplyUpdate_(self, update):
        self.reload_conversations()
        epoch, selector, payload = update
        if not self._capture_allowed() or epoch != self._reply_epoch:
            return
        if selector not in {"applyWaiting:", "applyError:"} and not self._reply_current():
            return
        getattr(self, selector.replace(":", "_"))(payload)

    def applyWaiting_(self, _payload):
        self._show()
        self._last_intent = ""
        self._last_risk = 0.0
        self._clear_candidates()
        self._stream_rows = {}
        for key in ("intent", "confidence", "risk", "actions"):
            self._render(key, "", PALETTE["muted"])
        data = _payload if isinstance(_payload, dict) else {}
        self._render("message", data.get("read_text", ""), PALETTE["muted"])
        self._render("sender", self._read_result_meta(data.get("count", 0)),
                     PALETTE["muted"])
        if hasattr(self, "_risk_dots"):
            self._set_risk_scale(None)
        if hasattr(self, "_set_candidate_header"):
            self._set_candidate_header("候选回复")
        else:
            # Lightweight test harnesses load this callback without constructing AppKit.
            self.rows["cand_header"].setStringValue_("候选回复")
        self._render("status", data.get("reason") or (
            _payload if isinstance(_payload, str) else None)
            or "等待可确认的对方消息…", PALETTE["muted"])

    def applyUnreadable_(self, payload):
        """A later incoming bubble exists, but OCR cannot safely supply its text."""
        read_text, count = payload
        self._show()
        self._last_intent = ""
        self._last_risk = 0.0
        self._clear_candidates()
        self._stream_rows = {}
        self._render("sender", self._read_result_meta(count, "最新一条未识别"),
                     PALETTE["amber"])
        self._render("message", read_text, PALETTE["text"])
        for key in ("intent", "confidence", "risk", "actions"):
            self._render(key, "", PALETTE["muted"])
        if hasattr(self, "_risk_dots"):
            self._set_risk_scale(None)
        self._set_candidate_header("候选回复 · 已暂停")
        self._render("status", "检测到最新对方气泡，但文字未识别 · 已暂停生成",
                     PALETTE["amber"])

    # --- main-thread callbacks (AppKit is not thread safe)
    def applyChat_(self, title):
        self._chat_title = title
        self._render("chat", title, PALETTE["accent"])

    def applyIncoming_(self, payload):
        # a new message landed but we are not analysing yet (burst in progress):
        # keep the previous verdict visible, just badge it
        text, sender, prev, read_text, count = payload
        self._show()
        self._render("status", "有新消息 · 等消息停稳…", PALETTE["muted"])
        self._render("message", read_text, PALETTE["muted"])
        self._render("sender", self._read_result_meta(count, "当前目标：对方"),
                     PALETTE["muted"])

    def applyPending_(self, payload):
        text, sender, prev, read_text, count = payload
        self._show()
        self._render("status", "分析中…" if self._judgment_enabled else "生成回复中…",
                     PALETTE["muted"])
        self._render("message", read_text, PALETTE["text"])
        self._render("sender", self._read_result_meta(count, "当前目标：对方"),
                     PALETTE["muted"])
        self._clear_candidates()
        self._stream_rows = {}     # a new run starts at line zero in every slot
        self._set_candidate_header("候选回复 · 等待判断…" if self._judgment_enabled
                                   else "候选回复 · 生成中…")

    def applyJudgment_(self, payload):
        v, sender, prev, read_text, count = payload
        self._show()
        # kept so a 话术 change can re-rank the new candidates against the same verdict
        self._last_intent = v.get("intent", "")
        self._last_risk = v.get("risk", 0)   # and so the overlay can badge the message
        self._render("message", read_text or v["message"], PALETTE["text"])
        self._render("sender", self._read_result_meta(count, "当前目标：对方"),
                     PALETTE["muted"])
        backend = v.get("backend", "")
        if backend.startswith("local (Jev"):
            # the backend label is "local (Jev-shaped decider-2b)": take what is inside the
            # parens without the paren, or the status line reads "... decider-2b)"
            detail = backend.split("(", 1)[1].rstrip(")")
            self._render("status", f"本地兜底 · {detail[:26]}", PALETTE["amber"])
        elif backend:
            self._render("status", f"分析完成 · {backend}", PALETTE["muted"])
        else:
            self._render("status", "分析完成", PALETTE["muted"])
        self._render("intent", v["intent"], PALETTE["text"])
        # the intent recognition rate, read off the judged intent — same muted slot
        self._render("confidence", f"意图识别率 {v['confidence']:.0%}", PALETTE["muted"])
        # Rounded, so the panel does not claim a precision it has: the judge reports a
        # mean like 4.7 out of a 10-level distribution, and "4.7/9" reads as a measurement
        # while "5/9" reads as the estimate it is. Deliberately the mean and not the most
        # likely level — measured on 8 real messages, this model's top level never exceeds
        # 0.4 and the argmax jumps 1/3/6 across near-identical criticism messages, while the
        # mean holds (派活 2.0–2.4, 批评 3.0–4.0, 闲聊 1.7).
        risk = int(round(float(v.get("risk", 0))))
        label = "安全" if risk <= 3 else ("留神" if risk <= 6 else "危险")
        color = PALETTE["green"] if risk <= 3 else (
            PALETTE["amber"] if risk <= 6 else PALETTE["red"])
        self._render("risk", f"● {label}  {risk}/9", color)
        if hasattr(self, "_risk_dots"):
            self._set_risk_scale(risk)
        self._render("actions", " · ".join(v.get("actions", [])), PALETTE["text"])
        self._set_candidate_header("候选回复 · 生成中…")
        # the verdict landing starts a new candidate run: without this reset, the streamed
        # line counters left over from the previous message would eat every new line
        # (applyStreamLine_ drops rows beyond PER_TONE) — only applyPending_ and
        # toneChanged_ used to reset it, and the pre-judged path goes through neither
        self._stream_rows = {}

    def applyCandidates_(self, payload):
        if not self._payload_current(payload):
            return
        self._set_candidate_header(self._cand_header(payload))
        self._render_groups(payload)

    def applyStreamLine_(self, payload):
        """One streamed candidate line, shown the moment it completes (not ranked yet).

        applyCandidates_ re-fills every row with scores when the full result lands, so the
        "#n" here is only "nth line of this tone" and the score slot reads as pending. A
        superseded run's lines are dropped by the epoch check — a tone change or a new
        message starting mid-stream must not write into the new run's rows.
        """
        epoch, slot, text = payload
        if epoch != self._gen_epoch or not self._slot_active(slot):
            return
        row = self._stream_rows.get(slot, 0)
        if row >= self.candidate_count:
            return                       # the prompt asks for this many lines; extras stray
        self._stream_rows[slot] = row + 1
        tag = slot * styles.PER_TONE + row
        self.cand_texts[tag] = text
        self._candidate_sources[tag] = text
        if self._collapsed:
            return      # collapse keeps the data; _set_collapsed(False) puts it back up
        r = self._rows[slot][row]
        self._set_probability_label(r["prob"], row,
                                    "排序中" if self._judgment_enabled else "原序")
        r["text"].setStringValue_(text)
        self._set_progress(slot, row, None)
        for c in self._row_controls(slot, row):
            c.setHidden_(False)
        if not self._judgment_enabled:
            r["track"].setHidden_(True)
            r["fill"].setHidden_(True)
        self._relayout()

    def applyError_(self, text):
        self._show()                       # never vanish without telling the user why
        self._render("status", text, PALETTE["red"])

    def applyStatus_(self, text):
        """A neutral status line pushed from a worker thread (e.g. the warm-up)."""
        self._render("status", text, PALETTE["muted"])

    def applyWarmFailed_(self, text):
        """Warm-up failure: same as applyError_ but outside the reply-epoch guard.

        The warm-up is not a reply run — a message that starts analysing while it
        fails must not be able to swallow this line like it swallows late results.
        It is still a global panel, though: with the chat app in the background the red
        line must not surface over other apps (the foreground boundary stays hard).
        The hint is not lost — every later analysis that hits LowMemoryError reports
        it again through the epoch-guarded applyError_ path, which the chat app
        foregrounds.
        """
        if self._app is None:
            return
        self._show()                       # never vanish without telling the user why
        self._render("status", text, PALETTE["red"])

    def applyWarmDone_(self, _payload):
        """Clear the warm-up line, but only if nothing more urgent replaced it.

        The first load can run for minutes; a message that arrived and got analysed in
        that window owns the status line now, and this must not steal it back.
        """
        if self.rows["status"].stringValue() == WARM_STATUS:
            self._render("status", IDLE_STATUS, PALETTE["muted"])

    def applyHidden_(self, reason):
        # WeChat gone or unreadable -> take the panel away (the app "opens with WeChat")
        self._render("status", reason, PALETTE["muted"])
        if (getattr(self, "auto_hide", True) and self.panel.isVisible()
                and not self.judge.load_status):
            self.panel.orderOut_(None)
        if self._ov_panel.isVisible():
            self._ov_panel.orderOut_(None)

    def applyForegroundHidden_(self, reason):
        """Hide a global panel without leaving stale conversation state behind."""
        self._last_intent = ""
        self._last_risk = 0.0
        self._stream_rows = {}
        self._chat_title = ""
        self._clear_candidates()
        for key in ("chat", "message", "sender", "intent", "confidence", "risk", "actions"):
            self._render(key, "", PALETTE["muted"])
        self.rows["cand_header"].setStringValue_("候选回复")
        self.applyHidden_(reason)

    def applyForegroundShown_(self, _payload):
        """Reveal the panel state accumulated by an opted-in background capture."""
        self._show()

    def applyPosition_(self, win):
        if (self._wechat_frontmost is not True
                or not getattr(self, "auto_dock", True)
                or getattr(self, "_calibrating", False)):
            return
        self._position_near(win)

    # --- YOLO overlay callbacks (visual only; see _build_overlay)
    def applyBoxes_(self, payload):
        """Repaint the overlay from the last read's window geometry + messages."""
        if self._app is None or getattr(self, "_calibrating", False) or not self._show_boxes:
            return
        win, msgs, newest_text, newest_message = payload
        W, H = win["w"], win["h"]
        flip = self._display_height()
        # top-left (Quartz) -> bottom-left (Cocoa), covering WeChat exactly
        self._ov_panel.setFrame_display_(
            NSMakeRect(win["x"], flip - win["y"] - H, W, H), False)
        font = (NSFont.fontWithName_size_("Menlo-Bold", 10)
                or NSFont.boldSystemFontOfSize_(10))
        judged = (newest_text is not None and newest_text == self.analyzed_text
                  and bool(self._last_intent))
        risk = int(round(float(self._last_risk)))
        boxes = []
        for m in msgs:
            if m.w <= 0:
                continue               # pre-overlay geometry: nothing to draw
            who = m.sender or {"them": "对方", "me": "我", "public": "公共信息"}.get(
                m.side, "方向未确认")
            label = f"{who} {m.conf:.2f}"
            if judged and m is newest_message:
                color = (PALETTE["green"] if risk <= 3 else
                         PALETTE["amber"] if risk <= 6 else PALETTE["red"])
                lw = 2.5
                label += f" · {self._last_intent} 风险{risk}/9"
            else:
                color = ({"me": _rgb(0x576B95), "them": PALETTE["green"],
                          "public": PALETTE["muted"]}.get(m.side, PALETTE["amber"]))
                lw = 1.5
            chip = NSAttributedString.alloc().initWithString_attributes_(
                label,
                {NSFontAttributeName: font,
                 NSForegroundColorAttributeName: NSColor.whiteColor(),
                 NSBackgroundColorAttributeName: color.colorWithAlphaComponent_(0.85)})
            y = H - (m.y + m.h) * H     # normalized top-origin -> view bottom-origin
            boxes.append((NSMakeRect(m.x * W, y, m.w * W, m.h * H), color, lw, chip))
        target = getattr(self, "_input_target", None)
        if target and target["window"] == win and target["rect"]:
            x, y, w, h = target["rect"]
            color = _rgb(0x2478DD)
            rect = NSMakeRect(x-win["x"], H-(y-win["y"])-h, w, h)
            label = target["reason"]
        elif target and target["window"] == win and target.get("visual_rect"):
            x, y, w, h = target["visual_rect"]
            color = PALETTE["amber"]
            rect = NSMakeRect(x-win["x"], H-(y-win["y"])-h, w, h)
            label = ("虚线：手动输入区 · 有草稿停止，不发送" if target.get('manual_region')
                     else "虚线：视觉输入区 · 点击填入后校验（不发送）")
        else:
            color = PALETTE["amber"]
            rect = NSMakeRect(12, 12, 0, 0)
            label = ("输入框：请点击右上角图标校准" if getattr(self,"_calibration_required",False)
                     else "输入框：" + (target["reason"] if target else "定位中…"))
        chip = NSAttributedString.alloc().initWithString_attributes_(label, {
            NSFontAttributeName: font, NSForegroundColorAttributeName: NSColor.whiteColor(),
            NSBackgroundColorAttributeName: color.colorWithAlphaComponent_(0.85)})
        boxes.append((rect, color, 2.0, chip, bool(target and target.get("visual_rect") and not target["rect"])))
        view = self._ov_panel.contentView()
        view.boxes = boxes
        view.setNeedsDisplay_(True)
        if not self._ov_panel.isVisible():
            self._ov_panel.orderFrontRegardless()

    def toggleBoxes_(self, sender):
        """Menu-bar switch; JEV_BOXES=1 in the env file makes it start on instead."""
        self._show_boxes = not self._show_boxes
        self.boxes_item.setState_(
            AppKit.NSOnState if self._show_boxes else AppKit.NSOffState)
        if not self._show_boxes and self._ov_panel.isVisible():
            self._ov_panel.orderOut_(None)

    # --------------------------------------------------------------- warm-up
    @objc.python_method
    def _warm_apps(self):
        """Pay each chat app's one-off read-path load (Vision for WeChat; QQ has none)."""
        for app in APPS:
            ms = app.warm()
            if ms is None:
                continue                  # QQ：AX 路径没有一次性加载
            if ms >= 0:
                self._read_once = True    # Vision's one-off load is paid; first read is steady-state
                _log(f"预热 {app.display_name} 读屏就绪 · {ms:.0f}ms")
            else:
                _log(f"预热 {app.display_name} 读屏失败 · 首次读屏会稍慢，不影响使用")

    @objc.python_method
    def _warm(self):
        """Pay the one-off loads in the background: Vision OCR first, then the judge model.

        The first real message used to carry both costs: Vision's ~0.7 s first OCR and
        decider-2b's 9-15 s load inside its first judge(). Starting both here, right after
        launch, moves them to idle time — the fast one first so it is ready within a
        second, the slow one after. If a message does land mid-warm-up nothing breaks:
        its judge() blocks on the model's load lock until the warm-up finishes, and the
        OCR warm-up is independent of the chat app entirely (a blank canvas, not a window).
        """
        t0 = time.perf_counter()
        self._warm_apps()

        if not self._judgment_enabled:
            _log("预热 判断功能未启用 · 仅生成候选回复")
            return

        # #37: the first decider-2b load can take minutes (download included) or die to
        # memory pressure — both used to look identical from outside: a silent panel.
        # The "loading" line says what the wait is; applyWarmDone_ clears it only if
        # nothing more urgent has replaced it in the meantime.
        local_judge = not userconfig.get("TYPESAFE_API_KEY")
        if local_judge:
            self._push("applyStatus:", WARM_STATUS)
        try:
            self.judge.warm()
        except (LowMemoryError, ModelNotDownloadedError) as e:
            # Refusal text is written for the user; show it verbatim like applyError does.
            _log(f"预热判断模型被拒 {type(e).__name__}")
            if local_judge:
                self._push("applyWarmFailed:", str(e))
        except Exception as e:
            _log(f"预热判断模型失败 {type(e).__name__}")
            if local_judge:
                self._push("applyWarmFailed:",
                           "判断模型加载失败 · 可配置 TYPESAFE_API_KEY 走云端判断")
        else:
            self._judged_once = True  # same: the load is paid, the first judge is steady-state
            _log(f"预热 判断模型就绪 · 总耗时 {(time.perf_counter() - t0) * 1000:.0f}ms")
            if local_judge:
                self._push("applyWarmDone:", None)

    # ------------------------------------------------- first-run choice (#38)
    @objc.python_method
    def _onboarding_needed(self) -> bool:
        """The #38 dialog fires exactly once: no key, nothing cached, no recorded choice."""
        return (not userconfig.get("TYPESAFE_API_KEY")
                and not judge.model_cached()
                and not userconfig.get("JUDGE_BACKEND"))

    def maybeOnboard_(self, sender):
        """Ask once how to judge: cloud key, offline model, or later.

        Runs on the main thread before the warm-up thread starts (see main()), so the
        choice is already in os.environ when _warm reads it — userconfig.get prefers the
        real environment, which is how the pick takes effect without a restart.
        """
        if not self._onboarding_needed():
            return
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("选择判断方式")
        alert.setInformativeText_(
            "未配置判断层 key。判断每条消息的意图与风险，可以用云端 key"
            "（轻量、无下载），也可以下载离线模型（约 3.8 GB，之后完全离线）。")
        accessory = AppKit.NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 360, 84))
        choices = (("cloud", "配置 key 在线判断（推荐）", "轻量、无下载，需要 TypeSafe key"),
                   ("local", "下载离线模型", "约 3.8 GB 磁盘，下载后完全离线可用"))
        radios = []
        y = 58
        for _value, title, detail in choices:
            radio = AppKit.NSButton.alloc().initWithFrame_(NSMakeRect(4, y, 348, 20))
            radio.setButtonType_(AppKit.NSRadioButton)
            radio.setTitle_(title)
            radio.setFont_(AppKit.NSFont.systemFontOfSize_(13))
            accessory.addSubview_(radio)
            radios.append(radio)
            note = ui_style.make_label(detail, 24, y - 15, 320, 14, 11,
                                       AppKit.NSColor.secondaryLabelColor())
            accessory.addSubview_(note)
            y -= 38
        radios[0].setState_(AppKit.NSControlStateValueOn)
        alert.setAccessoryView_(accessory)
        alert.addButtonWithTitle_("确定")
        alert.addButtonWithTitle_("稍后再说")
        alert.addButtonWithTitle_("打开模型设置…")
        choice = alert.runModal()
        if choice == AppKit.NSAlertFirstButtonReturn:
            picked = next((v for (v, _t, _d), r in zip(choices, radios)
                           if r.state() == AppKit.NSControlStateValueOn), "skip")
        elif choice == AppKit.NSAlertSecondButtonReturn:
            picked = "skip"           # Esc lands here too: postpone, no side effects
        else:
            picked = next((v for (v, _t, _d), r in zip(choices, radios)
                           if r.state() == AppKit.NSControlStateValueOn), "skip")
        self._record_onboarding(picked)
        if picked == "local":
            # Start the download now — rerunning _warm is cheap: the OCR half was already
            # paid at launch, and the judge half reads the choice from os.environ.
            threading.Thread(target=self._warm, daemon=True).start()
        elif choice == AppKit.NSAlertThirdButtonReturn:
            self.openSettings_(None)

    @objc.python_method
    def _record_onboarding(self, value: str) -> None:
        """Persist the choice: env file for future launches, session override for now.

        A plain os.environ write does not work here: userconfig froze its snapshot of
        the environment at import time. session_override sits in front of every source
        until the process exits, so the warm-up below sees the pick on this launch.
        """
        userconfig.session_override("JUDGE_BACKEND", value)
        try:
            import settings_config
            path = userconfig.env_files()[0]
            original = settings_config.read_document(path)
            settings_config.write_settings(path, original, {"JUDGE_BACKEND": value})
        except (ValueError, OSError) as e:
            # The session still honours the pick; a failed write just means the dialog
            # asks again next launch.
            _log(f"首次引导写入 env 失败 {type(e).__name__}")


def warn_if_no_generation_key() -> None:
    """Say it out loud at launch when the candidate half has no key behind it.

    The judgment half runs locally and needs nothing, so a panel with an empty candidate
    area reads as "the app is broken" rather than "I never configured this". One dialog at
    launch is the cheapest way to tell the two apart — it cannot be missed the way a line
    of grey text in a floating panel can.

    OPENAI_* and ANTHROPIC_* are two ways to configure the same generation layer, so this
    fires only when NEITHER is set: either one on its own is a complete configuration.
    A packaged build also carries a shared default (src/builtin.py), so this dialog only
    appears when that default was deliberately emptied out. TypeSafe is not checked — it
    has a local fallback, so it is never missing, only different.

    Drawn with osascript rather than NSAlert, which was measured to not work here: an
    accessory app cannot activate itself (NSApp.isActive stays False after
    activateIgnoringOtherApps_), and an NSAlert stayed isVisible=False even inside its own
    modal session — so the user would get nothing to click while the app sat in a modal
    loop, i.e. an app that looks hung. osascript's dialog belongs to a process that can
    activate, and Popen does not wait, so a dialog nobody dismisses cannot stall us.
    """
    if load_credentials()[1]:
        return
    path = str(userconfig.ENV_FILE).replace(str(Path.home()), "~")
    # AppleScript string escapes (\n) work inside the literal; keep it free of double quotes
    script = (
        'display alert "生成层还没配 Key，候选回复会是空的" message "'
        "意图和风险判断不受影响 —— 那部分跑在本地模型上，不需要 Key。\\n\\n"
        f"在下面的文件里填这两组中的任意一组（二选一即可），然后重启本应用：\\n{path}\\n\\n"
        "    OPENAI_API_KEY      （任意 OpenAI 兼容端点，如 DeepSeek）\\n"
        '    ANTHROPIC_API_KEY   （任意 Anthropic 兼容端点，如智谱）" as informational'
    )
    try:
        subprocess.Popen(["osascript", "-e", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass          # no osascript: the panel still shows the hint in the candidate area


def main() -> None:
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    # Selectable NSTextFields can become first responder inside our non-activating
    # panel, but Cmd+C has no responder-chain command unless an Edit menu supplies it.
    # A minimal hidden main menu keeps the HUD from stealing WeChat focus while making
    # native selection, Cmd+C and Cmd+A work exactly like ordinary macOS text.
    main_menu = AppKit.NSMenu.alloc().initWithTitle_("jev-jarvis")
    edit_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "编辑", None, "")
    edit_menu = AppKit.NSMenu.alloc().initWithTitle_("编辑")
    for title, action, key in (("复制", "copy:", "c"), ("全选", "selectAll:", "a")):
        item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            title, action, key)
        item.setTarget_(None)  # first responder: the selected read-result NSTextField
        edit_menu.addItem_(item)
    edit_item.setSubmenu_(edit_menu)
    main_menu.addItem_(edit_item)
    app.setMainMenu_(main_menu)
    warn_if_no_generation_key()
    controller = HudController.alloc().init()
    # First line of every run: which backends are actually in play. Support requests
    # always need it, and it proves the log is live before the first message arrives.
    _base, _key, _model, _src, _api = load_credentials()
    judge_name = ("TypeSafe Jev" if userconfig.get("TYPESAFE_API_KEY") else
                  "本地 decider-2b" if controller._judgment_enabled else
                  "未启用（仅生成回复）")
    _log(f"启动 · 判断层 "
         f"{judge_name}"
         f" · 生成层 {(_base + ' / ' + _model) if _key else '未配置（候选区会是空的）'}"
         + ("（内置默认）" if _src == BUILTIN_SOURCE else "")
         + (" · YOLO 框开" if controller._show_boxes else ""))
    if styles.REJECTED_TONES:
        # the dropdown silently missing a tone the user typed is a support ticket; say
        # why it was refused and what a passing description looks like, once, at startup
        _log("自定义话术未加载 · " + "；".join(styles.REJECTED_TONES))
    controller._show()
    # #38: ask a brand-new user how to judge BEFORE warming — the choice lands in
    # os.environ (and the env file), so the warm-up below honours it on this launch.
    controller.maybeOnboard_(None)
    # Warm the heavy one-off loads (Vision OCR, judge model) while the panel is idle, so
    # the user's first message pays only steady-state costs. With TypeSafe Jev configured
    # warm() is a no-op — the network path has nothing to load.
    threading.Thread(target=controller._warm, daemon=True).start()
    timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        FAST_TICK, controller, "tick:", None, True)
    AppKit.NSRunLoop.currentRunLoop().addTimer_forMode_(timer, AppKit.NSDefaultRunLoopMode)
    app.run()


if __name__ == "__main__":
    main()
