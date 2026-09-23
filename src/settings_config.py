"""Settings editor and explicit network probes; never mutates running credentials."""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import re
import shlex
import tempfile
import urllib.error
import urllib.parse

import orcarouter
import userconfig
from generate import _endpoint, base_is_verbatim_action, http_post_json, jev_request_url, Generator, ThinkingOnlyError

PREFIXES = ("TYPESAFE", "OPENAI", "ANTHROPIC", "ORCAROUTER")
FIELDS = ("API_KEY", "BASE_URL", "MODEL")
DEFAULTS = {
    "TYPESAFE": ("https://api.typesafe.ai", "jev-latest"),
    "OPENAI": ("https://api.openai.com/v1", ""),
    "ANTHROPIC": ("https://api.anthropic.com", ""),
    # OrcaRouter's model comes from the live catalog, so no default is offered here; the
    # neutral routing model is only what a headless configuration falls back to.
    "ORCAROUTER": (orcarouter.API_BASE_DEFAULT, ""),
}
# Written through the same guarded editor as the provider groups. ORCAROUTER_ACCOUNT_ID
# records which account a signed-in key belongs to — a label for the status line and for
# attributing a rejected credential, never a second credential.
EXTRA_KEYS = ("ORCAROUTER_ACCOUNT_ID",)
ASSIGNMENT = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z_0-9]*)(\s*=\s*)(.*)$")


def read_document(path: Path) -> str:
    try:
        return path.read_text()
    except FileNotFoundError:
        return ""


def write_settings(path: Path, original: str, changes: dict[str, str]) -> str:
    """Change only edited assignments, preserve other lines, replace atomically at 0600."""
    if read_document(path) != original:
        raise ValueError("配置文件已被其他程序修改，请关闭设置窗口后重新打开。")
    # JUDGE_BACKEND is the first-run dialog's choice (judge.download_block_reason);
    # the settings window's offline-model section writes it through the same guarded path.
    allowed = ({f"{p}_{f}" for p in PREFIXES for f in FIELDS}
               | {"JUDGE_BACKEND"} | set(EXTRA_KEYS))
    if not changes.keys() <= allowed:
        raise ValueError("不支持的配置项。")
    for value in changes.values():
        if any(c in value for c in "\r\n\0"):
            raise ValueError("配置值不能含换行或空字符。")
    remaining = dict(changes)
    lines = []
    for line in original.splitlines(keepends=True):
        match = ASSIGNMENT.match(line.rstrip("\r\n"))
        if match and match[2] in changes:
            key = match[2]
            # Keep even duplicate assignments consistent, so shell and Python agree.
            _, comment = userconfig.split_env_comment(match[4])
            ending = "\n" if line.endswith("\n") else ""
            line = f"{match[1]}{key}{match[3]}{shlex.quote(changes[key])}"
            line += (" " + comment if comment else "") + ending
            remaining.pop(key, None)
        lines.append(line)
    text = "".join(lines)
    if remaining:
        if text and not text.endswith("\n"):
            text += "\n"
        text += "".join(f"export {k}={shlex.quote(v)}\n" for k, v in remaining.items())
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".env-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            out.write(text)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return text


def validate_endpoint(base: str) -> str:
    base = base.strip().rstrip("/")
    p = urllib.parse.urlsplit(base)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise ValueError("服务地址需为 http(s) 地址，不包含用户名、密码、查询参数或片段。")
    return base


def validate_orcarouter_endpoint(base: str) -> str:
    """OrcaRouter's own origin rule: HTTPS anywhere, HTTP only for loopback."""
    try:
        return orcarouter.validate_origin(base, "OrcaRouter 服务地址")
    except ValueError as exc:
        raise ValueError("OrcaRouter 服务地址需为 https 地址（本机地址可用 http）。") from exc


def list_orcarouter_models(base: str, key: str, capability: str = "chat",
                           require_input: str = "", refresh: bool = True):
    """Capability-filtered live catalog. Returns an orcarouter.Catalog, never raises.

    The dropdown for OrcaRouter is built from this and only this: the live catalog when it
    answers, and an explicitly labelled verified fallback when it does not. The caller must
    not turn either case into a free-text model field.
    """
    base = validate_orcarouter_endpoint(base)
    return orcarouter.catalog().fetch(base, key, capability, require_input, refresh)


