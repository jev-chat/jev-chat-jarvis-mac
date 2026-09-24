"""Run the native settings smoke flow under unittest/coverage on macOS CI."""
from __future__ import annotations

import runpy
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class NativeSettingsSmokeTests(unittest.TestCase):
    def test_native_settings_workflow(self):
        # The probe uses only temporary configuration, synthetic chat messages and a
        # loopback HTTP server.  Keeping it in discovery makes the diff-coverage gate
        # account for the native UI paths that ordinary model/config unit tests cannot.
        runpy.run_path(str(ROOT / "probe" / "settings_smoke.py"), run_name="__main__")


if __name__ == "__main__":
    unittest.main()
