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
from perception import TextBlock, reply_span, reply_text
import chat_context
import settings_config
import styles
from judge import LowMemoryError, ModelNotDownloadedError


def hud_harness():
    tree = ast.parse((ROOT / 'src/hud.py').read_text())
    source = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'HudController')
    names = {'_work_inner', '_set_foreground_state', '_capture_allowed', 'applySettings_',
             '_push', '_reply_task', '_reply_current', '_push_reply',
             'applyReplyUpdate_', 'applyWaiting_', '_context_text', '_stream_hook',
             '_take_pregen', '_gen_with_pregen', '_finish_generate', '_enqueue_prework',
             '_prejudge_loop', '_pregen_loop', '_analyze', '_run_generation', 'reload_conversations',
             '_context_changed', 'save_background', 'configure_context', 'clear_history',
             '_regen_work', '_regenerate_work', 'regenerateReply_', '_rank_payload', '_payload_from_gen',
             'fillCandidate_', '_warm', '_warm_apps', 'toggleAlwaysOnTop_', 'tick_',
             '_format_read_result',
             '_read_result_meta', '_target_text', '_judge_text',
             'adjustCandidate_', '_adjust_work', 'applyAdjusted_',
             '_render_groups'}
    methods = [n for n in source.body if isinstance(n, ast.FunctionDef) and n.name in names]
    for method in methods:
        method.decorator_list = []
    klass = ast.ClassDef(name='Harness', bases=[], keywords=[], body=methods, decorator_list=[])
    from apps.registry import UNKNOWN
    locate = Mock(return_value={'box': None, 'rect': None, 'reason': 'test'})
    read_conv = Mock()
    fake_app = SimpleNamespace(key='wechat', display_name='微信', needs_screen_capture=True,
                               read_conversation=read_conv, locate_input=locate,
                               fill_text=Mock(return_value=(True, '已填入')), warm=Mock(return_value=0.0))
    scope = {'LowMemoryError': LowMemoryError, 'ModelNotDownloadedError': ModelNotDownloadedError,
             'chat_context': chat_context, 'settings_config': settings_config, 'styles': styles,
             'Generator': Mock, 'make_judge': Mock,
             'fill': SimpleNamespace(locate_input=locate, has_accessibility=Mock(return_value=True),
                                     request_accessibility=Mock()),
             'time': time, 'threading': threading, '_log': lambda *_: None,
             'AppKit': SimpleNamespace(NSFloatingWindowLevel=3, NSNormalWindowLevel=0,
                                       NSOnState=1, NSOffState=0),
             'frontmost_app': Mock(return_value=fake_app), 'FAKE_APP': fake_app, 'UNKNOWN': UNKNOWN,
             'APPS': (fake_app,),
             'screen_capture_ok': Mock(return_value=True), 'request_screen_capture': Mock(),
             'read_conversation': read_conv,
             'find_wechat_window': Mock(return_value=None),
             'reply_span': reply_span, 'reply_text': reply_text,
             'PALETTE': {'muted': None, 'red': 'RED', 'green': 'GREEN', 'amber': None},
             'SLOW_TICK': 1, 'BURST_TICK': .45, 'FAST_TICK': .25, 'BURST_READS': 3,
             'READ_FAILURE_HIDE_S': 2, 'EMPTY_FRAME_REUSE_S': 2,
             'SETTLE_S': 1.2, 'STABLE_READS': 3, 'EARLY_SETTLE_S': .7, 'MIN_GAP_S': 2}
    module = ast.fix_missing_locations(ast.Module(body=[klass], type_ignores=[]))
    exec(compile(module, str(ROOT / 'src/hud.py'), 'exec'), scope)
    return scope['Harness'], scope


Harness, HUD = hud_harness()


def block(text, x, y, w, h=.035):
    return TextBlock(text, 1.0, x, y, w, h)
