"""Download/status regressions without network, credentials or model weights."""
import ast
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from apps.registry import UNKNOWN
from judge import Judge, FallbackJudge, _download_progress


class DownloadTests(unittest.TestCase):
    def test_resume_and_completion(self):
        reports = []
        bar = _download_progress(reports.append, min_interval=0)(unit='B', total=4_000_000_000,
                                                  initial=1_000_000_000, disable=True)
        self.assertIn('25% · 1.0/4.0 GB', reports[-1])
        bar.update(1_000_000_000)
        self.assertIn('50% · 2.0/4.0 GB', reports[-1])
        bar.update(2_000_000_000)
        self.assertIn('100%', reports[-1])
        bar.close()
        self.assertEqual(reports[-1], '加载判断模型…')
        count = len(reports)
        bar.close()
        self.assertEqual(len(reports), count)

    def test_report_throttled_to_min_interval(self):
        # Per-chunk update() storms held the GIL away from the Cocoa main thread
        # (probe: beachball + hang at 63%). Intermediate reports are droppable.
        reports = []
        bar = _download_progress(reports.append, min_interval=0.5)(
            unit='B', total=4_000_000_000, initial=0, disable=True)
        bar.update(1_000_000_000)
        self.assertIn('25%', reports[-1])          # first report always fires
        bar.update(500_000_000)
        self.assertIn('25%', reports[-1])          # inside the window: dropped
        bar.update(2_500_000_000)
        self.assertIn('100%', reports[-1])         # completion is never throttled
        bar.close()
        self.assertEqual(reports[-1], '加载判断模型…')

    def test_xet_transfer_is_not_counted_twice(self):
        try:
            # 私有模块：huggingface_hub 只钉下限，锁升级后接口可能变动——跳过而非收集期崩
            from huggingface_hub.utils._xet_progress_reporting import XetDownloadProgressReporter
        except ImportError:
            self.skipTest("huggingface_hub 私有 _xet_progress_reporting 不可用")
        reports = []
        with XetDownloadProgressReporter(
            reconstruction_desc='model: reconstructing file', total=4_000_000_000,
            log_level=100, name='huggingface_hub.xet_get',
            tqdm_class=_download_progress(reports.append, min_interval=0),
        ) as progress:
            progress.update_progress(SimpleNamespace(
                total_bytes_completed=1_000_000_000,
                total_transfer_bytes_completed=200_000_000,
                total_bytes_completion_rate=1, total_transfer_bytes_completion_rate=1,
                total_bytes=4_000_000_000,
            ))
            self.assertIn('25% · 1.0/4.0 GB', reports[-1])
            self.assertNotIn('已接收', reports[-1])
            # Network keeps moving while buffered model writes stand still.
            progress.update_progress(SimpleNamespace(
                total_bytes_completed=1_000_000_000,
                total_transfer_bytes_completed=300_000_000,
                total_bytes_completion_rate=0, total_transfer_bytes_completion_rate=1,
                total_bytes=4_000_000_000,
            ))
            self.assertIn('25% · 1.0/4.0 GB', reports[-1])
            self.assertNotIn('已接收', reports[-1])

    def test_file_count_and_quiet_terminal(self):
        from huggingface_hub.utils import disable_progress_bars, enable_progress_bars
        reports = []
        cls = _download_progress(reports.append, min_interval=0)
        with cls(total=1, unit='it', disable=True) as bar:
            bar.update()
        self.assertFalse(reports)
        disable_progress_bars()
        try:
            with cls(unit='B', total=100, disable=True) as bar:
                bar.update(25)
                self.assertIn('25%', reports[-1])
        finally:
            enable_progress_bars()

    def test_snapshot_total_updated_after_creation(self):
        reports = []
        with _download_progress(reports.append, min_interval=0)(unit='B', total=0) as bar:
            self.assertFalse(reports)
            bar.total = 100
            bar.refresh()
            bar.update(34)
            self.assertIn('34%', reports[-1])

    def local(self):
        j = Judge.__new__(Judge)
        j._loaded = False
        j.load_status = None
        j._load_lock = threading.RLock()
        j.repo = 'test/model'
        j.device = 'cpu'
        j.torch = SimpleNamespace(float32='float32')
        return j

    def fake_transformers(self, loader):
        tok = Mock()
        tok.encode.return_value = [1]
        return SimpleNamespace(AutoTokenizer=SimpleNamespace(from_pretrained=Mock(return_value=tok)),
                               AutoModelForCausalLM=SimpleNamespace(from_pretrained=loader))

    def test_cache_hit_has_no_download_and_loads_once(self):
        j = self.local()
        observed = []
        def cached(*args, **kwargs):
            observed.append(j.load_status)
            return Mock()
        loader = Mock(side_effect=cached)
        with patch.dict(sys.modules, transformers=self.fake_transformers(loader)), \
                patch('judge.low_memory_reason', return_value=None), \
                patch('judge.download_block_reason', return_value=None):
            j._load()
            j._load()
        self.assertEqual(loader.call_count, 1)
        self.assertEqual(observed, ['加载判断模型…'])
        self.assertIsNone(j.load_status)
        self.assertTrue(j._loaded)

    def test_failure_clears_progress_and_preserves_exception(self):
        j = self.local()
        def fail(*args, **kwargs):
            with kwargs['tqdm_class'](unit='B', total=100) as bar:
                bar.update(20)
                self.assertIn('20%', j.load_status)
                raise OSError('disk full')
        with patch.dict(sys.modules, transformers=self.fake_transformers(fail)), \
                patch('judge.low_memory_reason', return_value=None), \
                patch('judge.download_block_reason', return_value=None):
            with self.assertRaisesRegex(OSError, 'disk full'):
                j._load()
        self.assertIsNone(j.load_status)
        self.assertFalse(j._loaded)

    def test_warm_and_load_still_share_one_lock(self):
        j = self.local()
        entered, release, other_done = threading.Event(), threading.Event(), threading.Event()
        states = []
        def forward(_):
            states.append(j.load_status)
            entered.set()
            release.wait(3)
        j.judge = forward
        loader = Mock(return_value=Mock())
        with patch.dict(sys.modules, transformers=self.fake_transformers(loader)), \
                patch('judge.low_memory_reason', return_value=None), \
                patch('judge.download_block_reason', return_value=None):
            warm = threading.Thread(target=j.warm)
            warm.start()
            self.assertTrue(entered.wait(3))
            def other():
                with j._load_lock:
                    j._load()
                other_done.set()
            thread = threading.Thread(target=other)
            thread.start()
            try:
                self.assertFalse(other_done.wait(.05))
            finally:
                release.set()
                warm.join(3)
                thread.join(3)
        self.assertTrue(other_done.is_set())
        self.assertEqual(loader.call_count, 1)
        self.assertEqual(states, ['预热判断模型…'])
        self.assertIsNone(j.load_status)

    def test_cloud_to_local_exposes_same_progress(self):
        j = FallbackJudge.__new__(FallbackJudge)
        j.local = None
        self.assertIsNone(j.load_status)
        j.local = SimpleNamespace(load_status='下载判断模型 34%')
        self.assertEqual(j.load_status, j.local.load_status)


