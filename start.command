#!/bin/zsh
# 启动 jev-jarvis 悬浮窗（不装 LaunchAgent，按需手动启动）
cd "$(dirname "$0")" || exit 1
export USE_TF=0
# uv installs to ~/.local/bin; a Finder-launched .command does not inherit a login shell
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
# same user-level env the .app launcher uses (API keys live outside the repo)
[ -f "$HOME/.config/jev-jarvis/env" ] && source "$HOME/.config/jev-jarvis/env"

LOG="$HOME/Library/Logs/jev-jarvis.log"
mkdir -p "$(dirname "$LOG")" || exit 1
source ./packaging/bootstrap_uv.sh || exit 1
if ! command -v uv >/dev/null 2>&1; then
    print "未找到 uv，正在自动安装；进度日志：$LOG"
fi
if ! jev_ensure_uv "$LOG"; then
    print -r -- "$JEV_UV_ERROR"
    print -r -- "详情：$LOG。也可手动运行 brew install uv 后重试。"
    exit 1
fi

exec uv run python src/hud.py
