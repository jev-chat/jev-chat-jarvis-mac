#!/bin/sh
# Shared by start.command and the packaged launcher. Source this file, then call
# jev_ensure_uv LOG_PATH. Failures leave a user-facing reason in JEV_UV_ERROR.

jev_load_env() {
    # Let Python load editable context settings from their file, without mistaking
    # sourced exports for external overrides. Locals restore any inherited values
    # on return; other settings (including shell/keychain credentials) still export.
    local JEV_HISTORY JEV_CONTEXT_MESSAGES
    if [ -f "$1" ]; then
        . "$1"
    fi
}

jev_check_arch() {
    # torch (>=2.14) ships no macOS x86_64 wheel, so an x86_64 process would only
    # die later in `uv sync` with an opaque resolver error (issue #19). Both launch
    # entries call this before any install work; failure sets JEV_ARCH_ERROR.
    # On a real Intel Mac `sysctl sysctl.proc_translated` fails (unknown oid), so
    # the empty/failed output falls through to the Intel branch.
    JEV_ARCH_ERROR=""
    [ "$(uname -m)" = "x86_64" ] || return 0
    if [ "$(sysctl -n sysctl.proc_translated 2>/dev/null || true)" = "1" ]; then
        JEV_ARCH_ERROR="检测到本应用正以 Rosetta（Intel 转译）方式运行。请改用原生 ARM 方式启动：终端里请退出 x86_64 终端、用原生终端重跑；.app 请右键「显示简介」取消勾选「使用 Rosetta 打开」后重试。"
    else
        JEV_ARCH_ERROR="本应用仅支持 Apple Silicon（M 系列）Mac：本地判断模型依赖的 torch 没有 Intel Mac 版本，无法运行。"
    fi
    return 1
}

jev_ensure_uv() {
    local uv_log="$1" install_script="" curl_code=0 install_code=0 brew_code=0
    JEV_UV_ERROR=""
    if command -v uv >/dev/null 2>&1 && uv --version >/dev/null 2>&1; then
        return 0
    fi

    printf '%s\n' 'uv 不可用，正在下载并执行官方安装脚本' >> "$uv_log"
    if install_script=$(mktemp "${TMPDIR:-/tmp}/jev-uv.XXXXXX"); then
        # Download completely before execution: curl | sh can report success when
        # curl fails, or execute a truncated script. Keep curl's original exit code.
        if curl -LsSf --connect-timeout 10 --max-time 60 \
                --retry 2 --retry-delay 1 --retry-max-time 120 \
                -o "$install_script" https://astral.sh/uv/install.sh >> "$uv_log" 2>&1; then
            # Pin the destination to the PATH used by both Finder and source runs;
            # do not depend on a shell-profile edit taking effect in this process.
            if UV_INSTALL_DIR="$HOME/.local/bin" UV_NO_MODIFY_PATH=1 \
                    sh "$install_script" >> "$uv_log" 2>&1; then
                if command -v uv >/dev/null 2>&1 && uv --version >> "$uv_log" 2>&1; then
                    rm -f "$install_script"
                    return 0
                fi
                JEV_UV_ERROR="官方安装脚本已结束，但 uv 仍不可用"
            else
                install_code=$?
                JEV_UV_ERROR="uv 官方安装脚本执行失败（退出码 $install_code），请查看日志中的二进制下载或权限错误"
            fi
        else
            curl_code=$?
            case "$curl_code" in
                28) JEV_UV_ERROR="uv 安装脚本下载超时，请检查网络或代理" ;;
                5|6) JEV_UV_ERROR="uv 下载地址或代理无法解析，请检查 DNS 和代理设置" ;;
                7) JEV_UV_ERROR="无法连接 uv 下载服务器，请检查网络或代理" ;;
                35|60) JEV_UV_ERROR="uv 下载的 TLS/证书校验失败，请检查系统时间、证书或代理" ;;
                22) JEV_UV_ERROR="uv 下载服务器返回 HTTP 错误，请查看日志" ;;
                23) JEV_UV_ERROR="无法保存 uv 安装脚本，请检查磁盘空间和临时目录权限" ;;
                *) JEV_UV_ERROR="uv 安装脚本下载失败（curl 退出码 $curl_code），请查看日志" ;;
            esac
        fi
        rm -f "$install_script"
    else
        JEV_UV_ERROR="无法创建 uv 安装临时文件，请检查临时目录权限和磁盘空间"
    fi

    printf '%s\n' "$JEV_UV_ERROR" >> "$uv_log"
    if command -v brew >/dev/null 2>&1; then
        printf '%s\n' '尝试使用已安装的 Homebrew 安装 uv' >> "$uv_log"
        if CI=1 NONINTERACTIVE=1 HOMEBREW_NO_AUTO_UPDATE=1 \
                brew install uv >> "$uv_log" 2>&1; then
            if command -v uv >/dev/null 2>&1 && uv --version >> "$uv_log" 2>&1; then
                JEV_UV_ERROR=""
                return 0
            fi
            JEV_UV_ERROR="$JEV_UV_ERROR；Homebrew 安装后 uv 仍不可用，请检查 PATH"
        else
            brew_code=$?
            JEV_UV_ERROR="$JEV_UV_ERROR；Homebrew 安装也失败（退出码 $brew_code）"
        fi
    else
        JEV_UV_ERROR="$JEV_UV_ERROR；未检测到 Homebrew"
    fi
    printf '%s\n' "$JEV_UV_ERROR" >> "$uv_log"
    return 1
}
