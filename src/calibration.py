"""Manual message-area geometry. Values are window points, never screen coordinates."""
from dataclasses import dataclass
import math

# Region minimums are per purpose. The message region has to hold a conversation
# (80x60), but an input region only needs to cover the editor — some composers are
# well under 60pt tall (WhatsApp's is about 40pt, #155), and sharing one threshold
# made those input regions impossible to save.
MESSAGE_MIN_WIDTH = 80.0
MESSAGE_MIN_HEIGHT = 60.0
INPUT_MIN_WIDTH = 40.0
INPUT_MIN_HEIGHT = 24.0


@dataclass(frozen=True)
class Calibration:
    window_width: float
    window_height: float
    x: float
    y: float
    width: float
    height: float

    def __post_init__(self):
        """Reject only structurally invalid geometry (finite, positive, inside window).

        Size minimums depend on what the region is for, so they live in
        `validate_message_region` / `validate_input_region`; here we just refuse boxes
        that can never be a region (zero/negative/NaN/outside the window).
        """
        values = tuple(self.__dict__.values())
        if (not all(math.isfinite(v) for v in values)
                or self.window_width <= 0 or self.window_height <= 0
                or self.x < 0 or self.y <= 0
                or self.width <= 0 or self.height <= 0
                or self.x + self.width > self.window_width
                or self.y + self.height >= self.window_height):
            raise ValueError('区域无效：请确认窗口尺寸并完整框选。')

    @classmethod
    def parse(cls, value):
        """Parse the `window,window_pts,width,height,x,y,w,h` string from the env file."""
        return cls(*(float(v) for v in value.split(',')))

    def serialize(self):
        """Inverse of `parse`: the exact string written back to the env file."""
        return ','.join(f'{v:.3f}' for v in self.__dict__.values())

    def matches(self, win):
        """True when the saved region belongs to this window size (±1pt)."""
        return (abs(win.w - self.window_width) < 1
                and abs(win.h - self.window_height) < 1)

    def rect(self):
        """Window-relative (x, y, w, h) in 0..1 for the OCR/overlay path."""
        return (self.x / self.window_width, self.y / self.window_height,
                self.width / self.window_width, self.height / self.window_height)

    def screen_rect(self, win):
        """Absolute screen rectangle (x, y, w, h) for a live window dict."""
        return (win['x']+self.x, win['y']+self.y, self.width, self.height)


def validate_message_region(region):
    """Reject a message region that is too small to hold a conversation (80x60)."""
    if region.width < MESSAGE_MIN_WIDTH or region.height < MESSAGE_MIN_HEIGHT:
        raise ValueError(
            f'消息识别区域太小，请完整框选消息区（至少 {MESSAGE_MIN_WIDTH:.0f}×'
            f'{MESSAGE_MIN_HEIGHT:.0f} 点）。')


def validate_input_region(messages, editor):
    """Validate both regions for preview/save: size minimums per purpose, then geometry.

    Errors name the region that failed, so a short editor no longer reports
    「消息区」(#155). Message region is checked here too because callers only pass the
    two regions together.
    """
    if (editor.width < INPUT_MIN_WIDTH or editor.height < INPUT_MIN_HEIGHT):
        raise ValueError(
            f'输入区域太小，请完整框选编辑区（至少 {INPUT_MIN_WIDTH:.0f}×'
            f'{INPUT_MIN_HEIGHT:.0f} 点）。')
    validate_message_region(messages)
    if (messages.window_width != editor.window_width
            or messages.window_height != editor.window_height
            or editor.y < messages.y+messages.height
            or editor.x < messages.x-8
            or editor.x+editor.width > messages.x+messages.width+8):
        raise ValueError('输入区应位于消息区下方，并完整框选编辑区，不含工具栏。')