def list_models(prefix: str, base: str, key: str) -> list[str]:
    """GET the provider's models endpoint. No presets, redirects or alternate service."""
    if prefix == "ORCAROUTER":
        raise ValueError("OrcaRouter 的模型列表按能力筛选，请使用 list_orcarouter_models。")
    base = validate_endpoint(base)
    if not key:
        raise ValueError("请先填写密钥；Ollama 可填写 ollama。")
    if prefix == "TYPESAFE" and base_is_verbatim_action(base):
        # A complete action path (e.g. Vercel …/v1/evaluate) has no sibling /models we
        # can derive — appending anything would just 404 on the action itself.
        raise ValueError("该地址是完整动作路径，模型列表不可用，请手动填写模型名。")
    api = "anthropic" if prefix == "ANTHROPIC" else "openai"
    url = _endpoint(base, api).rsplit("/", 1)[0]
    if api == "openai":
        url = url.removesuffix("/chat")
    url += "/models"
    headers = ({"x-api-key": key, "anthropic-version": "2023-06-01"}
               if api == "anthropic" else {"authorization": f"Bearer {key}"})
    offered = []
    after = None
    while True:
        p = urllib.parse.urlsplit(url)
        path = p.path + ("?after_id=" + urllib.parse.quote(after, safe="") if after else "")
        cls = http.client.HTTPSConnection if p.scheme == "https" else http.client.HTTPConnection
        conn = cls(p.hostname, p.port, timeout=15)
        try:
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            if resp.status >= 300:
                raise urllib.error.HTTPError(url, resp.status, "", resp.headers, None)
            data = json.loads(resp.read())
        finally:
            conn.close()
        # TypeSafe documents {models: [{name, description, release_date}]};
        # OpenAI/Anthropic use {data: [{id, ...}]}. Do not guess alternate schemas.
        collection, field = ("models", "name") if prefix == "TYPESAFE" else ("data", "id")
        offered.extend(m[field] for m in data.get(collection, [])
                       if isinstance(m, dict) and isinstance(m.get(field), str) and m[field])
        if api != "anthropic" or not data.get("has_more"):
            break
        next_id = data.get("last_id")
        if not next_id or next_id == after:
            raise ValueError("模型列表分页返回异常，请手动填写模型。")
        after = next_id
    if not offered:
        raise ValueError("服务未返回模型列表，请手动填写模型。")
    return sorted(set(offered))


def test_connection(prefix: str, base: str, key: str, model: str, extra: dict | None = None) -> None:
    """Use exactly the unsaved form values; never fall back to built-in credentials."""
    base = (validate_orcarouter_endpoint(base) if prefix == "ORCAROUTER"
            else validate_endpoint(base))
    if not key or not model.strip():
        raise ValueError("请填写密钥和模型后再测试。")
    if prefix == "TYPESAFE":
        # Same endpoint/transport as JevJudge — through the SAME composition rule, so a
        # base that tests well here cannot 404 at run time (…/v1, Vercel verbatim, …).
        body = {"model": model, "state": "你好", "questions": {
            "test": {"type": "choice", "instructions": "请选择问候", "criteria": {"问候": None}}}}
        data = http_post_json(jev_request_url(base), {
            "content-type": "application/json", "authorization": f"Bearer {key}"}, body, 30)
        if ((data.get("answers") or {}).get("test") or {}).get("choice") != "问候":
            raise ValueError("服务返回了响应，但未返回有效判断结果。")
        return
    api = "anthropic" if prefix == "ANTHROPIC" else "openai"
    body = {"model": model, "max_tokens": 300, "temperature": 0.9,
            "messages": [{"role": "user", "content": "请只回复：连接成功"}]}
    headers = {"content-type": "application/json"}
    if api == "anthropic":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    else:
        body.update(extra or {})
        # Testing must exercise the model the user selected, not an extra-body override.
        body.update(model=model, stream=False)
        headers["authorization"] = f"Bearer {key}"
    if prefix == "ORCAROUTER":
        # Same request the generation layer sends (src/orcarouter.chat_request), so a
        # model that tests well here cannot fail at run time.
        try:
            data = orcarouter.chat_request(base, key, model, body["messages"],
                                           max_tokens=300, timeout=30)
        except urllib.error.HTTPError as exc:
            raise OrcaRouterHTTPError(exc.code) from exc
        raw = Generator._openai_json(data, model, "非思考模型")
    else:
        data = http_post_json(_endpoint(base, api), headers, body, 30)
        if api == "anthropic":
            raw = "".join(p.get("text", "") for p in data.get("content", []) if isinstance(p, dict))
        else:
            raw = Generator._openai_json(data, model, "非思考模型")
    if not raw.strip():
        raise ValueError("服务未返回文字；请检查模型是否支持生成，或关闭思考模式。")


class OrcaRouterHTTPError(Exception):
    """A rejected OrcaRouter request, carrying only the status code.

    The relay's error bodies are not shown: they can echo request details, and the user
    only needs to know whether to retry, fix the key, or sign in again.
    """

    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status = status


def error_message(error: Exception) -> str:
    """Never display raw remote bodies, URLs or exception strings containing credentials."""
    if isinstance(error, OrcaRouterHTTPError):
        if error.status in (401, 403):
            return (f"HTTP {error.status}：密钥无效或无权使用该模型。"
                    "可在设置窗口重新登录，或到 OrcaRouter 控制台确认密钥权限。")
        if error.status == 429:
            return "HTTP 429：请求过于频繁，请稍后重试。"
        return f"HTTP {error.status}：OrcaRouter 未接受该请求，请检查模型名与账户额度。"
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code}：请检查地址、密钥及模型权限。"
    if isinstance(error, ThinkingOnlyError):
        return "模型只返回了思考内容，没有正文；请关闭思考模式或更换模型。"
    if isinstance(error, (TimeoutError, OSError, http.client.HTTPException)):
        return "连接失败或超时，请检查服务地址和网络。"
    return "请求未得到有效结果，请检查地址、模型及服务是否支持该接口。"
