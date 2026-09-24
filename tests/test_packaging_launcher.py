"""Regression guards for the packaged app's first-launch bootstrap."""

from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "probe" / "bootstrap_regression.py"


class PackagedLauncherTests(unittest.TestCase):
    def test_offline_bootstrap_regression(self):
        completed = subprocess.run(
            [sys.executable, str(PROBE)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
