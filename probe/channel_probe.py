#!/usr/bin/env python3
"""通用渠道探测：按 bundle id 判断一个聊天应用能否走 AX，还是必须走截图 OCR。

用法（目标应用**主窗口已打开、并处于前台**时跑一次）：

    uv run python probe/channel_probe.py net.whatsapp.WhatsApp
    uv run python probe/channel_probe.py com.tencent.xinWeChat --json
    uv run python probe/channel_probe.py ru.keepcoder.Telegram --dump --no-capture

这个脚本只回答三个问题，不碰聊天内容：

  1. 这个应用在本机暴露了多少 AX 窗口？有没有正文级文本节点和输入控件？
  2. 它的 CGWindow 长什么样（窗口 id、层级、尺寸、有没有标题）？
  3. 能不能截到它的窗口（截图 OCR 路线的前置条件）？

**隐私**：脚本不打印任何消息正文，文本属性只统计「节点数 / 最大字符数」。
`--dump` 也只输出角色直方图与长度分布。全程只读：不写入、不点击、不合成按键、不截图落盘
（`--capture` 时临时截图在退出时删除）。

注意：若输出 `AX windows before=0`，先确认目标应用的主窗口真的打开了（最小化/只留浮层时
AX 窗口会是 0），把应用切到前台再跑一次——这个坑在本机实测过：窗口关着时 0，打开后 1。

判定标准（与 docs/pm-channel-expansion 一致）：
  · AX windows > 0，且树里有 AXStaticText/AXTextArea 级节点 → 可写 AX 适配器；
  · 否则 → 转截图 + Vision OCR，并参考 `probe/capture_probe.py` 校准布局。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

import AppKit
import ApplicationServices as AS
import Quartz

TEXT_ROLES = {"AXStaticText", "AXTextField", "AXTextArea", "AXHeading"}
INPUT_ROLES = {"AXTextArea", "AXTextField"}
MAX_NODES = 6000
MAX_DEPTH = 40
BIDI = "\u200e\u200f\u202a\u202b\u202c"


def _attr(el, name):
    """Read one AX attribute; any error becomes None (the probe never raises)."""
    err, value = AS.AXUIElementCopyAttributeValue(el, name, None)
    return value if err == 0 else None


def _clean(name: str) -> str:
    """Strip bidi marks around owner/title names (WhatsApp prepends U+200E)."""
    return (name or "").strip().strip(BIDI + " ")


def _running(bundle: str):
    """The running NSRunningApplication for `bundle`, or None when not running."""
    apps = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle)
    return apps[0] if apps else None


def _walk(el, depth: int, acc: dict, budget: list[int]) -> None:
    """只收集结构信息：角色计数、文本节点数、最长文本长度。"""
    if depth > MAX_DEPTH or budget[0] <= 0:
        return
    budget[0] -= 1
    role = _attr(el, AS.kAXRoleAttribute) or ""
    acc["roles"][role] += 1
    if role in TEXT_ROLES:
        for name in (AS.kAXValueAttribute, AS.kAXTitleAttribute, AS.kAXDescriptionAttribute):
            value = _attr(el, name)
            if isinstance(value, str) and value.strip():
                acc["n_text"] += 1
                acc["max_text_len"] = max(acc["max_text_len"], len(value.strip()))
                break
    if role in INPUT_ROLES:
        acc["n_inputs"] += 1
        # 文本区 != 可写输入控件：文档区、只读预览也可能是 AXTextArea，
        # 只有 AXValue 可 set 才算「能填入」（与 src/fill.py 的判据一致）
        try:
            result = AS.AXUIElementIsAttributeSettable(el, AS.kAXValueAttribute, None)
            settable = result[1] if isinstance(result, tuple) else bool(result)
        except Exception:                              # noqa: BLE001
            settable = False
        if settable:
            acc["n_writable_inputs"] += 1
    for child in _attr(el, AS.kAXChildrenAttribute) or []:
        _walk(child, depth + 1, acc, budget)


def probe_ax(bundle: str, dump: bool) -> dict:
    """Read the target application's already-exposed AX tree (read-only, no writes)."""
    app = _running(bundle)
    if app is None:
        return {"ok": False, "reason": "应用未运行"}

    pid = app.processIdentifier()
    element = AS.AXUIElementCreateApplication(pid)
    # 纯只读：不写 AXManualAccessibility，也不改目标应用的任何状态。
    # 因此这里看到的是应用「已经暴露」的 AX 树；Electron 应用可能需要用户在别处
    # 主动开启无障碍树，窗口数为 0 只是「当前没暴露」，不能当作不支持 AX 的结论。
    err, windows = AS.AXUIElementCopyAttributeValue(
        element, AS.kAXWindowsAttribute, None)
    windows = windows or []

    report = {
        "pid": pid,
        "ax_error": err,
        "ax_windows": len(windows),
        "windows": [],
    }
    if err != 0:
        # 接口调用失败（无权限 / 目标无响应 / 应用正在退出）与「成功读到 0 个窗口」
        # 是两件事：前者不能用来判断 AX 是否可用。
        report["ok"] = False
        report["reason"] = (f"AX 接口读取失败（err={err}）：检查辅助功能权限与目标应用状态；"
                            "这不是「不支持 AX」的证据")
        return report
    if not windows:
        report["ok"] = False
        report["reason"] = ("AX 接口正常但当前没有暴露窗口：先确认主窗口已打开并置于前台；"
                            "仍为 0 只说明当前未暴露 AX 树，不等于不支持 AX")
        return report

    for index, win in enumerate(windows[:3]):
        acc = {"roles": Counter(), "n_text": 0, "max_text_len": 0,
               "n_inputs": 0, "n_writable_inputs": 0}
        _walk(win, 0, acc, [MAX_NODES])
        entry = {
            "index": index,
            "title_chars": len(_attr(win, AS.kAXTitleAttribute) or ""),
            "role": _attr(win, AS.kAXRoleAttribute) or "",
            "children": len(_attr(win, AS.kAXChildrenAttribute) or []),
            "n_text_nodes": acc["n_text"],
            "max_text_len": acc["max_text_len"],
            "n_input_controls": acc["n_inputs"],
            "n_writable_inputs": acc["n_writable_inputs"],
        }
        if dump:
            entry["roles"] = dict(acc["roles"].most_common(12))
        report["windows"].append(entry)

    first = report["windows"][0]
    # 判据分三档：窗口都没有 / 只有壳（少量文本、极短）/ 真能读到正文。
    # 只看「有没有文本节点」会把只暴露标题栏的应用误判成 AX 可用。
    shallow = first["n_text_nodes"] < 8 and first["max_text_len"] < 20
    report["ok"] = not shallow
    if not report["ok"]:
        report["reason"] = ("AX 树过浅（文本节点 %d、最长 %d 字符）：疑似只暴露窗口壳，"
                            "建议转截图 OCR 并人工确认" % (first["n_text_nodes"],
                                                          first["max_text_len"]))
    elif first["n_input_controls"] == 0:
        report["reason"] = "有正文节点但没找到 AX 输入控件（填入需走视觉路径）"
    else:
        report["reason"] = "AX 树可读且含输入控件，适合 AX 适配器"
    return report


