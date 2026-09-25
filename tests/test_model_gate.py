import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from userconfig import session_override  # noqa: E402
import judge  # noqa: E402


# #38: 下载门与离线模型管理。全部离线——不 import torch/transformers，不读凭据，
# 不发网络请求（缓存判定只扫目录，env 值用 session_override / mock 注入）。
def _fake_cache(hf_home: Path, weights: bool = True) -> None:
    """Lay out a minimal HF cache: snapshots/<sha>/ holding a weight file."""
    snap = hf_home / "hub" / "models--Mapika--decider-2b" / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    if weights:
        (snap / "model.safetensors").write_bytes(b"\0" * 16)


class ModelCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.addCleanup(os.environ.pop, "HF_HOME", None)
        os.environ["HF_HOME"] = str(root)
        self.root = root

    def test_cached_true_when_snapshot_has_weights(self):
        _fake_cache(self.root)
        self.assertTrue(judge.model_cached())

    def test_cached_false_for_empty_snapshot(self):
        # 中断的下载留下没有权重文件的 snapshot 目录——不算已下载
        _fake_cache(self.root, weights=False)
        self.assertFalse(judge.model_cached())

    def test_cached_false_without_cache_dir(self):
        self.assertFalse(judge.model_cached())

    def test_disk_usage_dedupes_symlinks_and_skips_dangling(self):
        _fake_cache(self.root)                     # snapshot 实体 16 B
        base = self.root / "hub" / "models--Mapika--decider-2b"
        blob = base / "blobs" / "deadbeef"
        blob.parent.mkdir(parents=True)
        blob.write_bytes(b"x" * 1024)
        # 同一实体的两条 symlink（snapshot 常见布局）只计一次；悬空 symlink 跳过
        (base / "snapshots" / "abc123" / "weights.safetensors").symlink_to(blob)
        (base / "snapshots" / "abc123" / "same.safetensors").symlink_to(blob)
        (base / "snapshots" / "abc123" / "gone.bin").symlink_to(base / "blobs" / "nope")
        self.assertEqual(judge.model_disk_usage(), 16 + 1024)   # 实体字节；symlink 不计

    def test_disk_usage_zero_when_absent(self):
        self.assertEqual(judge.model_disk_usage(), 0)

    def test_snapshot_dir_resolves_refs_main_first(self):
        """#95: offline loading hands from_pretrained the snapshot refs/main pins."""
        base = self.root / "hub" / "models--Mapika--decider-2b"
        refs = base / "refs"
        refs.mkdir(parents=True)
        (refs / "main").write_text("abc123")
        snap = base / "snapshots" / "abc123"
        snap.mkdir(parents=True)
        (snap / "model.safetensors").write_bytes(b"\0" * 8)
        self.assertEqual(judge.cached_snapshot_dir(), str(snap))

    def test_snapshot_dir_falls_back_to_newest_weighted(self):
        base = self.root / "hub" / "models--Mapika--decider-2b"
        old, new = base / "snapshots" / "old1", base / "snapshots" / "new2"
        for s, weight in ((old, False), (new, True)):
            s.mkdir(parents=True)
            if weight:
                (s / "model.safetensors").write_bytes(b"\0" * 8)
        os.utime(old, (1, 1))          # dangling old snapshot loses to the weighted one
        self.assertEqual(judge.cached_snapshot_dir(), str(new))

    def test_snapshot_dir_none_without_weights_anywhere(self):
        _fake_cache(self.root, weights=False)
        self.assertIsNone(judge.cached_snapshot_dir())


class EndpointFallbackTests(unittest.TestCase):
    """#95: the mirror decision — explicit config wins, unreachable default falls back."""

    def setUp(self):
        self.addCleanup(os.environ.pop, "HF_ENDPOINT", None)
        os.environ.pop("HF_ENDPOINT", None)

    def test_explicit_endpoint_wins_untouched(self):
        os.environ["HF_ENDPOINT"] = "https://my-own-endpoint.example"
        with mock.patch.object(judge.urllib.request, "urlopen", side_effect=AssertionError):
            self.assertIsNone(judge.ensure_download_endpoint())
        self.assertEqual(os.environ["HF_ENDPOINT"], "https://my-own-endpoint.example")

    def test_reachable_default_keeps_default_endpoint(self):
        with mock.patch.object(judge.urllib.request, "urlopen") as urlopen:
            self.assertIsNone(judge.ensure_download_endpoint())
        urlopen.assert_called_once()
        self.assertIsNone(os.environ.get("HF_ENDPOINT"))

    def test_unreachable_default_switches_to_mirror(self):
        import urllib.error
        with mock.patch.object(judge.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("timeout")):
            self.assertEqual(judge.ensure_download_endpoint(), judge.FALLBACK_ENDPOINT)
        self.assertEqual(os.environ["HF_ENDPOINT"], judge.FALLBACK_ENDPOINT)


