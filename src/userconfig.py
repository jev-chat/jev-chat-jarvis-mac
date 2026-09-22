"""Configuration — one set of names, one format, three places to look.

All personal settings live OUTSIDE the repository (so `git add -A` can never leak a key).
The format is always shell-style `KEY=value`; only the search order varies, because this app
is both a .app and a terminal tool:

    1. real environment      wins over everything (good for a one-off override)
    2. ~/.config/jev-jarvis/env            <- where the README tells you to put your keys
    3. <project>/.env                      <- for working on the repo itself

Both system conventions are searched for that `env` file, since the app is a GUI bundle and
a CLI tool at once:

    macOS native (GUI apps)   ~/Library/Application Support/jev-jarvis/   <- also holds the venv
    dev-tool convention       ~/.config/jev-jarvis/   (or $XDG_CONFIG_HOME/jev-jarvis/)

One format (`env`), one file to remember. Deliberately not two: a second accepted file with
the same setting names is how you end up carefully editing the one nothing reads.

The names are the conventional ones you likely already export for other tools:

    TYPESAFE_API_KEY     TypeSafe Jev — the "mouthless" model that judges intent + risk
    TYPESAFE_BASE_URL    default https://api.typesafe.ai   (gateways: see README)
    TYPESAFE_MODEL       default jev-latest

    OPENAI_API_KEY       reply-candidate generation, any OpenAI-compatible endpoint
    OPENAI_BASE_URL      e.g. https://api.deepseek.com, http://localhost:11434/v1
    OPENAI_MODEL         e.g. deepseek-chat, glm-4-flash, qwen2.5:7b

    ANTHROPIC_API_KEY    same job, for Anthropic-compatible endpoints instead
    ANTHROPIC_BASE_URL
    ANTHROPIC_MODEL

    LLM_MODEL            shared model name, used when the per-provider one is absent
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

PROJECT_ENV = Path(__file__).resolve().parent.parent / ".env"


def config_dirs() -> list[Path]:
    """Candidate config homes, most specific first.

    macOS puts GUI-app data in ~/Library/Application Support; developer CLI tools
    conventionally use ~/.config (XDG). This app is both, so we read either — and say
    which one won in `--check`, because a silently ignored config file is worse than none.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    dirs = []
    if xdg:
        dirs.append(Path(xdg) / "jev-jarvis")
    dirs.append(Path.home() / ".config" / "jev-jarvis")
    dirs.append(Path.home() / "Library" / "Application Support" / "jev-jarvis")
    return dirs


def env_files() -> list[Path]:
    """The `env` file in each candidate directory, in priority order."""
    return [d / "env" for d in config_dirs()]


# kept for callers that want to name the canonical (dev-tool) location
CONFIG_DIR = Path.home() / ".config" / "jev-jarvis"
ENV_FILE = CONFIG_DIR / "env"


def split_env_comment(value: str) -> tuple[str, str]:
    """Split shell comments outside quotes, including escaped/concatenated quotes."""
    quote = None
    escaped = False
    for i, ch in enumerate(value):
        if escaped:
            escaped = False
        elif ch == "\\" and quote != "'":
            escaped = True
        elif quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and i > 0 and value[i - 1] in " \t":
            return value[:i].rstrip(), value[i:]
    return value, ""


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a shell-style env file: KEY=VALUE, optional `export`, quotes, # comments.

    Comments are stripped by scanning rather than splitting on " #": splitting first used
    to skip the unquoting step, which turned `KEY=""` into the two characters `""`
    (truthy!) and left literal quotes inside real keys.
    """
    out: dict[str, str] = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return out

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip()

        val, _comment = split_env_comment(val)
        try:
            lexer = shlex.shlex(val, posix=True)
            lexer.whitespace = ""
            lexer.commenters = ""
            val = "".join(lexer)
        except ValueError:
            continue  # malformed shell quoting is not a usable setting
        if key:
            out[key] = val
    return out


def _merged_env_file() -> dict[str, str]:
    """Union of every candidate `env` file; earlier directories win on conflicts."""
    out: dict[str, str] = {}
    for f in reversed(env_files()):
        out.update(parse_env_file(f))
    return out


def _label() -> str:
    """Name the source by the real file(s) it read, not by a generic label."""
    paths = [f for f in env_files() if parse_env_file(f)]
    if not paths:
        return "env"
    home = str(Path.home())
    return ", ".join(str(f).replace(home, "~") for f in paths)


_startup_sources: list[tuple[str, dict[str, str]]] | None = None

# Session-scoped overrides: read first by get(), never persisted. Exists because the
# startup snapshot freezes os.environ at import time, so a plain os.environ write later
# is invisible to get() — the #38 first-run dialog needs its choice honoured immediately,
# not after a restart.
_session_overrides: dict[str, str] = {}


def session_override(key: str, value: str) -> None:
    """Make `key` read as `value` for the rest of this process, ahead of every source."""
    _session_overrides[key] = value


def _sources() -> list[tuple[str, dict[str, str]]]:
    if _startup_sources is not None:
        return _startup_sources
    return [
        ("环境变量", dict(os.environ)),
        (_label(), _merged_env_file()),
        (str(PROJECT_ENV), parse_env_file(PROJECT_ENV)),
    ]


def get(*names: str) -> str:
    """First non-empty value among `names`, searching sources in priority order."""
    for name in names:
        if _session_overrides.get(name):
            return _session_overrides[name]
    for _src, vals in _sources():
        for name in names:
            if vals.get(name):
                return vals[name]
    return ""


def source_of(*names: str) -> str:
    for src, vals in _sources():
        for name in names:
            if vals.get(name):
                return src
    return "none"


def provider(prefix: str) -> dict[str, str]:
    """Resolve one provider's triple, anchored on its key.

    A key and its endpoint must come from the same place — mixing them means calling
    provider A with provider B's key and getting an unexplained 401. So whichever source
    supplies the key also supplies base/model; other sources only fill the gaps.
    """
    key_name = f"{prefix}_API_KEY"
    for src, vals in _sources():
        if vals.get(key_name):
            return {
                "key": vals[key_name],
                "base": vals.get(f"{prefix}_BASE_URL") or get(f"{prefix}_BASE_URL"),
                "model": (vals.get(f"{prefix}_MODEL") or get(f"{prefix}_MODEL")
                          or get("LLM_MODEL")),
                "source": src,
            }
    return {"key": "", "base": get(f"{prefix}_BASE_URL"),
            "model": get(f"{prefix}_MODEL") or get("LLM_MODEL"), "source": "none"}


def load() -> dict[str, str]:
    """Copy the user env files into os.environ (variables already set win)."""
    global _startup_sources
    # Keep this process on its startup configuration: settings saves require restart.
    if _startup_sources is None:
        _startup_sources = _sources()
    loaded = _merged_env_file()
    for key, val in loaded.items():
        if val and not os.environ.get(key):
            os.environ[key] = val
    return loaded


def where() -> str:
    """Which config file is actually supplying the keys — for --check style output."""
    for f in env_files():
        if parse_env_file(f):
            return str(f)
    return "（未找到配置文件）"
