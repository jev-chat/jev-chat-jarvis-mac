"""Shared offline fixtures for the HUD/perception test files.

Not collected by `unittest discover` (the file name does not start with `test`):
test files import `hud_harness()`/`Harness`/`HUD` and `block()` from here.
hud_harness() AST-loads the actual HudController methods so tests never start
Cocoa, read the screen, load user credentials, or make model calls; block()
builds synthetic OCR text blocks.
"""
import ast
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from perception import TextBlock


def hud_harness():
    tree = ast.parse((ROOT / 'src/hud.py').read_text())
    source = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'HudController')
    names = {'_work_inner', '_set_foreground_state', '_push', '_reply_task', '_reply_current', '_push_reply',
             'applyReplyUpdate_', 'applyWaiting_', '_context_text', '_stream_hook',
             '_take_pregen', '_gen_with_pregen', '_finish_generate', '_enqueue_prework',
             '_prejudge_loop', '_pregen_loop'}
    methods = [n for n in source.body if isinstance(n, ast.FunctionDef) and n.name in names]
    for method in methods:
        method.decorator_list = []
    klass = ast.ClassDef(name='Harness', bases=[], keywords=[], body=methods, decorator_list=[])
    scope = {'fill': SimpleNamespace(locate_input=Mock(return_value={'box': None, 'rect': None, 'reason': 'test'})), 'time': time, 'threading': threading, '_log': lambda *_: None,
             'frontmost_app_is_wechat': Mock(return_value=True),
             'screen_capture_ok': Mock(return_value=True), 'request_screen_capture': Mock(),
             'read_conversation': Mock(),
             'PALETTE': {'muted': None}, 'CONTEXT_TURNS': 8, 'JUDGE_TURNS': 4,
             'SLOW_TICK': 1, 'BURST_TICK': .45, 'FAST_TICK': .25, 'BURST_READS': 3,
             'READ_FAILURE_HIDE_S': 2, 'EMPTY_FRAME_REUSE_S': 2,
             'SETTLE_S': 1.2, 'STABLE_READS': 3, 'EARLY_SETTLE_S': .7, 'MIN_GAP_S': 2}
    module = ast.fix_missing_locations(ast.Module(body=[klass], type_ignores=[]))
    exec(compile(module, str(ROOT / 'src/hud.py'), 'exec'), scope)
    return scope['Harness'], scope


Harness, HUD = hud_harness()


def block(text, x, y, w, h=.035):
    return TextBlock(text, 1.0, x, y, w, h)
