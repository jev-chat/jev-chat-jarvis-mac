"""Candidate reply generation via a fast Chinese LLM API.

Why an API instead of a local model: a 3B local model costs ~6 GB of disk, ~2-3 s per
generation on MPS and writes noticeably worse Chinese than the hosted fast tier. The
judgment half stays local (decider-2b, ~0.5 s, no network) — only reply writing goes out.

Two API shapes are supported, because providers disagree:
    openai     POST {base}/v1/chat/completions   Authorization: Bearer   -> choices[0].message.content
    anthropic  POST {base}/v1/messages           x-api-key + version    -> content[].text
推 most providers (DeepSeek, 通义, Moonshot, SiliconFlow, Ollama, vLLM, OpenRouter) only
speak the OpenAI shape; 智谱 and a few gateways offer both. The shape is inferred.
one, otherwise it is inferred from the base URL (a path containing "anthropic" => anthropic).

Nothing is ever written back, and the key is never logged. Run
`uv run python src/generate.py --check` to see which source is in use (key masked).

Privacy: the boss's message text is sent to the provider. That is the one place this app
leaves the machine — swap in a local model if that matters more than reply quality.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import time
import urllib.error
import urllib.request

from pathlib import Path

import userconfig
import styles

DEFAULT_MODEL = "glm-4-flash"
# any Anthropic-compatible /v1/messages endpoint works; this one is a cheap, fast
# Chinese-native option and is what the project was tested against
DEFAULT_BASE = "https://open.bigmodel.cn/api/anthropic"
DEFAULT_OPENAI_BASE = "https://api.openai.com/v1"
DEFAULT_ANTHROPIC_BASE = "https://api.anthropic.com"
MISSING_HINT = ("未配置生成层 Key：候选回复需要它，判断/风险不需要。"
                "设置 OPENAI_API_KEY（或 ANTHROPIC_API_KEY）后重启，见 README 配置章节。")


class ThinkingOnlyError(Exception):
    """A reasoning model spent the whole max_tokens budget thinking and wrote no text.

    DeepSeek-style reasoning models return the chain of thought alongside the answer; with
    this app's small per-request budget (300 tokens) the thinking can consume everything
    and `content` arrives empty. That is a wrong-model problem, not a network one, so the
    error names the model and the fix — the panel would otherwise fold it into 「空结果」,
    which reads as "generation is broken" instead of "the model is misconfigured".
    """


# The message carried by ThinkingOnlyError. Fits the panel's err[:60] display budget for
# realistic model names (fixed part is 41 chars), so the suggestion survives truncation.
# {alt} is a non-thinking model the configured endpoint actually serves (see _call).
THINKING_ONLY_HINT = ("思考型 {model}：额度被思考耗尽，正文 0 条；"
                      "换非思考模型（如 {alt}）")

# One request per tone. {n} appears twice on purpose: the "exactly n lines" demand has to
# agree with the count asked for, or the model pads the answer with a line of its own.
#
# The boldness line is what gives a tone its edges. Without it both replies sit at the same
# safe distance and every tone reads a bit flat; with it the first is always something you
# could send as-is and the second is where the persona gets to breathe. Measured on the
# built-in tones: 卑微乙方's pair goes from two polite apologies to "收到收到…" plus
# "您息怒我马上跪着改完给您磕头了", and 贴吧老哥 picks up "我自己看了都想删号".
PROMPT_ONE = """刚收到一条微信消息，你要帮我回。

{context_line}消息：「{message}」
{intent_line}
请写 {n} 条回复候选，语气统一成下面这一种，但两条的胆量要有差别：
「{tone}」{instruction}