def hud_harness():
    source = ast.parse((ROOT / 'src/hud.py').read_text())
    cls = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == 'HudController')
    methods = [n for n in cls.body if isinstance(n, ast.FunctionDef)
               and n.name in {'_render', '_refresh_model_status', 'tick_', 'applyHidden_'}]
    for method in methods:
        method.decorator_list = []
        method.returns = None
        for arg in method.args.args:
            arg.annotation = None
    module = ast.fix_missing_locations(ast.Module(body=[ast.ClassDef(
        name='Harness', bases=[], keywords=[], body=methods, decorator_list=[])], type_ignores=[]))
    scope = {'PALETTE': {'amber': 'amber', 'muted': 'muted', 'red': 'red'},
            # tick_ 的前台检查在本测试作用域外：UNKNOWN 表示检查不了，tick_ 直接返回
            'frontmost_app': lambda: UNKNOWN, 'UNKNOWN': UNKNOWN,
            'time': time}
    exec(compile(module, 'hud.py', 'exec'), scope)
    return scope['Harness']


class HudStatusTests(unittest.TestCase):
    def test_busy_or_paused_does_not_hide_download_and_restores_latest_status(self):
        h = hud_harness()()
        h.rows = {'status': Mock()}
        h._show = Mock()
        h.panel = Mock()
        h.panel.isVisible.return_value = False
        h._app = None
        h.judge = SimpleNamespace(load_status=None)
        h._model_status = None
        h._paused = True
        h._render('status', '等待微信消息…', 'muted')
        h.judge.load_status = '下载判断模型 34% · 1.2/3.8 GB'
        h.tick_(None)
        h.rows['status'].setStringValue_.assert_called_with(h.judge.load_status)
        h._render('status', '分析中…', 'muted')
        h.rows['status'].setStringValue_.assert_called_with(h.judge.load_status)
        h.judge.load_status = None
        h.tick_(None)
        h.rows['status'].setStringValue_.assert_called_with('分析中…')
        h.rows['status'].setTextColor_.assert_called_with('muted')

    def test_missing_wechat_does_not_hide_active_download(self):
        h = hud_harness()()
        h.rows = {'status': Mock()}
        h.judge = SimpleNamespace(load_status='下载判断模型 34%')
        h.panel, h._ov_panel = Mock(), Mock()
        h.applyHidden_('没有微信窗口')
        h.panel.orderOut_.assert_not_called()
        h._ov_panel.orderOut_.assert_called_once()
        h.judge.load_status = None
        h.applyHidden_('没有微信窗口')
        h.panel.orderOut_.assert_called_once()

    def test_auto_hide_can_be_disabled(self):
        h = hud_harness()()
        h.rows = {'status': Mock()}
        h.judge = SimpleNamespace(load_status=None)
        h.panel, h._ov_panel = Mock(), Mock()
        h.panel.isVisible.return_value = True
        h.auto_hide = False
        h.applyHidden_('微信不在前台')
        h.panel.orderOut_.assert_not_called()
        h._ov_panel.orderOut_.assert_called_once()


if __name__ == '__main__':
    unittest.main()