class LoadSourceTests(unittest.TestCase):
    """#95: _load 的加载源决策——已缓存走本地离线，未缓存先定端点。"""

    def test_cached_snapshot_loads_offline_without_endpoint_probe(self):
        with mock.patch.object(judge, "cached_snapshot_dir", return_value="/snap/x"), \
             mock.patch.object(judge, "ensure_download_endpoint",
                               side_effect=AssertionError) as ensure:
            self.assertEqual(judge.resolve_load_source(), ("/snap/x", True))
        ensure.assert_not_called()

    def test_uncached_resolves_endpoint_then_repo(self):
        with mock.patch.object(judge, "cached_snapshot_dir", return_value=None), \
             mock.patch.object(judge, "ensure_download_endpoint",
                               return_value=None) as ensure:
            self.assertEqual(judge.resolve_load_source(), ("Mapika/decider-2b", False))
        ensure.assert_called_once()

    def test_uncached_mirror_switch_announces(self):
        with mock.patch.object(judge, "cached_snapshot_dir", return_value=None), \
             mock.patch.object(judge, "ensure_download_endpoint",
                               return_value="https://hf-mirror.com"), \
             mock.patch.dict(os.environ, {"HF_ENDPOINT": "https://hf-mirror.com"}):
            self.assertEqual(judge.resolve_load_source(), ("Mapika/decider-2b", False))


class DownloadGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.addCleanup(os.environ.pop, "HF_HOME", None)
        os.environ["HF_HOME"] = str(root)
        self.root = root
        # 隔离本机真实配置（#133）：「未设置」用例要求 get() 看不到 env 文件与
        # 真实环境变量里的 JUDGE_BACKEND；空串覆盖会屏蔽全部下层来源
        session_override("JUDGE_BACKEND", "")
        self.addCleanup(session_override, "JUDGE_BACKEND", "")

    def _gate(self):
        return judge.download_block_reason()

    def test_unset_allows_cached_model(self):
        # 老用户已下载、从未被引导过：行为与引入本门之前完全一致
        _fake_cache(self.root)
        self.assertIsNone(self._gate())

    def test_unset_blocks_uncached_model(self):
        self.assertIsNotNone(self._gate())
        self.assertIn("TYPESAFE_API_KEY", self._gate())

    def test_local_allows_download_even_uncached(self):
        session_override("JUDGE_BACKEND", "local")
        self.assertIsNone(self._gate())

    def test_cloud_blocks_even_cached(self):
        # 选了在线判断：本地模型连「不下载的加载」也不用做
        _fake_cache(self.root)
        session_override("JUDGE_BACKEND", "cloud")
        self.assertIsNotNone(self._gate())

    def test_skip_blocks_uncached_model(self):
        session_override("JUDGE_BACKEND", "skip")
        self.assertIn("3.8 GB", self._gate())


class SettingsWriteTests(unittest.TestCase):
    def test_write_settings_accepts_judge_backend(self):
        import settings_config
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "env"
            path.write_text("# 注释保留\nexport OPENAI_MODEL=\"m\"\n")
            out = settings_config.write_settings(path, path.read_text(),
                                                 {"JUDGE_BACKEND": "local"})
            self.assertIn("export JUDGE_BACKEND=local", out)
            self.assertIn("# 注释保留", out)
            self.assertIn('OPENAI_MODEL="m"', out)   # 无关行原样保留
            self.assertEqual(path.read_text(), out)


class SessionOverrideTests(unittest.TestCase):
    def test_override_beats_every_source(self):
        session_override("JUDGE_BACKEND", "cloud")
        self.addCleanup(session_override, "JUDGE_BACKEND", "")
        self.assertEqual(judge.userconfig.get("JUDGE_BACKEND"), "cloud")
        self.assertEqual(judge.download_block_reason(),
                         judge.download_block_reason())   # 不崩即可：门读同一个值


if __name__ == '__main__':
    unittest.main()