硬性要求：
- 前一条稳妥、可以直接发出去；后一条把这个语气做足，更皮、更夸张一点也行
- 每条不超过 30 个字，是微信里打字的语气，不要客套话、不要解释
- 只输出 {n} 行，每行一条，不要编号、不要引号、不要任何前后缀
- 不要写出语气名称（不要写「{tone}：」这类前缀），直接从回复内容开始"""


# The model is told not to label its lines, and usually complies — but "usually" is exactly
# why these exist. Seen for real: "轻松型：" (the intended echo), "轻松的回复：", "轻松版：",
# "轻松一点：", "**轻松型**：", and behind numbering ("1. 轻松型：" — the numbering is
# stripped first, leaving the bare label). So: a style word, then up to a few characters of
# filler that may not contain sentence punctuation, then the colon.
_STYLE_LABEL = re.compile(
    r"^[*_#\s]*(稳妥|轻松|简短|简洁)[^，。！？；、,.!?;：:]{0,5}[*_#\s]*[:：]\s*")
# One-character style words match only when the colon follows almost immediately: a loose
# filler here would eat a legitimate reply like "简单说：我先确认一下".
_STYLE_LABEL_SHORT = re.compile(r"^[*_#\s]*(简|稳|轻)\s*(型|洁)?[*_#\s]*[:：]\s*")
# wrapping quotes, straight or CJK — applied before the label strip, and again after it,
# because either order can expose the other ("「轻松型：xxx」")
_QUOTES = re.compile(r"""^["“”「『'‘]+|["”」』'’]+$""")


def _strip_style_label(s: str) -> str:
    s = _STYLE_LABEL.sub("", s)
    s = _STYLE_LABEL_SHORT.sub("", s)
    return styles.strip_label(s)


def _strip_quotes(s: str) -> str:
    return _QUOTES.sub("", s)


def _endpoint(base: str, api: str) -> str:
    """Compose the request URL, tolerating both base-URL conventions.

    Providers disagree about whether the version segment belongs to the base:
        https://api.deepseek.com             -> /v1/chat/completions
        https://api.deepseek.com/v1          -> /v1/chat/completions
        https://open.bigmodel.cn/api/paas/v4 -> /v4/chat/completions
    So: if the base already ends in a version segment, append only the path.
    """
    b = (base or "").rstrip("/")
    last = b.rsplit("/", 1)[-1].lower()
    has_version = bool(re.fullmatch(r"v\d+[a-z]*", last))
    if api == "anthropic":
        return b + ("/messages" if has_version else "/v1/messages")
    return b + ("/chat/completions" if has_version else "/v1/chat/completions")


def pick_api_format(base: str, configured: str | None) -> str:
    """Explicit setting wins; otherwise infer from the URL.

    A base path containing "anthropic" means the Anthropic shape. Every other endpoint is
    assumed OpenAI-shaped, which is what most providers and local servers expose.
    """
    if configured:
        c = configured.strip().lower()
        if c in ("openai", "anthropic"):
            return c
    return "anthropic" if "anthropic" in (base or "").lower() else "openai"


def load_credentials() -> tuple[str, str, str, str, str]:
    """Returns (base_url, api_key, model, source, api_format). Never raises.

    Names are the conventional ones (src/userconfig.py), so whatever you already export
    for other tools works here:
        OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL         the common case
        ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL / ANTHROPIC_MODEL
    The API shape is inferred from the endpoint (a path containing "anthropic" means the
    Anthropic /v1/messages format; everything else is assumed OpenAI-shaped).
    """
    oai = userconfig.provider("OPENAI")
    anth = userconfig.provider("ANTHROPIC")

    if oai["key"]:
        base = oai["base"] or DEFAULT_OPENAI_BASE
        return base, oai["key"], oai["model"] or DEFAULT_MODEL, oai["source"], pick_api_format(base, None)
    if anth["key"]:
        base = anth["base"] or DEFAULT_ANTHROPIC_BASE
        return base, anth["key"], anth["model"] or DEFAULT_MODEL, anth["source"], pick_api_format(base, None)

    base = oai["base"] or anth["base"] or DEFAULT_OPENAI_BASE
    model = oai["model"] or anth["model"] or DEFAULT_MODEL
    return base, "", model, "none", pick_api_format(base, None)


def credential_status() -> str:
    """Human-readable state for --check; the key itself is never printed."""
    base, key, model, source, api = load_credentials()
    shape = ("Anthropic 格式 /v1/messages" if api == "anthropic"
             else "OpenAI 格式 /v1/chat/completions")
    home = str(Path.home())
    if not key:
        return (f"❌ 未配置 API Key\n"
                f"   端点: {base}  ({shape})\n"
                f"   模型: {model}\n"
                f"   {MISSING_HINT}")
    return (f"✅ 凭据来源: {source.replace(home, '~')}\n"
            f"   端点: {base}\n"
            f"   接口: {shape}\n"
            f"   模型: {model}\n"
            f"   Key : {key[:6]}…{key[-4:]}  ({len(key)} chars)")


class Generator:
    def __init__(self, model: str | None = None, timeout: int = 30,
                 api: str | None = None):
        self.model_override = model
        self.api_override = api if api in ("openai", "anthropic") else None
        self.timeout = timeout
        self._creds: tuple[str, str, str] | None = None
        self._last_url = ""

    def _creds_or_load(self):
        if self._creds is None:
            base, key, model, _src, _api = load_credentials()
            self._creds = (base, key, self.model_override or model)
        return self._creds

    def _call(self, prompt: str, on_delta=None) -> str:
        """One completion. With `on_delta`, streams: each content fragment is passed to it
        as it arrives, and the full text is still returned at the end (so the caller can
        parse lines once, authoritatively, from the same string).

        Streaming is OpenAI-shape only (`stream: true` + SSE) — that is what the DeepSeek /
        SiliconFlow / vLLM tier speaks and where the latency win is. The Anthropic shape
        keeps its one-shot request: `on_delta` is silently ignored there. If a gateway
        accepts `stream: true` but answers with plain JSON anyway, the response is parsed
        the old way — streaming degrades, it does not fail.
        """
        base, key, model, _src, api = load_credentials()
        # the constructor's overrides win — without this the `model` argument was accepted
        # and silently ignored, so the request went out with whatever the config named
        if self.model_override:
            model = self.model_override
        if self.api_override:
            api = self.api_override
        # the "switch to this" example should be a model the configured endpoint actually
        # serves: deepseek-chat on OpenAI-shaped providers, this app's non-thinking default
        # (DEFAULT_MODEL) behind the Anthropic shape
        alt = "glm-4-flash" if api == "anthropic" else "deepseek-chat"
        if api == "anthropic":
            url = _endpoint(base, "anthropic")
            body = {"model": model, "max_tokens": 300, "temperature": 0.9,
                    "messages": [{"role": "user", "content": prompt}]}
            headers = {"content-type": "application/json", "x-api-key": key,
                       "anthropic-version": "2023-06-01"}
            data = self._post(url, headers, body)
            parts = data.get("content") or []
            raw = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
            if not raw.strip():
                # extended thinking returns its blocks next to the text blocks; text
                # missing while thinking is present means the budget died mid-thought
                thinking = "".join(p.get("thinking", "") for p in parts
                                   if isinstance(p, dict))
                if thinking.strip():
                    raise ThinkingOnlyError(THINKING_ONLY_HINT.format(model=model, alt=alt))
            return raw

        url = _endpoint(base, "openai")
        body = {"model": model, "max_tokens": 300, "temperature": 0.9,
                "messages": [{"role": "user", "content": prompt}]}
        headers = {"content-type": "application/json", "authorization": f"Bearer {key}"}
        if on_delta is not None:
            return self._stream_openai(url, headers, body, model, alt, on_delta)
        data = self._post(url, headers, body)
        return self._openai_json(data, model, alt)

    @staticmethod
    def _openai_json(data: dict, model: str, alt: str) -> str:
        """Parse a one-shot OpenAI-shape response; raises on the thinking-only case."""
        choices = data.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        content = msg.get("content") or ""
        if not content.strip():
            # reasoning lives in reasoning_content (DeepSeek, SiliconFlow) or reasoning
            # (OpenRouter); a string there with empty content is the same wrong-model case
            for field in ("reasoning_content", "reasoning"):
                v = msg.get(field)
                if isinstance(v, str) and v.strip():
                    raise ThinkingOnlyError(THINKING_ONLY_HINT.format(model=model, alt=alt))
        return content

    def _stream_openai(self, url: str, headers: dict, body: dict,
                       model: str, alt: str, on_delta) -> str:
        """SSE variant of the OpenAI-shape call; returns the full content text.

        The wire format is `data: {json}` lines ended by `data: [DONE]`, each carrying a
        `delta` with the next fragment. Reasoning models send their thinking through the
        same deltas (reasoning_content / reasoning) before any content, so a stream that
        ends with thinking and no text is the same wrong-model case as the one-shot path
        and raises the same error — nothing was shown yet, because nothing was emitted.
        """
        self._last_url = url
        req = urllib.request.Request(
            url, data=json.dumps({**body, "stream": True}).encode(), headers=headers)
        content: list[str] = []
        reasoning: list[str] = []
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            ctype = (r.headers.get("content-type") or "").lower()
            if "event-stream" not in ctype:
                # the gateway took `stream: true` but answered with one JSON document:
                # parse it the ordinary way instead of failing
                return self._openai_json(json.load(r), model, alt)
            for raw_line in r:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue        # blank separators, "event:" lines, ": keep-alive"
                payload = line[len("data:"):].strip()
                if payload == "[DONE]":
                    break
                try:
                    evt = json.loads(payload)
                except ValueError:
                    continue        # a malformed keepalive must not kill the stream
                for ch in evt.get("choices") or []:
                    delta = ch.get("delta") or {}
                    frag = delta.get("content") or ""
                    if frag:
                        content.append(frag)
                        on_delta(frag)
                    for field in ("reasoning_content", "reasoning"):
                        v = delta.get(field)
                        if isinstance(v, str) and v:
                            reasoning.append(v)
        raw = "".join(content)
        if not raw.strip() and "".join(reasoning).strip():
            raise ThinkingOnlyError(THINKING_ONLY_HINT.format(model=model, alt=alt))
        return raw

    def _post(self, url: str, headers: dict, body: dict) -> dict:
        self._last_url = url
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.load(r)

    @staticmethod
    def _parse(raw: str) -> list[str]:
        out = []
        for line in raw.splitlines():
            s = line.strip()
            if not s:
                continue
            s = re.sub(r"^[\d]+[.、)．]\s*", "", s)   # "1." / "2、" numbering
            s = _strip_quotes(s)
            s = _strip_style_label(s)
            s = _strip_quotes(s)                     # quotes the label removal exposed
            if s:
                out.append(s.strip())
        return out

    def _one_tone(self, message: str, intent: str, tone: str,
                  context: str | None = None,
                  on_line=None) -> tuple[list[str], str]:
        """One request for one tone. Returns (texts, error); never raises.

        With `on_line`, each finished line is handed over the moment it completes so the
        panel can show it before the request ends — the final `texts` stay the one
        authoritative parse of the whole reply, and the callback is only the early look.
        """
        # The recent turns go in with their speakers ("王总: …"), because a reply that fits
        # the last two sentences is usually not a reply to this one sentence in isolation.
        context_line = f"最近的对话：\n{context}\n\n" if context else ""
        intent_line = f"判断出的意图：{intent}\n" if intent else ""
        prompt = PROMPT_ONE.format(message=message, context_line=context_line,
                                   intent_line=intent_line,
                                   n=styles.PER_TONE, tone=tone,
                                   instruction=styles.PRESETS[tone])
        emitted = 0
        buf = ""                 # fragments since the last newline

        def on_delta(frag: str) -> None:
            nonlocal buf, emitted
            buf += frag
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                for text in self._parse(line):
                    if emitted < styles.PER_TONE:
                        emitted += 1
                        on_line(text)

        try:
            raw = self._call(prompt, on_delta if on_line is not None else None)
        except ThinkingOnlyError as e:
            return [], str(e)            # already panel-ready: model named, fix suggested
        except urllib.error.HTTPError as e:
            detail = e.read()[:160].decode(errors="replace")
            return [], f"HTTP {e.code} @ {self._last_url} — {detail}"
        except Exception as e:
            return [], f"{type(e).__name__}: {e}"
        if on_line is not None:
            # Sync the callback with the authoritative parse. Two ways lines can be
            # missing from what the stream emitted: the model often stops without a
            # trailing newline (the last line sits in `buf`), and a gateway that fell
            # back to one-shot JSON streams nothing at all. Either way the remaining
            # lines go out here, so the panel shows them at this request's end rather
            # than waiting for ranking.
            for text in self._parse(raw)[emitted:styles.PER_TONE]:
                emitted += 1
                on_line(text)
        return self._parse(raw)[:styles.PER_TONE], ""

    def generate(self, message: str, intent: str = "",
                 slot_tones: list[str] | None = None,
                 context: str | None = None,
                 on_candidate=None) -> dict:
        """One concurrent request per selected 话术; returns the candidates grouped by tone.

        A tone gets its own request rather than one request listing every tone: asking a
        single call for "2 in this voice and 2 in that voice" makes the voices bleed into
        each other, and it makes the response harder to split back into groups. Three
        requests in flight together cost about as long as the slowest one.

        `slot_tones` is the panel's per-slot selection (styles.NONE_LABEL marks an unused
        slot). Two slots holding the same tone is allowed and simply runs it twice.

        `on_candidate(slot, tone, text)` fires from the worker threads the moment a line
        completes — streaming's early look, before the full result is in. Callers that do
        not pass it get exactly the old collect-then-return behaviour.
        """
        slots = list(slot_tones or (styles.DEFAULT_SLOTS + [styles.NONE_LABEL]))
        active = [(i, t) for i, t in enumerate(slots) if t in styles.PRESETS]
        if not active:
            return {"groups": [], "error": "没有选择任何话术", "elapsed_s": 0.0}
        if not self._creds_or_load()[1]:
            return {"groups": [], "error": MISSING_HINT, "elapsed_s": 0.0}

        t0 = time.perf_counter()
        groups: list[dict] = []

        def run(i: int, tone: str):
            # slot index rides along so the panel knows where the line belongs
            def on_line(text: str) -> None:
                on_candidate(i, tone, text)
            return self._one_tone(message, intent, tone, context,
                                  on_line if on_candidate is not None else None)

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(active)) as ex:
            futures = {i: ex.submit(run, i, tone) for i, tone in active}
            for i, tone in active:          # read in slot order, not completion order
                try:
                    texts, err = futures[i].result()
                except Exception as e:      # defensive: _one_tone swallows its own errors
                    texts, err = [], f"{type(e).__name__}: {e}"
                groups.append({"slot": i, "tone": tone, "texts": texts, "error": err})

        _base, _key, model = self._creds_or_load()
        # when nothing came back from any tone, the per-group reasons are the only
        # diagnosis there is — lift them to the top level so the panel shows e.g.
        # "思考型 deepseek-v4-pro：…" instead of hud's generic 「空结果」 fallback
        error = ""
        if not any(g["texts"] for g in groups):
            seen: list[str] = []
            for g in groups:
                e = (g.get("error") or "").strip()
                if e and e not in seen:      # same wrong model -> same hint N times
                    seen.append(e)
            error = " · ".join(seen)
        return {"groups": groups, "model": model, "error": error,
                "elapsed_s": time.perf_counter() - t0}


if __name__ == "__main__":
    import sys

    if "--check" in sys.argv:
        print(credential_status())
        raise SystemExit(0 if load_credentials()[1] else 1)

    g = Generator()
    msg = sys.argv[1] if len(sys.argv) > 1 else "这个需求你今天跟一下"
    intent = sys.argv[2] if len(sys.argv) > 2 else "派活"
    print(json.dumps(g.generate(msg, intent), ensure_ascii=False, indent=1))
