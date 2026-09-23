"""Offline launcher regression. Run with python probe/bootstrap_regression.py.

Uses zsh if available, otherwise bash (or --shell PATH). curl, brew, uv,
osascript, HOME and the venv are isolated fixtures: nothing is downloaded or
installed. The .app shell bootstrap is extracted from the actual build heredoc;
the native Mach-O launcher is outside this offline test's scope.
"""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHELL = None

PRELUDE = r'''
export HOME="$PWD/home"
export TMPDIR="$PWD/tmp"
export FIXTURE_ROOT="$PWD"
export TRACE="$PWD/trace"
mkdir -p "$HOME" "$TMPDIR"
if [ "$SCENARIO" = existing ]; then
    mkdir -p "$HOME/.local/bin"
    cp "$FIXTURE_ROOT/uv" "$HOME/.local/bin/uv"
    chmod +x "$HOME/.local/bin/uv"
fi
# bash lacks zsh's print builtin. Production script is unchanged.
if [ -n "${BASH_VERSION:-}" ]; then
    print() { [ "${1:-}" != -r ] || shift; [ "${1:-}" != -- ] || shift; printf '%s\n' "$*"; }
fi
command() {
    if [ "${1:-}" = -v ] && [ "${2:-}" = uv ]; then
        [ -f "$HOME/.local/bin/uv" ] || return 1
        printf '%s\n' "$HOME/.local/bin/uv"
    elif [ "${1:-}" = -v ] && [ "${2:-}" = brew ]; then
        case "$SCENARIO" in brew|brew-fails|install-fails-brew) ;; *) return 1 ;; esac
        printf '%s\n' brew
    else
        builtin command "$@"
    fi
}
curl() {
    printf 'curl %s\n' "$*" >> "$TRACE"
    local output=""
    while [ "$#" -gt 0 ]; do
        case "$1" in -o|--output) output="$2"; shift ;; esac
        shift
    done
    if [ -n "$output" ]; then cat "$FIXTURE_ROOT/installer" > "$output"; fi
    case "$SCENARIO" in
        timeout|brew|brew-fails) return 28 ;;
        download-*) return "${SCENARIO#download-}" ;;
    esac
    if [ -z "$output" ]; then cat "$FIXTURE_ROOT/installer"; fi
}
brew() {
    printf 'brew %s\n' "$*" >> "$TRACE"
    [ "$SCENARIO" != brew-fails ] || return 9
    mkdir -p "$HOME/.local/bin"
    cp "$FIXTURE_ROOT/uv" "$HOME/.local/bin/uv"
    chmod +x "$HOME/.local/bin/uv"
}
uv() { "$HOME/.local/bin/uv" "$@"; }
osascript() { printf 'osascript %s\n' "$*" >> "$TRACE"; }
source "$1"
'''

INSTALLER = r'''#!/bin/sh
echo installer-ran >> "$TRACE"
echo "install-dir=$UV_INSTALL_DIR modify-path=$UV_NO_MODIFY_PATH" >> "$TRACE"
case "$SCENARIO" in
    install-fails|install-fails-brew) echo 'binary download failed' >&2; exit 7 ;;
    missing-binary) exit 0 ;;
esac
mkdir -p "$HOME/.local/bin"
cp "$FIXTURE_ROOT/uv" "$HOME/.local/bin/uv"
chmod +x "$HOME/.local/bin/uv"
'''

CONTEXT_PROBE = r'''
if [ "${CONTEXT_PROBE:-}" = 1 ]; then
    echo "context=${JEV_HISTORY-unset}:${JEV_CONTEXT_MESSAGES-unset}" >> "$TRACE"
    [ "${OPENAI_API_KEY:-}" != fixture-key ] || echo 'shell-key-evaluated' >> "$TRACE"
fi
'''

UV = '#!/bin/sh\n' + CONTEXT_PROBE + r'''
echo "uv $*" >> "$TRACE"
case "$1" in
    --version) echo 'uv 0.0.fixture' ;;
    sync)
        mkdir -p "$UV_PROJECT_ENVIRONMENT/bin"
        cp "$FIXTURE_ROOT/python" "$UV_PROJECT_ENVIRONMENT/bin/python"
        chmod +x "$UV_PROJECT_ENVIRONMENT/bin/python" ;;
    run) echo app-started >> "$TRACE" ;;
esac
'''


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def run_launcher(scenario, source=False, context_env=None):
    with tempfile.TemporaryDirectory(prefix="jev-bootstrap-") as td:
        root = Path(td)
        build = (ROOT / "packaging/build_app.sh").read_text(encoding="utf-8")
        launcher = build.split("<<'LAUNCHER'\n", 1)[1].split("\nLAUNCHER", 1)[0]
        launcher = launcher.replace("@PYTHON_PIN@", "3.12")
        # zsh accepts this one-line function without a semicolon; bash needs it.
        launcher = launcher.replace('>> "$LOG" }', '>> "$LOG"; }')
        app = root / "jev test.app/Contents"
        (app / "Resources/app").mkdir(parents=True)
        write(app / "Resources/launcher.zsh", launcher)
        write(root / "source/start.command", (ROOT / "start.command").read_text(encoding="utf-8"))
        helper = ROOT / "packaging/bootstrap_uv.sh"
        if helper.exists():
            for dest in (app / "Resources/app/packaging/bootstrap_uv.sh",
                         root / "source/packaging/bootstrap_uv.sh"):
                write(dest, helper.read_text(encoding="utf-8"))
        if context_env is not None:
            write(root / "home/.config/jev-jarvis/env",
                  'export JEV_HISTORY=1\nexport JEV_CONTEXT_MESSAGES=20\n'
                  'export OPENAI_API_KEY="$(printf fixture-key)"\n')
        write(root / "installer", INSTALLER)
        write(root / "uv", UV)
        write(root / "python", '#!/bin/sh\n' + CONTEXT_PROBE + 'echo app-started >> "$TRACE"\n')
        target = "source/start.command" if source else "jev test.app/Contents/Resources/launcher.zsh"
        env = dict(os.environ, SCENARIO=scenario)
        if context_env is not None:
            env.pop('JEV_HISTORY', None)
            env.pop('JEV_CONTEXT_MESSAGES', None)
            env.update(context_env, CONTEXT_PROBE='1')
        # Use a relative path so Git Bash and native POSIX shells share the same fixture.
        completed = subprocess.run([SHELL, "-c", PRELUDE, target, target], cwd=root, env=env,
                                   capture_output=True, text=True, encoding="utf-8", timeout=15)
        trace = (root / "trace").read_text(encoding="utf-8") if (root / "trace").exists() else ""
        log = root / "home/Library/Logs/jev-jarvis.log"
        detail = log.read_text(encoding="utf-8") if log.exists() else ""
        return completed, trace, detail


