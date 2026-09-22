"""Shared AppKit presentation primitives for the HUD and its settings window."""
from __future__ import annotations

import AppKit as A
from Foundation import NSMakeRect


def rgb(hex_code: int, alpha: float = 1.0) -> A.NSColor:
    return A.NSColor.colorWithCalibratedRed_green_blue_alpha_(
        ((hex_code >> 16) & 0xFF) / 255.0,
        ((hex_code >> 8) & 0xFF) / 255.0,
        (hex_code & 0xFF) / 255.0,
        alpha,
    )


# Native light vibrancy with cool ink, quiet metadata and semantic accents.
PALETTE = {
    "bg": rgb(0xF5F9F8, 0.74),
    "text": rgb(0x102142),
    "muted": rgb(0x6F7D94),
    "accent": rgb(0x0B8A4A),
    "green": rgb(0x00B95F),
    "amber": rgb(0xF0A000),
    "red": rgb(0xF05252),
    "surface": rgb(0xFFFFFF, 0.36),
    "row": rgb(0xFFFFFF, 0.24),
    "field": rgb(0xFFFFFF, 0.42),
    "edge": rgb(0xFFFFFF, 0.72),
    "track": rgb(0xB8C1C6, 0.42),
}

# Opaque layers need their own contrast; white-on-white vibrancy colors disappear
# when macOS substitutes a solid backdrop for Reduce Transparency.
SOLID_PALETTE = {
    "bg": rgb(0xE7EEEB),
    "surface": rgb(0xFFFFFF),
    "row": rgb(0xF5F8F6),
    "field": rgb(0xEAF1ED),
    "edge": rgb(0xC5D2CB),
    "track": rgb(0xD3DED8),
}

RADIUS_FIELD = 8
RADIUS_CARD = 12


def make_surface(radius: float, color: A.NSColor,
                 border: A.NSColor | None = None) -> A.NSView:
    """Create a layer-backed visual surface without application state or behavior."""
    surface = A.NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 1, 1))
    surface.setWantsLayer_(True)
    surface.layer().setBackgroundColor_(color.CGColor())
    surface.layer().setCornerRadius_(radius)
    if border is not None:
        surface.layer().setBorderColor_(border.CGColor())
        surface.layer().setBorderWidth_(0.75)
    return surface


def make_label(text: str, x: float, y: float, width: float, height: float,
               size: float = 13, color: A.NSColor | None = None,
               bold: bool = False, selectable: bool = False) -> A.NSTextField:
    field = A.NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, width, height))
    field.setStringValue_(text)
    field.setBezeled_(False)
    field.setDrawsBackground_(False)
    field.setEditable_(False)
    field.setSelectable_(selectable)
    field.setTextColor_(PALETTE["text"] if color is None else color)
    font = A.NSFont.boldSystemFontOfSize_(size) if bold else A.NSFont.systemFontOfSize_(size)
    field.setFont_(font)
    return field


def style_button(button: A.NSButton, *, font_size: float = 11,
                 radius: float = 16, primary: bool = False) -> A.NSButton:
    """Apply the same quiet pill treatment used by actions on the HUD."""
    button.setBordered_(False)
    button.setFont_(A.NSFont.boldSystemFontOfSize_(font_size)
                    if primary else A.NSFont.systemFontOfSize_(font_size))
    button.setContentTintColor_(A.NSColor.whiteColor() if primary else PALETTE["text"])
    button.setWantsLayer_(True)
    button.layer().setBackgroundColor_((PALETTE["green"] if primary else PALETTE["row"]).CGColor())
    button.layer().setBorderColor_((PALETTE["green"] if primary else PALETTE["edge"]).CGColor())
    button.layer().setBorderWidth_(0.75)
    button.layer().setCornerRadius_(radius)
    return button
