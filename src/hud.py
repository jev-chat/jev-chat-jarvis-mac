"""Floating HUD: a non-activating panel beside WeChat showing intent, risk and ranked replies.

Design notes
  * NSWindowStyleMaskNonactivatingPanel + floating level: the panel never steals focus
    from WeChat, and window-ID capture means it never appears in our own screenshots.
  * Poll loop: read the chat, hash the newest message, judge only when it changes.
  * Judgment lands first (fast, ~0.5 s) and candidates fill in when the generator
    finishes (~2 s), mirroring the phone demo's "生成中…" state.
  * The panel positions itself against WeChat's window each tick, so it follows moves,
    resizes and monitor changes without any window-server hooks.
  * Palette is WeChat's light theme (see PALETTE below); the Appearance is pinned to Aqua
    so the title bar and button bezels stay light even when the system is in dark mode.
  * 「填入」 writes through the Accessibility API into WeChat's input box (src/fill.py): no
    synthetic keystrokes, no clipboard, and nothing needs to be frontmost. It needs the
    Accessibility permission; when that is missing the HUD asks for it and reports the
    failure.
"""

from __future__ import annotations

import objc
import os
import subprocess
import threading
import time
from pathlib import Path

import AppKit
import sys

from AppKit import (
    NSAppearance,
    NSBackingStoreBuffered,
    NSBezelStyleRounded,
    NSButton,
    NSColor,
    NSFont,
    NSPanel,
    NSPasteboard,
    NSPasteboardTypeString,
    NSPopUpButton,
    NSScreen,
    NSTextField,
    NSView,
    NSWindowMiniaturizeButton,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskNonactivatingPanel,
    NSWindowStyleMaskTitled,
    NSWindowZoomButton,
    NSWindowCloseButton,
)
from Foundation import NSMakeRect, NSMakeSize, NSObject, NSTimer

sys.path.insert(0, str(Path(__file__).parent))
import userconfig  # noqa: E402

userconfig.load()   # ~/.config/jev-jarvis/env -> os.environ (Finder apps inherit none)

from perception import (  # noqa: E402
    read_conversation, screen_capture_ok, request_screen_capture, warm_ocr)
from judge import make_judge  # noqa: E402
from generate import Generator, load_credentials  # noqa: E402
import styles  # noqa: E402
import fill  # noqa: E402

PANEL_W, PANEL_H = 360, 614   # tall enough for 3-line candidates + the chat name row
COLLAPSED_H = 96              # height when the panel is rolled up
# The tick timer fires at FAST_TICK; a read only runs when due. A quiet screen (fingerprint
# match ⇒ no OCR) re-checks every FAST_TICK — a new message surfaces within 0.25 s instead
# of within 1 s — while a moving screen (someone typing a burst) pays the full capture+OCR
# per read and drops back to SLOW_TICK, the cadence the old fixed poll had.
FAST_TICK = 0.25         # re-check cadence while the chat pane is quiet
SLOW_TICK = 1.0          # re-check cadence while the chat pane is moving
SETTLE_S = 1.2           # wait this long with no new message before analysing (anti-flood)
MIN_GAP_S = 2.0          # never restart analysis faster than this
CONTEXT_TURNS = 4       # how many recent turns both halves get to see


# ---------------------------------------------------------------- palette
# WeChat's light theme: a #F7F7F7 surface, near-black body text, #888888 for anything
# secondary, and the brand green/amber/red carrying the risk state.


def _rgb(hex_code: int, alpha: float = 1.0) -> NSColor:
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(
        ((hex_code >> 16) & 0xFF) / 255.0,
        ((hex_code >> 8) & 0xFF) / 255.0,
        (hex_code & 0xFF) / 255.0,
        alpha,
    )


PALETTE = {
    "bg": _rgb(0xF7F7F7),     # panel surface
    "text": _rgb(0x191919),   # judged message, intent, candidates, action advice
    "muted": _rgb(0x888888),  # status, sender/context, confidence, percentages, headers
    "green": _rgb(0x07C160),  # WeChat brand green — risk 安全, success feedback
    "amber": _rgb(0xFA9D3B),  # risk 留神
    "red": _rgb(0xFA5151),    # risk 危险, failures
    # The 话术 dropdown is drawn as a WeChat-style field: a flat light surface with a
    # hairline, because the stock popup bezel brings the system accent colour (a blue
    # chevron) into a panel that has no other system-accent pixel in it.
    "field": _rgb(0xF2F2F2),
    "edge": _rgb(0xE3E3E3),
}

# Candidate row geometry. A row is 48 pt tall inside a 56 pt pitch, so rows keep the same
# breathing room as before; prob and buttons share the text's bottom edge.
CAND_BTN_W, CAND_BTN_H, CAND_BTN_GAP = 56, 24, 4
CAND_BTN_X = PANEL_W - 14 - (2 * CAND_BTN_W + CAND_BTN_GAP)   # 230
# Rank/percentage label ("#3 · 100%"): NSTextField's cell insets mean the widest string
# actually consumes 63 px at 11 pt, so the old 48 px frame clipped the "%" off every row.
# 72 px still clears that with 9 px to spare, and the 12 px it gives back go to the
# candidate text — which needs them: at 116 px a 30-character candidate (the generation
# prompt's own cap) lost its last two characters to the 3-line limit.
CAND_PROB_X, CAND_PROB_W = 14, 72                              # 14 .. 86
CAND_TEXT_X = CAND_PROB_X + CAND_PROB_W + 8                    # 94
CAND_TEXT_W = CAND_BTN_X - CAND_TEXT_X - 8                     # 128
CAND_TEXT_H = 48                                                # up to 3 wrapped lines
CAND_ROW_H = 56                                                 # vertical pitch of one row

