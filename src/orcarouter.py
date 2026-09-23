"""OrcaRouter — one OpenAI-compatible gateway, two ways to hand it a key.

OrcaRouter is an OpenAI-compatible AI gateway that routes many providers behind one
endpoint. This module is the whole OrcaRouter surface of the app: where its two origins
are, how a credential is obtained (pasted key or OAuth 2.0 + PKCE), how that credential is
stored and retired, and what the model catalog says each model can do.

    inference + catalog   https://api.orcarouter.ai/v1     (OpenAI wire format)
    authorization         https://www.orcarouter.ai/auth
    code exchange         https://www.orcarouter.ai/api/v1/auth/keys

Authentication and inference are **different origins** on purpose. The relay lives at
`api.orcarouter.ai/v1`; the auth endpoints are at `www.orcarouter.ai/api/v1/auth`. Deriving
one from the other by swapping a hostname (or by appending `/v1`) produces
`https://api.orcarouter.ai/v1/auth/keys`, which is a 301 — the single most common
integration mistake, and the reason both origins are named constants here instead of being
computed. A self-hosted deployment can serve both from one origin: set
`ORCAROUTER_ORIGIN`, or override either side alone with `ORCAROUTER_AUTH_BASE_URL` /
`ORCAROUTER_BASE_URL`. Explicit overrides always win over the shared one.

**Two entrances, one credential.** Pasting an `sk-orca-…` key and signing in with a browser
both end as the same thing: a normal OrcaRouter API key belonging to the user, billed to
their account and revocable from their console. `CredentialSource` is that seam; the two
adapters (`KeySource`, `PkceSource`) are the only code that knows where a key came from, and
everything downstream — the chat transport, the catalog, the settings window — takes the
`Credential` they return and never asks.

**The PKCE key is durable, not refreshable.** OrcaRouter issues no refresh token and has no
refresh endpoint, so nothing here schedules a refresh. The key is reused until OrcaRouter
revokes it; a `401` from the relay means the exact account that sent that request needs to
sign in again, which is what `mark_needs_reauth` records. Never log, print or persist the
verifier: it is the only thing standing between an intercepted auth code and a redeemable
key, and it never leaves this process.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

import userconfig

# ------------------------------------------------------------------ origins

AUTH_BASE_DEFAULT = "https://www.orcarouter.ai"
API_BASE_DEFAULT = "https://api.orcarouter.ai/v1"

AUTHORIZE_PATH = "/auth"                      # browser consent screen, not an API
EXCHANGE_PATH = "/api/v1/auth/keys"           # POST: code + verifier -> durable key
DEVICE_CODE_PATH = "/api/v1/auth/device/code"
DEVICE_TOKEN_PATH = "/api/v1/auth/device/token"

APP_NAME = "jev-jarvis"
SCOPE = "api"

CONSOLE_KEYS_URL = "https://www.orcarouter.ai/console/token"
CONSOLE_APPS_URL = "https://www.orcarouter.ai/console/authorized-apps"

# Names this app already uses for every other provider (src/userconfig.py): a key and its
# endpoint come from one source, so the same three names carry OrcaRouter too.
PREFIX = "ORCAROUTER"
KEY_NAMES = (f"{PREFIX}_API_KEY",)
BASE_NAMES = (f"{PREFIX}_BASE_URL", f"{PREFIX}_ORIGIN")
MODEL_NAMES = (f"{PREFIX}_MODEL", "LLM_MODEL")
AUTH_NAMES = (f"{PREFIX}_AUTH_BASE_URL", f"{PREFIX}_ORIGIN")
# A key obtained by signing in is written back to the same env file the settings window
# edits, so the next launch reuses it instead of minting another one (10 per user / 24 h).
STORED_KEY_NAME = f"{PREFIX}_API_KEY"
STORED_ACCOUNT_NAME = f"{PREFIX}_ACCOUNT_ID"

KEY_PREFIX = "sk-orca-"
KEY_SHAPE = re.compile(r"^sk-orca-[A-Za-z0-9._~-]{8,}$")

# Authorization codes are single-use with a 10 minute TTL; give the browser a little less
# than that so a code we are about to redeem cannot have expired on the way.
AUTHORIZE_TIMEOUT_S = 600.0
EXCHANGE_TIMEOUT_S = 30.0
CATALOG_TIMEOUT_S = 10.0
# One catalog response cannot consume unbounded memory or advertise an unbounded number
# of routes.
CATALOG_MAX_BYTES = 2_000_000
CATALOG_MAX_ITEMS = 2000


class OrcaRouterError(Exception):
    """Anything the user has to act on. Messages never contain a credential."""


class AuthRejected(OrcaRouterError):
    """The authorization server said no: denied, expired, replayed, or wrong verifier."""


class AuthFailed(OrcaRouterError):
    """Transport-level failure while talking to the authorization server."""


class NeedsReauth(OrcaRouterError):
    """The relay rejected the stored key. Sign in again; do not retry or refresh."""


# ------------------------------------------------------------------ origins


def _loopback(host: str) -> bool:
    return (host or "").lower() in ("localhost", "127.0.0.1", "[::1]", "::1")


def validate_origin(url: str, what: str) -> str:
    """One rule for both origins: http(s), a host, no userinfo/query/fragment.

    Remote origins must be HTTPS — a credential exchange over plain HTTP is readable by
    anything on the path. HTTP stays available for loopback development, and for the local
    fake auth server the tests point these overrides at.
    """
    url = (url or "").strip().rstrip("/")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"{what}需为 http(s) 地址。")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError(f"{what}不能包含用户名、密码、查询参数或片段。")
    if parts.scheme != "https" and not _loopback(parts.hostname):
        raise ValueError(f"{what}必须使用 https（本机地址可用 http）。")
    return url


def auth_base() -> str:
    """Authorization origin: explicit override > shared self-hosted origin > public default."""
    return validate_origin(
        userconfig.get(*AUTH_NAMES) or AUTH_BASE_DEFAULT, "认证地址")


def api_base() -> str:
    """Inference origin: explicit override > shared self-hosted origin > public default."""
    return validate_origin(
        userconfig.get(*BASE_NAMES) or API_BASE_DEFAULT, "服务地址")


def authorize_url(base: str) -> str:
    return base.rstrip("/") + AUTHORIZE_PATH


def exchange_url(base: str) -> str:
    return base.rstrip("/") + EXCHANGE_PATH


# ------------------------------------------------------------------ credentials


@dataclass(frozen=True)
class Credential:
    """What every adapter returns. `key` is the only thing downstream may use."""

    key: str
    source: str                      # "api_key" | "pkce" | "none"
    account: str = ""                # stable id from the exchange response, never an email
    scope: str = SCOPE
    generation: int = 0

    @property
    def present(self) -> bool:
        return bool(self.key)


class CredentialSource:
    """The seam. Two adapters implement it; nothing else knows where a key came from."""

    name = "none"

    def acquire(self) -> Credential:                 # pragma: no cover - interface
        raise NotImplementedError

    def available(self) -> bool:                     # pragma: no cover - interface
        raise NotImplementedError


def mask(key: str) -> str:
    """Display form. Never return enough of a key to be reused."""
    if not key:
        return "（未配置）"
    if len(key) <= 10:
        return "…"
    return f"{key[:6]}…{key[-4:]}  ({len(key)} chars)"


def looks_like_key(value: str) -> bool:
    """Shape check only — an `sk-orca-` prefix is not proof a credential is valid.

    OrcaRouter exposes no stable non-billing validation endpoint, so validity is reported
    as unknown and the first real request settles it. Sending a paid inference request just
    to make a settings form say 「有效」 would be worse than saying nothing.
    """
    return bool(KEY_SHAPE.match((value or "").strip()))


class KeySource(CredentialSource):
    """Adapter 1 — the user pastes (or exports) an existing `sk-orca-…` key."""

    name = "api_key"

    def __init__(self, key: str = "", account: str = "", generation: int = 0):
        self._key = (key or "").strip()
        self._account = account
        self._generation = generation

    def available(self) -> bool:
        return bool(self._key)

    def acquire(self) -> Credential:
        if not self._key:
            return Credential(key="", source="none")
        return Credential(key=self._key, source=self.name, account=self._account,
                          generation=self._generation)


def stored_credential() -> Credential:
    """The key this install already holds, from wherever this app keeps secrets.

    There is one place: the env file (`src/userconfig.py`). No second credential store is
    introduced for OrcaRouter, and nothing is written outside that file.
    """
    key = userconfig.get(*KEY_NAMES)
    if not key:
        return Credential(key="", source="none")
    return Credential(key=key.strip(), source="api_key",
                      account=userconfig.get(STORED_ACCOUNT_NAME), generation=1)


def resolve_credential() -> Credential:
    """The credential the generation layer should use, and which entrance produced it.

    The env file cannot tell a pasted key from a signed-in one — both are the same durable
    `sk-orca-…` key, which is the point. So the recorded account id is what distinguishes
    them for display, and it is deliberately *not* treated as anything but a label.
    """
    cred = stored_credential()
    if cred.present and cred.account:
        return Credential(key=cred.key, source="pkce", account=cred.account,
                          generation=cred.generation)
    return cred


def source_label(cred: Credential) -> str:
    """What to print as the credential source. Never includes the key."""
    if not cred.present:
        return "none"
    entrance = "账号登录" if cred.source == "pkce" else "手填密钥"
    origin = api_base()
    return f"OrcaRouter（{entrance}） · {origin}"


# ------------------------------------------------------------------ reauth state

_reauth_lock = threading.Lock()
_reauth: dict[str, int] = {}          # account id ("" allowed) -> credential generation
_reauth_reason: dict[str, str] = {}


def mark_needs_reauth(cred: Credential, reason: str = "") -> bool:
    """Record that *this exact* account + credential generation was rejected.

    Generation-safe on purpose: a late failure from an old request must never mark a
    freshly reauthorized credential as broken, so a stale generation is dropped instead of
    recorded. Returns True when this call changed the state.
    """
    account = cred.account or ""
    with _reauth_lock:
        if cred.generation < _reauth.get(account, -1):
            return False                      # a newer credential already replaced this one
        if cred.generation == _reauth.get(account, -1):
            return False
        _reauth[account] = cred.generation
        _reauth_reason[account] = reason
        return True


def needs_reauth(cred: Credential) -> bool:
    """True when this exact credential generation is the one that was rejected."""
    with _reauth_lock:
        return _reauth.get(cred.account or "", -1) >= cred.generation


def reauth_reason(cred: Credential) -> str:
    with _reauth_lock:
        return _reauth_reason.get(cred.account or "", "")


def clear_reauth() -> None:
    """Called after a successful sign-in: the new generation is usable again."""
    with _reauth_lock:
        _reauth.clear()
        _reauth_reason.clear()


# ------------------------------------------------------------------ PKCE helpers


def b64url(raw: bytes) -> str:
    """base64url without padding, per RFC 7636."""
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def new_verifier() -> str:
    """Fresh high-entropy verifier. Cryptographic RNG, never a timestamp or a salt."""
    return b64url(secrets.token_bytes(32))


def challenge_for(verifier: str) -> str:
    return b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def new_state() -> str:
    return b64url(secrets.token_bytes(16))


def constant_time_equal(a: str, b: str) -> bool:
    """`state` is the only thing between our listener and a code somebody else dropped on it."""
    return secrets.compare_digest((a or "").encode(), (b or "").encode())


def build_authorize_url(base: str, callback_url: str, challenge: str, state: str,
                        app_name: str = APP_NAME, scope: str = SCOPE) -> str:
    """The consent URL. `S256` is always sent, for every flow.

    A redirect travels straight to the address we named, but the user may still choose
    "Show me a code" on the consent screen — that choice is theirs and no authorize
    parameter preselects or prevents it. Whenever a human can be handed the code, `plain`
    would put the verifier itself through browser history and request logs, so S256 goes
    out unconditionally.
    """
    query = urllib.parse.urlencode({
        "callback_url": callback_url,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "app_name": app_name,
        "scope": scope,
    })
    return f"{authorize_url(base)}?{query}"


# ------------------------------------------------------------------ loopback listener


class LoopbackCallback:
    """Flow A: a listener on 127.0.0.1, started BEFORE the browser opens.

    Binding first is what makes the port known, so the authorize URL cannot race the
    socket. The browser gets a page it can close; the code goes to `wait()`.
    """

    PAGE = ("<!doctype html><meta charset=utf-8><title>OrcaRouter</title>"
            "<body style='font:16px -apple-system,sans-serif;padding:2rem'>"
            "<h3>OrcaRouter</h3><p>%s</p><p>可以关闭此页面。</p>")

    def __init__(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self._lock = threading.Lock()
        self._result: tuple[str, str] | None = None   # (code, error)
        self._cancelled = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass                       # never write the query string anywhere

            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                if url.path != "/cb":
                    self.send_error(404)
                    return
                params = urllib.parse.parse_qs(url.query)
                code = (params.get("code") or [""])[0]
                error = (params.get("error") or [""])[0]
                state = (params.get("state") or [""])[0]
                ok = not error and bool(code)
                body = (outer.PAGE % ("已连接，可以关闭此页面。" if ok else "授权未完成。"))
                payload = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                with outer._lock:
                    if outer._result is None:
                        outer._result = (code, error, state)
                threading.Thread(target=outer._server.shutdown, daemon=True).start()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_port
        self.callback_url = f"http://127.0.0.1:{self.port}/cb"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def wait(self, state: str, timeout: float = AUTHORIZE_TIMEOUT_S) -> str:
        """Return the code, or raise. Compares `state` before anything else."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._cancelled.is_set():
                raise LoginCancelled("已取消登录。")
            with self._lock:
                result = self._result
            if result is not None:
                code, error, got_state = result
                if not constant_time_equal(got_state, state):
                    raise AuthRejected("授权回调的 state 不匹配，已放弃本次登录。")
                if error:
                    raise AuthRejected(_denial_message(error))
                if not code:
                    raise AuthRejected("授权回调没有携带授权码。")
                return code
            time.sleep(0.1)
        raise AuthRejected("等待浏览器授权超时，请重试。")

    def close(self) -> None:
        self._cancelled.set()
        try:
            self._server.shutdown()
        except Exception:
            pass
        try:
            self._server.server_close()
        except Exception:
            pass