def probe_windows(bundle: str) -> list[dict]:
    """按 owner 名枚举 CGWindow；bundle 用来确认进程归属，名字去掉 bidi 控制符。"""
    app = _running(bundle)
    pid = app.processIdentifier() if app else None
    owner_hint = _clean(app.localizedName()) if app else ""
    options = Quartz.kCGWindowListOptionAll | Quartz.kCGWindowListExcludeDesktopElements
    rows = []
    for win in Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID):
        if pid is not None and win.get("kCGWindowOwnerPID") != pid:
            continue
        if pid is None and _clean(win.get("kCGWindowOwnerName")) != owner_hint:
            continue
        bounds = dict(win.get("kCGWindowBounds") or {})
        rows.append({
            "wid": win.get("kCGWindowNumber"),
            "layer": win.get("kCGWindowLayer"),
            "owner": _clean(win.get("kCGWindowOwnerName")),
            # 只保留「有没有标题」，不存标题原文：聊天窗口标题常是联系人名
            "titled": bool(_clean(win.get("kCGWindowName"))),
            "w": round(float(bounds.get("Width", 0)), 1),
            "h": round(float(bounds.get("Height", 0)), 1),
        })
    rows.sort(key=lambda r: r["w"] * r["h"], reverse=True)
    return rows


def probe_capture(windows: list[dict]) -> dict:
    """挑一个带标题的主窗口试截图，只报像素尺寸，不落盘、不 OCR。"""
    if not windows:
        return {"ok": False, "reason": "没有可截图的窗口"}
    # CGWindowList 是前到后排序：第一个带标题的窗口就是该应用最前面的主窗口。
    # 按面积最大的话，独立聊天窗/浮层可能被误当成主窗口。
    candidate = next((w for w in windows if w["titled"]), windows[0])
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "window.png"
        try:
            proc = subprocess.run(
                ["screencapture", "-x", "-o", "-l", str(candidate["wid"]), str(out)],
                capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "reason": f"{type(exc).__name__}"}
        if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            return {"ok": False, "reason": "screencapture 失败（多半缺屏幕录制权限）"}
        source = Quartz.CGImageSourceCreateWithURL(
            AppKit.NSURL.fileURLWithPath_(str(out)), None)
        image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None) if source else None
        if image is None:
            return {"ok": False, "reason": "截图为空"}
        return {"ok": True, "wid": candidate["wid"],
                "pixels": [Quartz.CGImageGetWidth(image), Quartz.CGImageGetHeight(image)]}