class BootstrapRegression(unittest.TestCase):
    def test_context_file_values_are_not_misclassified_as_external_environment(self):
        for source in (False, True):
            for overrides, expected in [({}, 'unset:unset'),
                                        ({'JEV_HISTORY': '0', 'JEV_CONTEXT_MESSAGES': '7'}, '0:7')]:
                with self.subTest(source=source, overrides=overrides):
                    completed, trace, log = run_launcher('existing', source, overrides)
                    self.assertEqual(completed.returncode, 0, completed.stderr + log)
                    self.assertIn('context=' + expected, trace)
                    self.assertIn('shell-key-evaluated', trace)
                    self.assertIn('app-started', trace)

    def test_app_executes_successfully_downloaded_installer(self):
        completed, trace, log = run_launcher("success")
        self.assertIn("installer-ran", trace, "Downloaded installer was never executed")
        self.assertEqual(completed.returncode, 0, completed.stderr + log)
        self.assertIn("app-started", trace)
        self.assertIn("modify-path=1", trace)

    def test_source_launcher_uses_the_same_successful_bootstrap(self):
        completed, trace, log = run_launcher("success", source=True)
        self.assertEqual(completed.returncode, 0, completed.stderr + log)
        self.assertIn("installer-ran", trace)
        self.assertIn("app-started", trace)

    def test_timeout_never_executes_a_partial_download(self):
        for source in (False, True):
            with self.subTest(source=source):
                completed, trace, log = run_launcher("timeout", source=source)
                self.assertNotEqual(completed.returncode, 0)
                self.assertNotIn("installer-ran", trace)
                self.assertNotIn("app-started", trace)
                self.assertIn("超时", log + completed.stdout + trace)
                self.assertIn("--connect-timeout", trace)
                self.assertIn("--max-time", trace)
                self.assertIn("--retry", trace)

    def test_installer_failure_is_reported(self):
        completed, trace, log = run_launcher("install-fails")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("installer-ran", trace)
        self.assertIn("binary download failed", log)
        self.assertNotIn("app-started", trace)

    def test_homebrew_recovers_after_official_download_fails(self):
        for source in (False, True):
            with self.subTest(source=source):
                completed, trace, log = run_launcher("brew", source=source)
                self.assertEqual(completed.returncode, 0, completed.stderr + log)
                self.assertNotIn("installer-ran", trace)
                self.assertIn("brew install uv", trace)
                self.assertIn("app-started", trace)

    def test_existing_uv_skips_installation(self):
        for source in (False, True):
            with self.subTest(source=source):
                completed, trace, log = run_launcher("existing", source=source)
                self.assertEqual(completed.returncode, 0, completed.stderr + log)
                self.assertNotIn("curl ", trace)
                self.assertNotIn("installer-ran", trace)
                self.assertNotIn("brew install", trace)
                self.assertIn("app-started", trace)

    def test_specific_download_errors_are_reported(self):
        for code, expected in ((6, "DNS"), (7, "无法连接"), (22, "HTTP"),
                               (23, "磁盘"), (60, "证书")):
            with self.subTest(code=code):
                completed, trace, log = run_launcher(f"download-{code}")
                self.assertNotEqual(completed.returncode, 0)
                self.assertNotIn("installer-ran", trace)
                self.assertIn(expected, log)
                self.assertNotIn("app-started", trace)

    def test_success_exit_without_uv_is_not_accepted(self):
        completed, trace, log = run_launcher("missing-binary")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("uv 仍不可用", log)
        self.assertNotIn("app-started", trace)

    def test_brew_failure_preserves_both_failure_reasons(self):
        completed, trace, log = run_launcher("brew-fails")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("超时", log)
        self.assertIn("Homebrew 安装也失败（退出码 9）", log)
        self.assertNotIn("app-started", trace)

    def test_brew_also_recovers_from_installer_execution_failure(self):
        completed, trace, log = run_launcher("install-fails-brew")
        self.assertEqual(completed.returncode, 0, completed.stderr + log)
        self.assertIn("installer-ran", trace)
        self.assertIn("brew install uv", trace)
        self.assertIn("app-started", trace)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--shell", default=shutil.which("zsh") or shutil.which("bash"))
    args, rest = parser.parse_known_args()
    SHELL = args.shell
    if not SHELL:
        parser.error("zsh or bash is required (or supply --shell PATH)")
    unittest.main(argv=[__file__] + rest)
