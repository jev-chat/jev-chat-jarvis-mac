#!/usr/bin/env python3
"""AppKit smoke test for live HUD resizing and collapse/restore."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import AppKit
import Quartz
from AppKit import NSMakeRect
from Foundation import NSDate

from hud import COLLAPSED_H, HudController, PANEL_MIN_H, PANEL_MIN_W
import styles


def close_enough(actual: float, expected: float, tolerance: float = 1.0) -> None:
    if abs(actual - expected) > tolerance:
        raise AssertionError(f"expected {expected}, got {actual}")


def resize(controller: HudController, width: float, height: float) -> None:
    frame = controller.panel.frame()
    controller._layout_resizing = True
    try:
        controller.panel.setFrame_display_(
            NSMakeRect(frame.origin.x, frame.origin.y, width, height), False)
    finally:
        controller._layout_resizing = False
    controller._user_sized = True
    controller._relayout()

    frame = controller.panel.frame()
    close_enough(frame.size.width, width)
    close_enough(frame.size.height, height)
    content_w = controller._content_view.frame().size.width
    close_enough(content_w, width)

    geometry_right = max((
        row["fill_btn"].frame().origin.x + row["fill_btn"].frame().size.width
        for slot in controller._rows
        for row_index, row in enumerate(slot)
        if row_index < controller.candidate_count and not row["fill_btn"].isHidden()
    ), default=0)
    if geometry_right > width + 0.5:
        raise AssertionError(f"candidate actions overflow at width {width}")
    for surface in controller._group_boxes + controller._dd_boxes:
        right = surface.frame().origin.x + surface.frame().size.width
        if right > width + 0.5:
            raise AssertionError(f"surface overflows at width {width}")
    for button in (controller.reanalyze_button, controller.regenerate_button):
        frame = button.frame()
        if button.isHidden():
            raise AssertionError(f"manual action is hidden at {width}x{height}")
        if frame.origin.x < 0 or frame.origin.x + frame.size.width > width + 0.5:
            raise AssertionError(f"manual action overflows at width {width}")

    viewport_h = controller._content_scroll.contentSize().height
    document_h = controller._content_view.frame().size.height
    if document_h + 0.5 < viewport_h:
        raise AssertionError("scroll document is shorter than its viewport")


def main() -> None:
    AppKit.NSApplication.sharedApplication()
    controller = HudController.alloc().init()
    controller.candidate_count = 4  # exercise the maximum configurable row count
    long_reply = "这是一段用来验证宽度变化后自动换行和高度增长的候选回复" * 3
    controller.rows["message"].setStringValue_(
        "\n".join(f"对方｜这是第 {line} 条用来检查窗口变高后自动展开的读屏结果"
                  for line in range(1, 11)))
    for slot_index, slot in enumerate(controller._rows):
        for row_index, row in enumerate(slot):
            row["text"].setStringValue_(long_reply)
            controller.cand_texts[slot_index * styles.PER_TONE + row_index] = long_reply

    resize(controller, PANEL_MIN_W, PANEL_MIN_H)
    controller.candidate_count = 2
    resize(controller, PANEL_MIN_W, PANEL_MIN_H)
    compact_message_h = controller._message_scroll.frame().size.height
    resize(controller, 480, 420)
    for slot_index, slot in enumerate(controller._rows):
        for row_index, row in enumerate(slot):
            row["text"].setStringValue_("短回复")
            controller.cand_texts[slot_index * styles.PER_TONE + row_index] = "短回复"
    resize(controller, 720, 820)
    expanded_message_h = controller._message_scroll.frame().size.height
    if expanded_message_h <= compact_message_h:
        raise AssertionError("taller window did not reveal more read-result text")

    resize(controller, 640, 760)
    controller.rows["message"].setStringValue_(
        "对方（fish）｜明天下午三点可以吗\n  ↳ 引用（白正秋）｜网络环境有关系")
    controller._render_groups([(
        0, controller.slot_tones[0], [
            {"text": "可以,我明天下午三点到", "prob": .86},
            {"text": "我先确认一下时间,晚点回复你", "prob": .62},
        ])])
    for width, height, path in ((320, 620, "/tmp/jev-hud-layout-narrow.png"),
                                (640, 760, "/tmp/jev-hud-layout-smoke.png")):
        resize(controller, width, height)
        controller.panel.center()
        controller.panel.orderFrontRegardless()
        AppKit.NSRunLoop.currentRunLoop().runUntilDate_(
            NSDate.dateWithTimeIntervalSinceNow_(0.2))
        controller.panel.displayIfNeeded()
        image = Quartz.CGWindowListCreateImage(
            Quartz.CGRectNull, Quartz.kCGWindowListOptionIncludingWindow,
            controller.panel.windowNumber(), Quartz.kCGWindowImageBoundsIgnoreFraming)
        if image:
            bitmap = AppKit.NSBitmapImageRep.alloc().initWithCGImage_(image)
            bitmap.representationUsingType_properties_(
                AppKit.NSBitmapImageFileTypePNG, {}).writeToFile_atomically_(path, True)
    controller._set_collapsed(True)
    collapsed = controller.panel.frame()
    close_enough(collapsed.size.width, 640)
    close_enough(collapsed.size.height, COLLAPSED_H)
    controller._set_collapsed(False)
    restored = controller.panel.frame()
    close_enough(restored.size.width, 640)
    close_enough(restored.size.height, 760)
    print("responsive layout smoke: OK (320×260, 480×420, 720×820, collapse/restore)")


if __name__ == "__main__":
    main()