def _denial_message(error: str) -> str:
    if error == "access_denied":
        return "你拒绝了本次授权。"
    return f"授权未通过（{error}）。"


# ------------------------------------------------------------------ exchange


def _post_json(url: str, payload: dict, timeout: float) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"content-type": "application/json", "accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(CATALOG_MAX_BYTES)
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise AuthFailed("授权服务返回了无法解析的响应。") from exc


def exchange_code(auth: str, code: str, verifier: str,
                  timeout: float = EXCHANGE_TIMEOUT_S) -> Credential:
    """Redeem the code for the durable key. The verifier goes out here and only here.

    The response's `scope` is what was **granted**, not what was asked for, so it is read
    back and reported rather than assumed. OrcaRouter answers `400` when the challenge
    method differs from the one sent at authorize time and `403` for an unknown, expired,
    already-used code or a verifier that does not match — both are terminal for this attempt.
    """
    if not code:
        raise AuthRejected("没有授权码。")
    try:
        data = _post_json(exchange_url(auth),
                          {"code": code, "code_verifier": verifier,
                           "code_challenge_method": "S256"}, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 400:
            raise AuthRejected("授权码的校验方式不被接受，请重试登录。") from exc
        if exc.code in (401, 403):
            raise AuthRejected("授权码无效、已过期或已被使用，请重新登录。") from exc
        if exc.code == 429:
            raise AuthRejected("登录过于频繁（每 24 小时最多签发 10 个密钥），"
                               "请稍后再试或直接填写已有密钥。") from exc
        raise AuthFailed(f"授权服务返回 HTTP {exc.code}。") from exc
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        raise AuthFailed("连接授权服务失败，请检查网络后重试。") from exc

    key = str(data.get("key") or "")
    if not key:
        raise AuthFailed("授权服务没有返回密钥。")
    scope = str(data.get("scope") or "")
    if scope and scope != SCOPE:
        # Granted less than asked for: say so instead of assuming we hold the wider grant.
        raise AuthRejected(f"授权范围是「{scope}」，不满足本应用需要的「{SCOPE}」。")
    return Credential(key=key, source="pkce",
                      account=str(data.get("user_id") or ""), scope=scope or SCOPE)


# ------------------------------------------------------------------ PKCE adapter


class LoginCancelled(OrcaRouterError):
    """The user (or the UI lifecycle) cancelled this attempt."""


class PkceSource(CredentialSource):
    """Adapter 2 — OAuth 2.0 + PKCE, then the same durable key as adapter 1.

    One attempt owns one verifier, one state and one listener. `cancel()` is safe to call
    from any thread at any time, and `pagehide`-style teardown is the caller's job: an
    AppKit window has no back-forward cache, so the native equivalents are window close and
    app terminate (see `OrcaLoginSession`).
    """

    name = "pkce"

    def __init__(self, base: str | None = None, flow: str = "loopback",
                 timeout: float = AUTHORIZE_TIMEOUT_S):
        self.base = base or auth_base()
        self.flow = flow
        self.timeout = timeout
        self.authorize_url = ""
        self.verifier = ""                 # never logged, never persisted
        self._state = ""
        self._cancel = threading.Event()
        self._listener: LoopbackCallback | None = None

    def available(self) -> bool:
        return True

    def cancel(self) -> None:
        self._cancel.set()
        if self._listener is not None:
            self._listener.close()

    def start(self) -> str:
        """Prepare the attempt and return the URL to open. No secret in the URL."""
        self.verifier = new_verifier()
        self._state = new_state()
        callback = "oob" if self.flow == "oob" else self._callback_url()
        self.authorize_url = build_authorize_url(
            self.base, callback, challenge_for(self.verifier), self._state)
        return self.authorize_url

    def _callback_url(self) -> str:
        self._listener = LoopbackCallback()
        return self._listener.callback_url

    def finish(self, code: str = "") -> Credential:
        """Wait for the code (Flow A) or take the pasted one (Flow B), then exchange."""
        if not self.verifier:
            raise OrcaRouterError("登录尚未开始。")
        try:
            if self.flow == "oob":
                code = (code or "").strip()
                if not code:
                    raise LoginCancelled("已取消登录。")
            else:
                if self._listener is None:
                    raise OrcaRouterError("登录尚未开始。")
                code = self._listener.wait(self._state, self.timeout)
            if self._cancel.is_set():
                raise LoginCancelled("已取消登录。")
            return exchange_code(self.base, code, self.verifier)
        finally:
            self.cancel()

    def acquire(self) -> Credential:
        self.start()
        return self.finish()


# ------------------------------------------------------------------ model catalog

# Verified fallback. Kept small on purpose: this exists so a fresh install can start when
# the catalog is slow or unreachable, not to be a second catalog. Live discovery is
# authoritative whenever it succeeds and these are never merged into a live result.
SEED_MODELS: tuple[dict, ...] = (
    {"id": "openai/gpt-5.5", "name": "OpenAI: GPT-5.5", "context_length": 400000,
     "input_modalities": ["text"], "reasoning": ["low", "medium", "high", "xhigh"]},
    {"id": "anthropic/claude-opus-4.8", "name": "Anthropic: Claude Opus 4.8",
     "context_length": 200000, "input_modalities": ["text"], "reasoning": []},
    {"id": "google/gemini-3.5-flash", "name": "Google: Gemini 3.5 Flash",
     "context_length": 1000000, "input_modalities": ["text", "image"], "reasoning": []},
    {"id": "deepseek/deepseek-v4-pro", "name": "DeepSeek: V4 Pro",
     "context_length": 1048576, "input_modalities": ["text"], "reasoning": []},
    {"id": "orcarouter/auto", "name": "OrcaRouter: Auto",
     "context_length": 0, "input_modalities": ["text"], "reasoning": []},
)
SEED_SOURCE = "verified-seed"

# The chat endpoint types this app can actually speak: it posts OpenAI chat-completions
# and (for the Anthropic-shaped group) /v1/messages. A route the client cannot speak must
# never reach the dropdown, so the accepted set is closed here rather than guessed.
CHAT_ENDPOINT_TYPES = ("openai", "anthropic", "gemini", "openai-response")
NON_TEXT_ENDPOINT_TYPES = ("image-generation", "openai-video", "jina-rerank",
                           "embeddings", "rerank")


@dataclass
class ModelInfo:
    id: str
    name: str = ""
    context_length: int = 0
    input_modalities: tuple[str, ...] = ("text",)
    reasoning: tuple[str, ...] = ()
    endpoint_types: tuple[str, ...] = ()
    source: str = SEED_SOURCE

    def supports_input(self, modality: str) -> bool:
        """Fail closed: a model that does not declare the modality is not offered for it."""
        if modality == "text":
            return True
        return modality in self.input_modalities


@dataclass
class Catalog:
    """One catalog answer plus where it came from — the UI must be able to say which."""

    models: list[ModelInfo] = field(default_factory=list)
    source: str = SEED_SOURCE          # SEED_SOURCE | "live"
    degraded: bool = False
    error: str = ""
    capability: str = "chat"

    def ids(self) -> list[str]:
        return [m.id for m in self.models]


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _modalities(record: dict) -> tuple[str, ...]:
    arch = record.get("architecture")
    if not isinstance(arch, dict):
        return ()
    values = arch.get("input_modalities")
    if not isinstance(values, list):
        return ()
    return tuple(str(v) for v in values if isinstance(v, str))


def parse_catalog(data, capability: str = "chat") -> list[ModelInfo]:
    """Parse `GET /models` into the shapes the capability filters need.

    Accepts only records that carry a usable id; everything else is dropped rather than
    guessed at. Item count is bounded by the caller.
    """
    records = data.get("data") if isinstance(data, dict) else data
    if not isinstance(records, list):
        raise ValueError("模型目录响应格式无法识别。")
    out: list[ModelInfo] = []
    for record in records[:CATALOG_MAX_ITEMS]:
        if not isinstance(record, dict):
            continue
        model_id = record.get("id")
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        types = record.get("supported_endpoint_types")
        types = tuple(str(t) for t in types if isinstance(t, str)) if isinstance(types, list) else ()
        reasoning = record.get("reasoning_efforts") or record.get("reasoning")
        reasoning = (tuple(str(r) for r in reasoning if isinstance(r, str))
                     if isinstance(reasoning, list) else ())
        out.append(ModelInfo(
            id=model_id.strip(),
            name=str(record.get("name") or ""),
            context_length=_int(record.get("context_length")),
            input_modalities=_modalities(record) or ("text",),
            reasoning=reasoning,
            endpoint_types=types,
            source="live",
        ))
    return out


def filter_models(models: list[ModelInfo], capability: str,
                  require_input: str = "") -> list[ModelInfo]:
    """The capability filter each AI entrance needs — one rule per capability.

    `chat` requires a chat-capable endpoint type and excludes the non-text-only routes
    (image generation, video, rerank). A multimodal entrance additionally requires the
    model to *declare* the modality it uploads, so an undeclared model fails closed
    instead of appearing in a dropdown it cannot serve.
    """
    if capability == "chat":
        kept = [m for m in models
                if not m.endpoint_types
                or (set(m.endpoint_types) & set(CHAT_ENDPOINT_TYPES)
                    and not set(m.endpoint_types) <= set(NON_TEXT_ENDPOINT_TYPES))]
    elif capability == "embedding":
        kept = [m for m in models if "embeddings" in m.endpoint_types]
    elif capability == "image":
        kept = [m for m in models if "image-generation" in m.endpoint_types]
    elif capability == "video":
        kept = [m for m in models if "openai-video" in m.endpoint_types]
    elif capability == "rerank":
        kept = [m for m in models if "jina-rerank" in m.endpoint_types]
    else:
        kept = []
    if require_input and require_input != "text":
        kept = [m for m in kept if m.supports_input(require_input)]
    return sorted(kept, key=lambda m: m.id)


class ModelCatalog:
    """Bounded live discovery with a verified fallback that is never mixed into it."""

    def __init__(self, cache_ttl: float = 300.0, timeout: float = CATALOG_TIMEOUT_S):
        self.cache_ttl = cache_ttl
        self.timeout = timeout
        self._lock = threading.Lock()
        self._cache: dict[tuple, tuple[float, list[ModelInfo]]] = {}

    def _cache_key(self, base: str, capability: str, key: str) -> tuple:
        # The credential generation is part of the key: a different key can see a
        # different model set, and one user's catalog must not be served to another's.
        return (base, capability, hashlib.sha256(key.encode()).hexdigest()[:16])

    def fetch(self, base: str, key: str, capability: str = "chat",
              require_input: str = "", refresh: bool = False) -> Catalog:
        """Live first, seed only on failure — and say which one the caller got."""
        if not key:
            return Catalog(models=self.seed(capability, require_input), source=SEED_SOURCE,
                           degraded=True, error="未配置密钥，无法获取模型列表。",
                           capability=capability)
        cache_key = self._cache_key(base, capability, key)
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(cache_key)
        if hit and not refresh and now - hit[0] < self.cache_ttl:
            models = filter_models(hit[1], capability, require_input)
            return Catalog(models=models, source="live", capability=capability)
        try:
            raw = self._get(base, key, capability)
            models = parse_catalog(raw, capability)
            if not models:
                raise ValueError("模型目录为空。")
            with self._lock:
                self._cache[cache_key] = (now, models)
            return Catalog(models=filter_models(models, capability, require_input),
                           source="live", capability=capability)
        except Exception as exc:                       # outage must not break the app
            reason = _catalog_error(exc)
            if hit:
                # Last known good beats the seed: it was real, and it is still labelled.
                return Catalog(models=filter_models(hit[1], capability, require_input),
                               source="live", degraded=True, error=reason,
                               capability=capability)
            return Catalog(models=self.seed(capability, require_input), source=SEED_SOURCE,
                           degraded=True, error=reason, capability=capability)

    @staticmethod
    def seed(capability: str, require_input: str = "") -> list[ModelInfo]:
        models = [ModelInfo(**m) for m in SEED_MODELS]
        return filter_models(models, capability, require_input)

    def _get(self, base: str, key: str, capability: str) -> dict:
        url = f"{base.rstrip('/')}/models?capability={urllib.parse.quote(capability)}"
        parts = urllib.parse.urlsplit(url)
        cls = (http.client.HTTPSConnection if parts.scheme == "https"
               else http.client.HTTPConnection)
        conn = cls(parts.hostname, parts.port, timeout=self.timeout)
        try:
            conn.request("GET", parts.path + ("?" + parts.query if parts.query else ""),
                         headers={"authorization": f"Bearer {key}",
                                  "accept": "application/json"})
            resp = conn.getresponse()
            if resp.status >= 300:
                raise urllib.error.HTTPError(url, resp.status, resp.reason,
                                             resp.headers, None)
            raw = resp.read(CATALOG_MAX_BYTES + 1)
        finally:
            conn.close()
        if len(raw) > CATALOG_MAX_BYTES:
            raise ValueError("模型目录响应过大。")
        return json.loads(raw)


def _catalog_error(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403):
            return f"HTTP {exc.code}：密钥无效或无权访问，请重新填写或登录。"
        if exc.code == 429:
            return "HTTP 429：请求过于频繁，请稍后刷新。"
        return f"HTTP {exc.code}：模型目录不可用。"
    if isinstance(exc, (urllib.error.URLError, OSError, http.client.HTTPException,
                        TimeoutError)):
        return "网络错误：模型目录不可用。"
    if isinstance(exc, ValueError):
        return str(exc)
    return "模型目录不可用。"


_CATALOG = ModelCatalog()


def catalog() -> ModelCatalog:
    return _CATALOG


def selector_options(cat: Catalog) -> list[str]:
    """What the model dropdown must contain for this catalog answer.

    One function so the GUI, the tests and any future surface bind to the same list: the
    options ARE the filtered catalog. There is deliberately no path from here to a
    free-text model name.
    """
    return cat.ids()


def selection_after_catalog(current: str, options: list[str]) -> tuple[str, bool]:
    """Resolve a previously selected model against freshly filtered options.

    Returns (value, invalidated). A model that is no longer offered — or no longer fits the
    capability the entrance requires — is cleared rather than silently kept, and the caller
    is told so it can prompt for a new choice.
    """
    if current and current not in options:
        return "", True
    return current, False


# ------------------------------------------------------------------ OpenAI transport


def chat_request(base: str, key: str, model: str, messages: list[dict],
                 max_tokens: int = 300, temperature: float = 0.9,
                 timeout: float = 30.0, extra: dict | None = None) -> dict:
    """One chat-completions call on the inference origin.

    Goes through `generate.http_post_json` so OrcaRouter shares the app's keep-alive pool
    with every other provider instead of opening a second connection strategy. `extra`
    carries provider-specific switches (the app's `OPENAI_EXTRA_BODY`); an unknown field
    is ignored by the gateway, which is why passing it through is safe here too.
    """
    import generate                                   # local: generate imports this module

    url = f"{base.rstrip('/')}/chat/completions"
    body = {"model": model, "max_tokens": max_tokens, "temperature": temperature,
            "messages": messages}
    if extra:
        body.update(extra)
    headers = {"content-type": "application/json", "authorization": f"Bearer {key}"}
    return generate.http_post_json(url, headers, body, timeout)


def models_endpoint(base: str) -> str:
    return f"{base.rstrip('/')}/models"