def main(argv: list[str]) -> int:
    """CLI entry: parse args, run the read-only probes, print report and verdict."""
    if len(argv) < 2 or argv[1].startswith("-"):
        print(__doc__.strip().splitlines()[2].strip())
        return 2
    bundle = argv[1]
    dump = "--dump" in argv
    with_capture = "--capture" in argv
    if "--json" in argv:
        as_json = True
    else:
        as_json = False
    no_capture = "--no-capture" in argv

    report: dict = {
        "bundle": bundle,
        "accessibility_trusted": bool(AS.AXIsProcessTrusted()),
        "screen_capture_allowed": bool(Quartz.CGPreflightScreenCaptureAccess()),
    }

    app = _running(bundle)
    if app is None:
        report["running"] = False
        report["verdict"] = "应用未运行：启动目标应用后重跑"
        _emit(report, as_json)
        return 1
    report["running"] = True
    report["app_name"] = _clean(app.localizedName())
    report["pid"] = app.processIdentifier()

    report["ax"] = (probe_ax(bundle, dump)
                    if report["accessibility_trusted"]
                    else {"ok": False, "reason": "没有辅助功能权限"})
    report["cgwindows"] = probe_windows(bundle)
    if with_capture and report["screen_capture_allowed"] and not no_capture:
        report["capture"] = probe_capture(report["cgwindows"])

    titled = [w for w in report["cgwindows"] if w["titled"]]
    ax = report["ax"]
    if not report["accessibility_trusted"]:
        verdict = "AX 未授权：先在系统设置里给本进程开辅助功能权限，再重跑"
    elif ax.get("ax_error") not in (None, 0):
        verdict = (f"AX 接口读取失败（err={ax.get('ax_error')}）：检查辅助功能权限/目标应用；"
                   "这不代表 AX 路不通") + (f"（原因：{ax.get('reason')}）" if ax.get("reason") else "")
    elif ax.get("ax_windows") == 0:
        verdict = ("AX 窗口数 0：先确认目标应用主窗口已打开并置于前台；"
                   "仍为 0 只说明当前未暴露 AX 树，不等于 AX 路不通")
    elif not ax.get("ok"):
        verdict = f"AX 不可读 → 转截图 OCR；主窗口：" + (
            f"{titled[0]['wid']} {titled[0]['w']}x{titled[0]['h']}" if titled
            else "（未找到带标题的窗口）") + f"；AX 原因：{ax.get('reason')}"
    elif (ax.get("windows") or [{}])[0].get("n_writable_inputs", 0) == 0:
        total_inputs = (ax.get("windows") or [{}])[0].get("n_input_controls", 0)
        verdict = (f"AX 可读但无可写输入控件（文本区 {total_inputs} 个，AXValue 均不可 set）："
                   "读屏可走 AX，填入需视觉/剪贴板兜底")
        if ax.get("reason"):
            verdict += f"（AX 原因：{ax['reason']}）"
    else:
        verdict = "AX 可读 → 可写 AX 适配器（填入优先 AX 写入 + 读回）"
    report["verdict"] = verdict
    _emit(report, as_json)
    return 0


def _emit(report: dict, as_json: bool) -> None:
    """Print the report as JSON (`--json`) or as fixed-width human-readable lines."""
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return
    print(f"bundle            {report['bundle']}")
    if not report.get("running"):
        print(f"verdict           {report['verdict']}")
        return
    print(f"app               {report['app_name']} (pid {report['pid']})")
    print(f"AX trusted        {report['accessibility_trusted']}")
    print(f"screen capture    {report['screen_capture_allowed']}")
    ax = report["ax"]
    err = ax.get("ax_error")
    print(f"AX windows        {ax.get('ax_windows')}（只读，未写入目标应用）"
          + (f" · err={err}" if err not in (None, 0) else ""))
    for w in ax.get("windows", []):
        print(f"  win#{w['index']} role={w['role']} children={w['children']} "
              f"text_nodes={w['n_text_nodes']} max_text_len={w['max_text_len']} "
              f"inputs={w['n_input_controls']} writable={w['n_writable_inputs']} "
              f"title_chars={w['title_chars']}")
        if "roles" in w:
            print(f"    roles {w['roles']}")
    print(f"CG windows        {len(report['cgwindows'])}")
    for w in report["cgwindows"][:8]:
        print(f"  wid={w['wid']} layer={w['layer']} {w['w']}x{w['h']} "
              f"titled={'yes' if w['titled'] else 'no'}")
    if "capture" in report:
        c = report["capture"]
        print(f"capture           {'ok ' + str(c.get('pixels')) if c.get('ok') else c.get('reason')}")
    if ax.get("reason"):
        print(f"AX reason         {ax['reason']}")
    print(f"verdict           {report['verdict']}")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