# 话术 groups. Each group is headed by its dropdown; its candidates sit under it. The panel
# is only as tall as the groups in use, so nothing is reserved for a tone that is switched
# off (that reservation is what used to leave a dead gap in the middle).
TONE_DD_X, TONE_DD_W, TONE_DD_H, TONE_DD_GAP = 14, PANEL_W - 28, 24, 6
TONE_DD_INSET = 6         # the popup sits this far inside its field, like text in an input box
TONE_DD_FONT = 13         # bigger than the 11 pt labels: it is a control, and it is the one
                          # thing on the panel the user is meant to click
GROUP_GAP = 12            # between one group's rows and the next group's dropdown
BOTTOM_PAD = 18           # below the last group


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


class HudController(NSObject):
    def init(self):
        self = objc.super(HudController, self).init()
        if self is None:
            return None
        self.last_seen = None          # newest message text observed
        self.last_change_ts = 0.0      # when it last changed (burst detection)
        self.last_analyze_ts = 0.0     # rate limit for analysis starts
        self.analyzed_text = None      # what the panel currently shows
        self._judged_once = False      # first judge call includes the local model load
        self._read_once = False        # first OCR call includes Vision's own load
        self._last_skip_reason = None
        self.judge = make_judge()
        self.generator = Generator()
        # 话术: per-slot tone selection. A slot on 不用 contributes no request and no rows,
        # so the panel is exactly as tall as the groups actually in use.
        self.slot_tones = list(styles.DEFAULT_SLOTS) + [styles.NONE_LABEL]
        self.slot_tones = self.slot_tones[:styles.MAX_SLOTS]
        while len(self.slot_tones) < styles.MAX_SLOTS:
            self.slot_tones.append(styles.NONE_LABEL)
        self._dds: list = []
        self._dd_boxes: list = []       # the flat fields the dropdowns are drawn into
        self._rows: list = []
        self._fixed: list = []          # (control, x, dy_from_top, w, h) — the rows above
        self._group_top = 0             # where the first group starts, from the top
        self._title_h = 28              # measured right after the panel is built
        self.cand_texts: list[str | None] = [None] * (styles.MAX_SLOTS * styles.PER_TONE)
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
        # Pre-judgment: the local judge starts the moment a new message is seen, and the
        # settle gate consumes the verdict if the text is unchanged — intent/risk land on
        # screen ~1 s earlier and only the (paid) generation half still waits. Single-slot
        # request = latest-wins: a newer text overwrites the slot and retires the verdict.
        self._model_lock = threading.Lock()   # never two local forwards (judge/rank) at once
        self._prejudge_req = None             # latest-wins slot: (text, context, sender, prev)
        self._prejudge_result = None          # (text, verdict, sender, prev), spent at settle
        self._prejudging = False              # a pre-judge forward is running right now
        self._prejudge_event = threading.Event()
        threading.Thread(target=self._prejudge_loop, daemon=True).start()
        self._collapsed = False
        self._expanded_h = None       # full height, captured the first time we collapse
        self._paused = False
        self._chat_title = ""
        self._asked_permission = False
        self._win_wid = None          # sticky WeChat window id
        self._last_origin = None      # last applied panel origin
        self._pending_origin = None   # candidate origin awaiting confirmation
        self._build_panel()
        self._expanded_h = self.panel.frame().size.height
        return self

    # ------------------------------------------------------------------ ui
    @objc.python_method
    def _build_panel(self):
        # Closable/Miniaturizable are what actually CREATE the standard window buttons;
        # NonactivatingPanel alone gives a title bar with no controls at all.
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskNonactivatingPanel)
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, PANEL_W, PANEL_H), style, NSBackingStoreBuffered, False)
        self.panel.setLevel_(AppKit.NSFloatingWindowLevel)
        self.panel.setOpaque_(False)
        self.panel.setAlphaValue_(1.0)   # light surfaces go grey/washed out below 1.0
        # The title bar and button bezels are drawn from the appearance, not from the
        # background colour, so pin Aqua: a dark-mode system would otherwise give a dark
        # title bar above a white panel.
        self.panel.setAppearance_(NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameAqua))
        self.panel.setBackgroundColor_(PALETTE["bg"])
        self.panel.setTitle_("jev-jarvis")
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setBecomesKeyOnlyIfNeeded_(True)

        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, PANEL_W, PANEL_H))
        # Paint the panel colour on the view itself rather than leaning on the window's
        # background colour: _relayout() grows and shrinks this view, and a region that
        # appears after a resize is not reliably covered by the window behind it. It also
        # makes offscreen renders (cacheDisplayInRect_) show what the screen shows — a
        # transparent view renders black there and hides real layout problems.
        view.setWantsLayer_(True)
        view.layer().setBackgroundColor_(PALETTE["bg"].CGColor())
        self.rows: dict[str, NSTextField] = {}

        # Layout order matters: the message being judged is the anchor of the panel,
        # so it sits right under the title in the brightest, largest type.
        # The full-width rows (PANEL_W - 28 = 332 px) cannot clip their widest string:
        # "意图识别率 100%" measures 103 px at 12 pt.
        # Every control is created once and then placed by _relayout(), which is what lets
        # the panel change height when the tone selection changes.
        dy = 30
        for key, size, color, bold, height in (
            ("chat", 12, PALETTE["green"], True, 18),      # 群名 / 联系人
            ("status", 10, PALETTE["muted"], False, 14),
            ("message", 15, PALETTE["text"], False, 50),      # the message under analysis
            ("sender", 10, PALETTE["muted"], False, 14),
            ("intent", 21, PALETTE["text"], True, 28),
            ("confidence", 12, PALETTE["muted"], False, 18),
            ("risk", 14, PALETTE["green"], True, 20),
            ("actions", 13, PALETTE["text"], False, 18),
        ):
            tf = self._make_label(14, 0, PANEL_W - 28, height,
                                  size=size, color=color, bold=bold)
            if key == "message":
                tf.cell().setWraps_(True)
            view.addSubview_(tf)
            self.rows[key] = tf
            self._fixed.append((tf, 14, dy, PANEL_W - 28, height))
            dy += height + 8

        # ---- candidates section
        dy += 6
        header = self._make_label(14, 0, PANEL_W - 28, 16,
                                  size=11, color=PALETTE["muted"])
        header.setStringValue_("候选回复（按合适度排序）")
        view.addSubview_(header)
        self.rows["cand_header"] = header
        self._fixed.append((header, 14, dy, PANEL_W - 28, 16))
        dy += 22
        self._group_top = dy

        # ---- 话术 groups: each dropdown heads a group and its candidates sit underneath,
        # so the tone is labelled by the thing that selects it. Every group's controls exist
        # from the start; _relayout() decides which are on screen. The button tags are slot
        # arithmetic (slot * PER_TONE + row) so they never shift when a group's results are
        # still in flight.
        tone_items = styles.labels() + [styles.NONE_LABEL]
        for slot in range(styles.MAX_SLOTS):
            # the field the popup sits in: a flat surface with a hairline, drawn by us so
            # the control carries no system-accent chrome
            box = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, TONE_DD_W, TONE_DD_H))
            box.setWantsLayer_(True)
            box.layer().setBackgroundColor_(PALETTE["field"].CGColor())
            box.layer().setBorderColor_(PALETTE["edge"].CGColor())
            box.layer().setBorderWidth_(1.0)
            box.layer().setCornerRadius_(5.0)
            view.addSubview_(box)
            self._dd_boxes.append(box)

            pop = NSPopUpButton.alloc().initWithFrame_pullsDown_(
                NSMakeRect(0, 0, TONE_DD_W - 2 * TONE_DD_INSET, TONE_DD_H), False)
            pop.setBordered_(False)          # <- no bezel, no accent-coloured chevron
            pop.setFont_(NSFont.systemFontOfSize_(TONE_DD_FONT))
            pop.addItemsWithTitles_(tone_items)
            pop.selectItemWithTitle_(self.slot_tones[slot])
            pop.setTarget_(self)
            pop.setAction_("toneChanged:")
            view.addSubview_(pop)
            self._dds.append(pop)

            slot_rows = []
            for row in range(styles.PER_TONE):
                tag = slot * styles.PER_TONE + row
                prob = self._make_label(CAND_PROB_X, 0, CAND_PROB_W, 14,
                                        size=11, color=PALETTE["muted"])
                text = self._make_label(CAND_TEXT_X, 0, CAND_TEXT_W, CAND_TEXT_H,
                                        size=12, color=PALETTE["text"])
                text.cell().setWraps_(True)
                copy_btn = self._make_button(CAND_BTN_X, 0, CAND_BTN_W, CAND_BTN_H,
                                             "复制", "copyCandidate:", tag)
                fill_btn = self._make_button(CAND_BTN_X + CAND_BTN_W + CAND_BTN_GAP, 0,
                                             CAND_BTN_W, CAND_BTN_H, "填入", "fillCandidate:", tag)
                for c in (prob, text, copy_btn, fill_btn):
                    view.addSubview_(c)
                slot_rows.append({"prob": prob, "text": text,
                                  "btn": copy_btn, "fill_btn": fill_btn})
            self._rows.append(slot_rows)

        self.panel.setContentView_(view)
        self._title_h = self.panel.frame().size.height - PANEL_H   # measured, not assumed
        self._relayout()
        self.rows["status"].setStringValue_("等待微信消息…")
        self._wire_window_controls()
        self._install_status_item()

    @objc.python_method
    def _slot_active(self, slot: int) -> bool:
        return self.slot_tones[slot] in styles.PRESETS

    @objc.python_method
    def _relayout(self):
        """Place every control for the current tone selection and size the panel to fit.

        Two things are computed here rather than at build time. Positions are measured from
        the TOP, so when the panel grows or shrinks nothing above the change moves — only the
        bottom edge does. And the height follows the groups in use: a slot on 不用 reserves
        neither a dropdown's worth of rows nor its candidates, which is what removes the dead
        space a fixed-height panel left in the middle.
        """
        dy = self._group_top
        placements = []          # (control, x, dy_from_top, w, h)
        for slot in range(styles.MAX_SLOTS):
            placements.append((self._dd_boxes[slot], TONE_DD_X, dy, TONE_DD_W, TONE_DD_H))
            placements.append((self._dds[slot], TONE_DD_X + TONE_DD_INSET, dy,
                               TONE_DD_W - 2 * TONE_DD_INSET, TONE_DD_H))
            dy += TONE_DD_H + TONE_DD_GAP
            active = self._slot_active(slot)
            for row in range(styles.PER_TONE):
                r = self._rows[slot][row]
                controls = (r["prob"], r["text"], r["btn"], r["fill_btn"])
                if active:
                    # row height is reserved whether or not the candidates have arrived, so
                    # nothing jumps when results land mid-generation
                    placements += [
                        (r["text"], CAND_TEXT_X, dy, CAND_TEXT_W, CAND_TEXT_H),
                        (r["prob"], CAND_PROB_X, dy + 34, CAND_PROB_W, 14),
                        (r["btn"], CAND_BTN_X, dy + 24, CAND_BTN_W, CAND_BTN_H),
                        (r["fill_btn"], CAND_BTN_X + CAND_BTN_W + CAND_BTN_GAP, dy + 24,
                         CAND_BTN_W, CAND_BTN_H),
                    ]
                    dy += CAND_ROW_H
                else:
                    for c in controls:
                        c.setHidden_(True)
            if slot < styles.MAX_SLOTS - 1:
                dy += GROUP_GAP

        content_h = dy + BOTTOM_PAD
        view = self.panel.contentView()
        view.setFrameSize_(NSMakeSize(PANEL_W, content_h))
        for ctrl, x, top, w, h in placements + self._fixed:
            ctrl.setFrame_(NSMakeRect(x, content_h - top - h, w, h))

        # resize the window with its TOP edge pinned: growing downwards is what the eye
        # expects here, and _position_near() anchors the panel to WeChat's top anyway
        f = self.panel.frame()
        top = f.origin.y + f.size.height
        frame_h = content_h + self._title_h
        self.panel.setFrame_display_(
            NSMakeRect(f.origin.x, top - frame_h, PANEL_W, frame_h), True)
        self._expanded_h = frame_h

    @objc.python_method
    def _wire_window_controls(self):
        """Native traffic lights, mapped to this app's actions.

        red    -> quit. A hidden panel would otherwise be unreachable: LSUIElement apps
                  have no Dock icon, so a plain order-out looks like a crash.
        yellow -> roll the panel up instead of miniaturizing, for the same reason.
        green  -> hidden: the HUD has a fixed size and nothing to zoom.
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
            zoom.setHidden_(True)

    @objc.python_method
    def _install_status_item(self):
        """Menu-bar item — the standard place for a background helper's controls."""
        bar = AppKit.NSStatusBar.systemStatusBar()
        self.status_item = bar.statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        self.status_item.button().setTitle_("J")
        self.status_item.button().setToolTip_("jev-jarvis · 微信意图助手")

        menu = AppKit.NSMenu.alloc().init()
        for title, action, key in (
            ("显示 / 收起面板", "collapsePanel:", ""),
            ("暂停读屏", "togglePause:", ""),
            ("立即重新分析", "reanalyze:", ""),
        ):
            menu.addItemWithTitle_action_keyEquivalent_(title, action, key)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        menu.addItemWithTitle_action_keyEquivalent_("退出 jev-jarvis", "quitApp:", "q")
        for item in menu.itemArray():
            item.setTarget_(self)
        self.pause_item = menu.itemArray()[1]
        self.status_item.setMenu_(menu)

    @objc.python_method
    def _make_label(self, x, y, w, h, size=13, color=None, bold=False):
        tf = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
        tf.setStringValue_("")
        tf.setBezeled_(False)
        tf.setDrawsBackground_(False)
        tf.setEditable_(False)
        tf.setSelectable_(True)
        tf.setTextColor_(PALETTE["text"] if color is None else color)
        tf.setFont_(NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size))
        return tf

    @objc.python_method
    def _make_button(self, x, y, w, h, title, action, tag):
        """A native rounded bezel with a WeChat-green label — reads correctly on #F7F7F7."""
        btn = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
        btn.setTitle_(title)
        btn.setBezelStyle_(NSBezelStyleRounded)
        btn.setFont_(NSFont.systemFontOfSize_(11))
        btn.setContentTintColor_(PALETTE["green"])
        btn.setTarget_(self)
        btn.setAction_(action)
        btn.setTag_(tag)
        btn.setHidden_(True)
        return btn

    @objc.python_method
    def _show(self):
        if not self.panel.isVisible():
            self.panel.orderFrontRegardless()

    @objc.python_method
    def _context_line(self, sender, prev: str) -> str:
        parts = []
        if sender:
            parts.append(f"来自 {sender}")
        if prev:
            parts.append(f"上文：{prev[:26]}")
        return " · ".join(parts)

    @objc.python_method
    def _render(self, key: str, text: str, color: NSColor | None = None):
        tf = self.rows[key]
        tf.setStringValue_(text)
        if color is not None:
            tf.setTextColor_(color)

    @objc.python_method
    def _row_controls(self, slot: int, row: int):
        r = self._rows[slot][row]
        return (r["prob"], r["text"], r["btn"], r["fill_btn"])

    @objc.python_method
    def _render_groups(self, payload: list):
        """payload: [(slot, tone, [{"text","prob"}, ...]), ...] — one entry per active tone.

        Rows the model did not fill are emptied and their buttons hidden, but the row keeps
        its space: the panel's height is decided by the tone selection, not by how many lines
        came back, so a late result cannot resize the panel under the cursor.
        """
        wanted = set()
        for slot, _tone, items in payload:
            for row in range(styles.PER_TONE):
                if row < len(items):
                    it = items[row]
                    wanted.add((slot, row))
                    r = self._rows[slot][row]
                    r["prob"].setStringValue_(f"#{row + 1} · {it['prob'] * 100:.0f}%")
                    r["text"].setStringValue_(it["text"])
                    for c in self._row_controls(slot, row):
                        c.setHidden_(not self._slot_active(slot))
                    self.cand_texts[slot * styles.PER_TONE + row] = it["text"]
        for slot in range(styles.MAX_SLOTS):
            for row in range(styles.PER_TONE):
                if (slot, row) not in wanted and self._slot_active(slot):
                    r = self._rows[slot][row]
                    r["prob"].setStringValue_("")
                    r["text"].setStringValue_("")
                    r["btn"].setHidden_(True)
                    r["fill_btn"].setHidden_(True)
                    self.cand_texts[slot * styles.PER_TONE + row] = None
        self._relayout()

    @objc.python_method
    def _clear_candidates(self):
        for slot in range(styles.MAX_SLOTS):
            for row in range(styles.PER_TONE):
                r = self._rows[slot][row]
                r["prob"].setStringValue_("")
                r["text"].setStringValue_("")
                r["btn"].setHidden_(True)
                r["fill_btn"].setHidden_(True)
                self.cand_texts[slot * styles.PER_TONE + row] = None

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
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(text, NSPasteboardTypeString)
        self._render("status", "已复制", PALETTE["green"])

    def fillCandidate_(self, sender):
        """Write the candidate into WeChat's input box (src/fill.py)."""
        idx = sender.tag()
        text = self.cand_texts[idx] if 0 <= idx < len(self.cand_texts) else None
        if not text:
            return
        # The status line is painted before the call because writing into WeChat takes a
        # beat; the click should look instant even though the write has not happened yet.
        self._render("status", "填入中…", PALETTE["muted"])
        self.panel.displayIfNeeded()
        if not fill.has_accessibility():
            # First click is the moment to ask: the system dialog is the only way in.
            fill.request_accessibility()
        ok, reason = fill.fill_text(text)
        if ok:
            self._render("status", "已填入", PALETTE["green"])
        else:
            self._render("status", f"填入失败：{reason}", PALETTE["red"])

    def toneChanged_(self, sender):
        """A 话术 dropdown moved: the verdict is still valid, only the writing changes."""
        picked = [p.titleOfSelectedItem() or styles.NONE_LABEL for p in self._dds]
        if picked == self.slot_tones:
            return
        self.slot_tones = picked
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
        self.rows["cand_header"].setStringValue_("候选回复 · 生成中…")
        threading.Thread(target=self._regen_work,
                         args=(text, self._last_intent, list(self.slot_tones)),
                         daemon=True).start()

    @objc.python_method
    def _grouped_payload(self, gen: dict, message: str, intent: str):
        """Generation result -> [(slot, tone, items)], with each group ranked.

        One ranking pass covers every candidate the requests produced, and each group is then
        ordered by that shared score. So `#1`/`#2` inside a group means "the better of these
        two", not "whichever line the model wrote first" — and it costs one forward pass
        rather than one per tone.
        """
        groups = [g for g in (gen.get("groups") or []) if g.get("texts")]
        if not groups:
            return None, (gen.get("error") or "空结果")
        texts = [t for g in groups for t in g["texts"]]
        scores: dict[str, float] = {}
        if intent:
            try:
                with self._model_lock:   # never two local forwards at once
                    ranked = self.judge.rank_candidates(message, intent, texts)
                scores = {r["text"]: r["prob"] for r in ranked}
            except Exception:
                scores = {}
        payload = []
        for g in groups:
            items = [{"text": t, "prob": scores.get(t, 0.0)} for t in g["texts"]]
            items.sort(key=lambda x: -x["prob"])
            payload.append((g["slot"], g["tone"], items))
        return payload, ""

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
        self._gen_epoch += 1
        epoch = self._gen_epoch
        prefix = f"{label} " if label else ""
        first_line = {"shown": False}

        def on_candidate(slot: int, _tone: str, text: str) -> None:
            if not first_line["shown"]:
                first_line["shown"] = True
                _log(f"{prefix}首条候选上屏 {(time.perf_counter() - t0) * 1000:.0f}ms（未排序）")
            self._push("applyStreamLine:", (epoch, slot, text))
        return on_candidate

    @objc.python_method
    def _regen_work(self, text: str, intent: str, slot_tones: list[str]):
        t0 = time.perf_counter()
        try:
            gen = self.generator.generate(text, intent, slot_tones, None,
                                          self._stream_hook(t0, "换话术"))
            groups = gen.get("groups") or []
            failed = [f"{g['tone']}({g['error'][:40]})" for g in groups if g.get("error")]
            _log(f"换话术 生成 {gen.get('elapsed_s', 0) * 1000:.0f}ms · {len(groups)} 个话术"
                 + (f" · 失败: {'; '.join(failed)}" if failed else ""))
            payload, err = self._grouped_payload(gen, text, intent)
            if payload is None:
                _log(f"换话术无可用候选: {err[:60]}")
                self._push("applyError:", f"候选生成失败: {err[:60]}")
                return
            _log(f"换话术 端到端 {(time.perf_counter() - t0) * 1000:.0f}ms")
            self._push("applyTones:", payload)
        except Exception as e:
            _log(f"换话术失败 {type(e).__name__}: {str(e)[:60]}")
            self._push("applyError:", f"换话术失败: {type(e).__name__}: {str(e)[:40]}")

    def applyTones_(self, payload):
        self.rows["cand_header"].setStringValue_("候选回复（按合适度排序）")
        total = sum(len(items) for _s, _t, items in payload)
        self._render("status", f"已换话术 · {total} 条", PALETTE["muted"])
        self._render_groups(payload)

    # ------------------------------------------------------------ controls
    def collapsePanel_(self, sender):
        self._set_collapsed(not self._collapsed)

    def togglePause_(self, sender):
        self._paused = not self._paused
        self.pause_item.setTitle_("继续读屏" if self._paused else "暂停读屏")
        if self._paused:
            self._prejudge_req = None        # a paused app judges nothing further
            self._prejudge_result = None
            self._render("status", "已暂停 · 不再读屏", PALETTE["amber"])
            self._render("message", "", PALETTE["text"])
            self._render("sender", "", PALETTE["muted"])
            self._render("intent", "—", PALETTE["muted"])
            self._render("confidence", "", PALETTE["muted"])
            self._render("risk", "", PALETTE["muted"])
            self._render("actions", "", PALETTE["text"])
            self.rows["cand_header"].setStringValue_("")
            self._clear_candidates()
        else:
            self._prejudge_result = None
            self.last_seen = None      # force a fresh read of whatever is on screen
            self.analyzed_text = None
            self._render("status", "已恢复 · 读屏中", PALETTE["muted"])

    def reanalyze_(self, sender):
        self._prejudge_req = None      # "re-analyze" means re-run, not reuse the pre-judge
        self._prejudge_result = None
        self.last_seen = None
        self.analyzed_text = None
        self._render("status", "重新分析中…", PALETTE["muted"])

    def quitApp_(self, sender):
        AppKit.NSApplication.sharedApplication().terminate_(None)

    @objc.python_method
    def _set_collapsed(self, collapsed: bool):
        """Roll the panel up to a title+status strip, or back to full height."""
        self._collapsed = collapsed
        controlled = ["message", "sender", "intent", "confidence", "risk", "actions",
                      "cand_header"]   # "chat" and "status" survive collapsing
        for key in controlled:
            self.rows[key].setHidden_(collapsed)
        for slot in range(styles.MAX_SLOTS):
            self._dds[slot].setHidden_(collapsed)
            self._dd_boxes[slot].setHidden_(collapsed)
            for row in range(styles.PER_TONE):
                has = self.cand_texts[slot * styles.PER_TONE + row] is not None
                for c in self._row_controls(slot, row):
                    c.setHidden_(collapsed or not has)
        if not collapsed:
            # re-expanding puts every control back where _relayout() wants it, and re-hides
            # the slots that are switched off — the collapse above cannot know that
            self._relayout()
            self._last_origin = None      # let the next tick re-dock cleanly
            return

        rect = self.panel.frame()
        # _expanded_h is maintained by _relayout() (it changes with the tone selection), so
        # expanding reads the current full height rather than a value captured at startup
        new_h = COLLAPSED_H if collapsed else (self._expanded_h or PANEL_H)
        self.panel.setFrame_display_(
            NSMakeRect(rect.origin.x, rect.origin.y + (rect.size.height - new_h),
                       rect.size.width, new_h), True)
        self._last_origin = None      # let the next tick re-dock cleanly

    # --------------------------------------------------------------- loop
    def tick_(self, timer):
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
        if not screen_capture_ok():
            if not self._asked_permission:
                self._asked_permission = True
                request_screen_capture()      # opens the system prompt
            self._push("applyError:", "需要屏幕录制权限 · 系统设置 › 隐私与安全性")
            self._next_read_ts = time.time() + SLOW_TICK
            return
        try:
            res = read_conversation(previous_wid=self._win_wid,
                                    prev_fingerprint=self._fingerprint)
        except Exception as e:
            self._push("applyError:", f"读取失败: {type(e).__name__}: {str(e)[:40]}")
            self._next_read_ts = time.time() + SLOW_TICK
            return
        if not res["ok"]:
            self._push("applyError:", f"{res['error']} · 微信没开或窗口被最小化？")
            self._next_read_ts = time.time() + SLOW_TICK
            return

        # Same fingerprint ⇒ same pixels ⇒ the messages are exactly what we last read.
        # Cadence follows the screen: quiet checks back in FAST_TICK (capture+hash only,
        # ~30 ms), moving screens just paid a full OCR and get SLOW_TICK like before.
        self._fingerprint = res.get("fingerprint")
        self._next_read_ts = time.time() + (FAST_TICK if res["unchanged"] else SLOW_TICK)

        # position immediately: analysis takes seconds, and a delayed correction
        # showed up as a visible jump after the verdict landed. Pushed on unchanged
        # frames too — the window can move while its pixels stay identical.
        self._win_wid = res["window"]["wid"]
        self._push("applyPosition:", res["window"])
        if res["unchanged"] and self._last_full is not None:
            # the settle/analyze gate below still runs every read; an unchanged frame
            # just skips re-deriving the messages it would act on
            res = self._last_full
        else:
            self._last_full = res
            self._push("applyChat:", res.get("chat_title") or "")

        msgs = res["messages"]
        if not msgs:
            self._push("applyError:", "聊天区没读到文字")
            return

        thems = [m for m in msgs if m.side == "them"]
        newest = thems[-1] if thems else msgs[-1]
        prev_text = thems[-2].text if len(thems) > 1 else ""
        now = time.time()

        # --- anti-flood: track arrivals, never analyze mid-burst
        if newest.text != self.last_seen:
            self.last_seen = newest.text
            self.last_change_ts = now
            # only on arrival: this function runs every second, and a per-tick line would
            # bury the timing that matters
            t = res.get("timing_ms") or {}
            first_read = not self._read_once
            self._read_once = True
            # Vision loads on the first call and costs ~2x steady state; saying so keeps a
            # one-off from being read as a regression (same reason the judge line does it)
            note = "（首次，含 Vision 加载）" if first_read and t.get("ocr", 0) > 400 else ""
            # say when the fast in-process capture was refused: otherwise a permanent
            # fallback looks like ordinary slowness instead of something to report
            slow_cap = " · 抓屏走了子进程（进程内被抓图接口拒绝）" \
                if t.get("capture_path") == "subprocess" else ""
            _log(f"读屏 抓取 {t.get('capture', 0):.0f}ms + OCR {t.get('ocr', 0):.0f}ms"
                 f" = {t.get('total', 0):.0f}ms · 读到 {len(msgs)} 条（对方 {len(thems)} 条）"
                 f"{note}{slow_cap}")
            _log(f"新消息 · 预判先跑，停稳 {SETTLE_S}s 后上屏（两次完整分析最小间隔 {MIN_GAP_S}s）")
            # latest-wins: overwrite the slot, retire the old verdict — only the newest
            # text's judgment can ever be consumed, and only by the settle gate below
            self._prejudge_req = (newest.text, self._context_text(msgs, newest),
                                  newest.sender, prev_text)
            self._prejudge_result = None
            self._prejudge_event.set()
            # keep the previous verdict readable; just badge that something new landed
            self._push("applyIncoming:", (newest.text, newest.sender, prev_text))

        settled = (now - self.last_change_ts) >= SETTLE_S
        cooled = (now - self.last_analyze_ts) >= MIN_GAP_S
        pr = self._prejudge_result
        pre_hit = pr is not None and pr[0] == newest.text
        # A pre-judged verdict needs no cooling: its cost was already paid per arrival.
        # Only the full path (no usable pre-judgment) still waits MIN_GAP_S out.
        if (newest.text != self.analyzed_text and settled and not self._analyzing
                and not self._prejudging and (pre_hit or cooled)):
            self.last_analyze_ts = now
            self.analyzed_text = newest.text
            self._prejudge_result = None      # spent: a verdict is shown exactly once
            self._analyzing = True
            if pre_hit:
                # Judgment already ran inside the settle window; go straight to the
                # verdict on screen and start only the generation half.
                _log(f"停稳 · 用预判结论上屏 · 这条消息出现到现在 {now - self.last_change_ts:.1f}s")
                self._push("applyJudgment:", (pr[1], pr[2], pr[3]))
                threading.Thread(target=self._run_generation,
                                 args=(newest, msgs, pr[1]), daemon=True).start()
            else:
                _log(f"开始分析 · 这条消息出现到现在 {now - self.last_change_ts:.1f}s")
                self._push("applyPending:", (newest.text, newest.sender, prev_text))
                # off the tick path on purpose: judge+generate+rank takes over a second, and
                # while it runs the loop must keep reading — a message landing mid-analysis
                # used to wait the whole analysis out before anyone even saw it
                threading.Thread(target=self._run_analysis,
                                 args=(newest, msgs, prev_text), daemon=True).start()
        elif newest.text != self.analyzed_text:
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
    def _run_analysis(self, newest, msgs, prev_text: str):
        try:
            self._analyze(newest, msgs, prev_text)
        except Exception as e:
            _log(f"分析失败 {type(e).__name__}: {str(e)[:60]}")
            self._push("applyError:", f"分析失败: {type(e).__name__}: {str(e)[:40]}")
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
                text, context, sender, prev = req
                if self._paused or text != self.last_seen:
                    continue          # superseded while queued: only the newest text counts
                self._prejudging = True
                try:
                    t0 = time.perf_counter()
                    with self._model_lock:
                        verdict = self.judge.judge(text, context=context)
                    ms = (time.perf_counter() - t0) * 1000
                    first = not self._judged_once
                    self._judged_once = True
                    note = "（首次，含本地模型加载）" if first else ""
                    _log(f"预判 {ms:.0f}ms → {verdict.get('intent', '?')}"
                         f" 把握 {verdict.get('confidence', 0):.0%}"
                         f" 风险 {verdict.get('risk', '?')}{note}（待停稳上屏）")
                except Exception as e:
                    _log(f"预判失败 {type(e).__name__}: {str(e)[:60]}")
                    verdict = None
                finally:
                    self._prejudging = False
                if verdict is not None and not self._paused and text == self.last_seen:
                    self._prejudge_result = (text, verdict, sender, prev)
            except Exception:
                pass                  # a resident worker must not die on one bad request

    @objc.python_method
    def _run_generation(self, newest, msgs, verdict: dict):
        """The pre-judged path's second half: generate + rank only, judgment already shown.

        Same shape as _run_analysis minus the judge pass — the settle gate consumed the
        pre-computed verdict, so the (paid) generation is what actually starts here.
        """
        t0 = time.perf_counter()
        try:
            context = self._context_text(msgs, newest)
            gen = self.generator.generate(newest.text, "", list(self.slot_tones),
                                          context, self._stream_hook(t0))
            self._finish_generate(gen, newest, t0, verdict)
        except Exception as e:
            _log(f"生成失败 {type(e).__name__}: {str(e)[:60]}")
            self._push("applyError:", f"候选生成失败: {type(e).__name__}: {str(e)[:40]}")
        finally:
            self._analyzing = False

    @objc.python_method
    def _context_text(self, msgs, newest) -> str | None:
        """The last few turns, each prefixed with who said it — shared by both halves.

        The names are the point. The judge used to receive a jumble of lines with no
        speaker, which in a group chat throws away the most useful clue available: who is
        talking, and whether the last thing said was mine. One-to-one chats render no name
        above the bubble, so 我/对方 stands in.

        The message under judgment is excluded **by identity**, not by position: `newest` is
        the last message from the other side, which is not the same as the last element of
        `msgs` (my own replies come after it).
        """
        prior = [m for m in msgs if m is not newest][-CONTEXT_TURNS:]
        if not prior:
            return None
        return "\n".join(
            f"{m.sender or ('我' if m.side == 'me' else '对方')}: {m.text}" for m in prior)

    @objc.python_method
    def _analyze(self, newest, msgs, prev_text: str = ""):
        """Judge and generate in parallel, then rank. Judgment lands on screen first.

        Runs on its own thread (started by _work_inner): it takes over a second and must
        not hold the read loop hostage.
        """
        import concurrent.futures as cf

        t0 = time.perf_counter()
        context = self._context_text(msgs, newest)
        with cf.ThreadPoolExecutor(max_workers=2) as ex:
            # generation does not need the intent, so it runs while judging; it does need the
            # chosen 话术, which is read here (a plain list read) and passed in
            gen_future = ex.submit(self.generator.generate, newest.text, "",
                                   list(self.slot_tones), context, self._stream_hook(t0))
            verdict = None
            t_judge = time.perf_counter()
            try:
                with self._model_lock:   # never two local forwards at once
                    verdict = self.judge.judge(newest.text, context=context)
                ms = (time.perf_counter() - t_judge) * 1000
                first = not self._judged_once
                self._judged_once = True
                # the model load happens on the first call and is seconds, not milliseconds —
                # without saying so the first verdict looks like a performance regression
                note = "（首次，含本地模型加载）" if first else ""
                _log(f"判断 {ms:.0f}ms → {verdict.get('intent', '?')}"
                     f" 把握 {verdict.get('confidence', 0):.0%}"
                     f" 风险 {verdict.get('risk', '?')}{note}")
                self._push("applyJudgment:", (verdict, newest.sender, prev_text))
            except Exception as e:
                _log(f"判断失败 {type(e).__name__}: {str(e)[:60]}")
                self._push("applyError:", f"判断失败: {type(e).__name__}: {str(e)[:40]}")

            try:
                gen = gen_future.result()
            except Exception as e:
                _log(f"生成失败 {type(e).__name__}: {str(e)[:60]}")
                self._push("applyError:", f"候选生成失败: {type(e).__name__}: {str(e)[:40]}")
                return
            self._finish_generate(gen, newest, t0, verdict)

    @objc.python_method
    def _finish_generate(self, gen: dict, newest, t0: float, verdict: dict | None):
        """Log the generation, rank it against the intent, push the candidates.

        Shared by both analysis paths — the full one (judge ran here) and the pre-judged
        one (the verdict was computed during the settle window) — so the second half of
        the pipeline has exactly one implementation.
        """
        groups = gen.get("groups") or []
        failed = [f"{g['tone']}({g['error'][:40]})" for g in groups if g.get("error")]
        _log(f"生成 {gen.get('elapsed_s', 0) * 1000:.0f}ms · {len(groups)} 个话术并发"
             f" → {sum(len(g['texts']) for g in groups)} 条候选"
             + (f" · 失败: {'; '.join(failed)}" if failed else ""))
        intent = verdict["intent"] if verdict else ""
        t_rank = time.perf_counter()
        payload, err = self._grouped_payload(gen, newest.text, intent)
        rank_ms = (time.perf_counter() - t_rank) * 1000
        if payload is None:
            _log(f"生成无可用候选: {err[:60]}")
            self._push("applyError:", f"候选生成失败: {err[:60]}")
            return
        _log(f"排序 {rank_ms:.0f}ms（本地模型，一次前向）")
        _log(f"端到端 {(time.perf_counter() - t0) * 1000:.0f}ms"
             f" · 从分析开始到候选上屏")
        self._push("applyCandidates:", payload)

    @objc.python_method
    def _push(self, selector: str, payload=None):
        self.performSelectorOnMainThread_withObject_waitUntilDone_(selector, payload, False)

    # --- main-thread callbacks (AppKit is not thread safe)
    def applyChat_(self, title):
        self._chat_title = title
        self._render("chat", title, PALETTE["green"])

    def applyIncoming_(self, payload):
        # a new message landed but we are not analysing yet (burst in progress):
        # keep the previous verdict visible, just badge it
        text, sender, prev = payload
        self._show()
        self._render("status", "有新消息 · 等消息停稳…", PALETTE["muted"])
        self._render("message", text, PALETTE["muted"])   # grey: not analysed yet
        self._render("sender", self._context_line(sender, prev), PALETTE["muted"])

    def applyPending_(self, payload):
        text, sender, prev = payload
        self._show()
        self._render("status", "分析中…", PALETTE["muted"])
        self._render("message", text, PALETTE["text"])    # inked: this is the one
        self._render("sender", self._context_line(sender, prev), PALETTE["muted"])
        self._clear_candidates()
        self._stream_rows = {}     # a new run starts at line zero in every slot
        self.rows["cand_header"].setStringValue_("候选回复 · 等待判断…")

    def applyJudgment_(self, payload):
        v, sender, prev = payload
        self._show()
        # kept so a 话术 change can re-rank the new candidates against the same verdict
        self._last_intent = v.get("intent", "")
        self._render("message", v["message"], PALETTE["text"])
        self._render("sender", self._context_line(sender, prev), PALETTE["muted"])
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
        self._render("actions", " · ".join(v.get("actions", [])), PALETTE["text"])
        self.rows["cand_header"].setStringValue_("候选回复 · 生成中…")

    def applyCandidates_(self, payload):
        self.rows["cand_header"].setStringValue_("候选回复（按合适度排序）")
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
        if row >= styles.PER_TONE:
            return                       # the prompt asks for PER_TONE lines; extras stray
        self._stream_rows[slot] = row + 1
        self.cand_texts[slot * styles.PER_TONE + row] = text
        if self._collapsed:
            return      # collapse keeps the data; _set_collapsed(False) puts it back up
        r = self._rows[slot][row]
        r["prob"].setStringValue_(f"#{row + 1}")
        r["text"].setStringValue_(text)
        for c in self._row_controls(slot, row):
            c.setHidden_(False)

    def applyError_(self, text):
        self._show()                       # never vanish without telling the user why
        self._render("status", text, PALETTE["red"])

    def applyHidden_(self, reason):
        # WeChat gone or unreadable -> take the panel away (the app "opens with WeChat")
        self._render("status", reason, PALETTE["muted"])
        if self.panel.isVisible():
            self.panel.orderOut_(None)

    def applyPosition_(self, win):
        self._position_near(win)

    # --------------------------------------------------------------- warm-up
    @objc.python_method
    def _warm(self):
        """Pay the one-off loads in the background: Vision OCR first, then the judge model.

        The first real message used to carry both costs: Vision's ~0.7 s first OCR and
        decider-2b's 9-15 s load inside its first judge(). Starting both here, right after
        launch, moves them to idle time — the fast one first so it is ready within a
        second, the slow one after. If a message does land mid-warm-up nothing breaks:
        its judge() blocks on the model's load lock until the warm-up finishes, and the
        OCR warm-up is independent of WeChat entirely (a blank canvas, not a window).
        """
        t0 = time.perf_counter()
        ocr_ms = warm_ocr()
        if ocr_ms >= 0:
            self._read_once = True    # Vision's one-off load is paid; first read is steady-state
            _log(f"预热 OCR 就绪 · {ocr_ms:.0f}ms")
        else:
            _log("预热 OCR 失败 · 首次读屏会稍慢，不影响使用")

        try:
            self.judge.warm()
        except Exception as e:
            _log(f"预热判断模型失败 {type(e).__name__}: {str(e)[:60]}")
        else:
            self._judged_once = True  # same: the load is paid, the first judge is steady-state
            _log(f"预热 判断模型就绪 · 总耗时 {(time.perf_counter() - t0) * 1000:.0f}ms")


def warn_if_no_generation_key() -> None:
    """Say it out loud at launch when the candidate half has no key behind it.

    The judgment half runs locally and needs nothing, so a panel with an empty candidate
    area reads as "the app is broken" rather than "I never configured this". One dialog at
    launch is the cheapest way to tell the two apart — it cannot be missed the way a line
    of grey text in a floating panel can.

    OPENAI_* and ANTHROPIC_* are two ways to configure the same generation layer, so this
    fires only when NEITHER is set: either one on its own is a complete configuration.
    TypeSafe is not checked — it has a local fallback, so it is never missing, only
    different.

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
    warn_if_no_generation_key()
    controller = HudController.alloc().init()
    # First line of every run: which backends are actually in play. Support requests
    # always need it, and it proves the log is live before the first message arrives.
    _base, _key, _model, _src, _api = load_credentials()
    _log(f"启动 · 判断层 "
         f"{'TypeSafe Jev' if userconfig.get('TYPESAFE_API_KEY') else '本地 decider-2b'}"
         f" · 生成层 {(_base + ' / ' + _model) if _key else '未配置（候选区会是空的）'}")
    controller._show()
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
