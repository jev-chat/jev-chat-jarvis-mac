"""Run on macOS with: uv run --frozen python probe/perception_regression.py"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from perception import TextBlock, extract_messages, find_wechat_window


def window(wid, title, width, height):
    return {"kCGWindowOwnerName": "WeChat", "kCGWindowNumber": wid,
            "kCGWindowName": title, "kCGWindowOwnerPID": 1,
            "kCGWindowBounds": {"Width": width, "Height": height}}


def check():
    detached = window(2, "微信 (窗口)", 947, 679)
    for title in ("微信", "WeChat"):
        main = window(1, title, 754, 593)
        with patch("Quartz.CGWindowListCopyWindowInfo", return_value=[detached, main]):
            assert find_wechat_window().wid == 1, "Selected detached window over main chat"
            assert find_wechat_window(2).wid == 1, "Stayed stuck on detached window"
            assert find_wechat_window(1).wid == 1
        other = window(3, title, 900, 700)
        with patch("Quartz.CGWindowListCopyWindowInfo", return_value=[other, main]):
            assert find_wechat_window(1).wid == 1, "Lost stickiness within the same priority"
    with patch("Quartz.CGWindowListCopyWindowInfo", return_value=[detached]):
        assert find_wechat_window().wid == 2, "Lost fallback when main is absent"

    # The visible right-hand '11' used to be discarded as a small sender label.
    messages = extract_messages([TextBlock("11", 1.0, .862, .284, .024, .024)])
    assert len(messages) == 1 and messages[0].text == "11"
    assert messages[0].side == "me" and messages[0].sender is None
    messages = extract_messages([
        TextBlock("11", 1.0, .86, .60, .024, .024),
        TextBlock("下一条消息", 1.0, .78, .45, .10, .035),
    ])
    assert [m.text for m in messages] == ["11", "下一条消息"]
    assert all(m.sender is None for m in messages)

    # These centers straddle the original .66 midline, but share a bubble's left edge.
    messages = extract_messages([
        TextBlock("这是自己说的长消息", 1.0, .55, .70, .29, .035),
        TextBlock("较短的下一行", 1.0, .55, .66, .20, .035),
        TextBlock("11", 1.0, .862, .284, .024, .024),
    ])
    assert len(messages) == 2 and all(m.side == "me" for m in messages)
    assert messages[0].lines == ["这是自己说的长消息", "较短的下一行"]
    assert messages[-1].text == "11"

    # Incoming sender labels still attach to their message or disappear when isolated.
    messages = extract_messages([
        TextBlock("小王", 1.0, .40, .60, .05, .020),
        TextBlock("下午开会", 1.0, .40, .45, .15, .035),
    ])
    assert len(messages) == 1 and messages[0].sender == "小王"
    assert not extract_messages([TextBlock("小王", 1.0, .40, .60, .05, .020)])
    print("PASS: window selection, outgoing short/wrapped messages and incoming sender labels")


if __name__ == "__main__":
    check()
