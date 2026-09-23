"""Judge backed by TypeSafe's Jev / System One API.

Jev is a structured-decision model: you hand it a state plus a map of typed questions and
it returns calibrated probabilities — never text. That is exactly our judgment layer, so
this is a drop-in replacement for the local decider-2b:

    POST {base}/v1/systemone   (base spellings, same rule as the generation layer:
                                `…` -> `…/v1/systemone`; `…/v1` -> `…/v1/systemone`;
                                `…/v1/evaluate` -> used verbatim, gateways rename the
                                action — Vercel serves TypeSafe under /v1/evaluate)
    Authorization: Bearer <key>
    {"model": "jev-latest", "state": "...", "questions": {
        "intent": {"type": "choice", "instructions": "...", "criteria": {...}},
        "risk":   {"type": "score",  "instructions": "...", "criteria": [...]}}}

Native access is waitlisted; TypeSafe-compatible gateways (OpenRouter, Vercel AI Gateway,
Opper, LiteLLM) serve the same shape with their own keys, so `base` + `model` are both
configurable.

Config uses TypeSafe's own conventional names (src/userconfig.py):
    TYPESAFE_API_KEY    TypeSafe key (or a gateway key)
    TYPESAFE_BASE_URL   default https://api.typesafe.ai   (gateways: see README)
    TYPESAFE_MODEL      default jev-latest
"""

from __future__ import annotations

import json
import os
import time
import urllib.error

import userconfig
from generate import jev_request_url, http_post_json
from judge import ACTION_MAP, INTENTS, RISK_LEVELS

DEFAULT_BASE = "https://api.typesafe.ai"
DEFAULT_MODEL = "jev-latest"
TIMEOUT = 30


def jev_configured() -> bool:
    """True when a TypeSafe key is present — callers prefer Jev over the local model."""
    return bool(userconfig.get("TYPESAFE_API_KEY", "JEV_API_KEY"))


class JevJudge:
    """Same surface as judge.Judge: judge(message, context) -> dict."""

    name = "jev-api"

    def __init__(self, base: str | None = None, key: str | None = None,
                 model: str | None = None, timeout: int = TIMEOUT):
        self.base = (base or userconfig.get("TYPESAFE_BASE_URL") or DEFAULT_BASE).rstrip("/")
        self.key = key or userconfig.get("TYPESAFE_API_KEY", "JEV_API_KEY")
        self.model = model or userconfig.get("TYPESAFE_MODEL") or DEFAULT_MODEL
        self.timeout = timeout
        self._last_url = ""

    def judge(self, message: str, context: str | None = None) -> dict:
        state = f"{context}\n\n{message}" if context else message
        payload = {
            "model": self.model,
            "state": state,
            "questions": {
                "intent": {"type": "choice",
                           "instructions": "这句话的真实意图是什么？",
                           "criteria": INTENTS},
                "risk": {"type": "score",
                         "instructions": "如果直接回复这句话，风险有多大？",
                         "criteria": RISK_LEVELS},
            },
        }
        data = self._post(payload)
        answers = data.get("answers") or {}
        intent_ans = answers.get("intent") or {}
        risk_ans = answers.get("risk") or {}

        intent = intent_ans.get("choice") or "闲聊"
        if intent not in INTENTS:
            # gateways occasionally echo the index or a near-miss label
            for name in INTENTS:
                if name in str(intent):
                    intent = name
                    break
            else:
                intent = "闲聊"
        confidence = float(intent_ans.get("confidence") or 0.0)
        risk = risk_ans.get("score")
        risk = float(risk) if isinstance(risk, (int, float)) else 0.0

        return {
            "intent": intent,
            "confidence": confidence,
            "intent_probs": intent_ans.get("probabilities") or {},
            "risk": round(risk, 1),
            "risk_probs": risk_ans.get("probabilities") or {},
            "actions": ACTION_MAP.get(intent, []),
            "message": message,
            "backend": f"jev/{self.model}",
        }

    def rank_candidates(self, message: str, intent: str,
                        candidates: list[str], context: str | None = None) -> list[dict]:
        """Rank reply candidates — just another `choice` question with the texts as options."""
        if not candidates:
            return []
        payload = {
            "model": self.model,
            "state": f"{context + chr(10) if context else ''}收到的消息：{message}\n判断出的意图：{intent}",
            "questions": {"best": {"type": "choice",
                                   "instructions": "哪一条回复最合适？",
                                   "criteria": {c: None for c in candidates}}},
        }
        data = self._post(payload)
        ans = ((data.get("answers") or {}).get("best") or {})
        probs = ans.get("probabilities") or {}
        ranked = []
        for c in candidates:
            p = probs.get(c)
            if p is None:                      # gateway may echo the chosen label only
                p = ans.get("confidence", 0.0) if ans.get("choice") == c else 0.0
            ranked.append({"text": c, "prob": float(p)})
        ranked.sort(key=lambda r: -r["prob"])
        return ranked

    # ------------------------------------------------------------------ transport
    def _post(self, payload: dict) -> dict:
        # Single #42 composition rule (src/generate.py jev_request_url): shared with
        # the settings window's 测试连接, so a base that tests well cannot judge badly.
        url = jev_request_url(self.base)
        self._last_url = url
        # 与生成层共用 keep-alive 池（src/generate.py）：判断+排序各一次网络调用，
        # 每次省掉一条 TLS 握手
        return http_post_json(
            url,
            {"content-type": "application/json",
             "authorization": f"Bearer {self.key}"},
            payload, self.timeout)

    def warm(self) -> None:
        """Nothing to load — kept so the two backends share a surface."""
        return None


if __name__ == "__main__":
    import sys

    if not jev_configured():
        print("❌ 未配置 TypeSafe key。设置 TYPESAFE_API_KEY 后重试。")
        print(f"   端点: {userconfig.get('TYPESAFE_BASE_URL') or DEFAULT_BASE}")
        print(f"   模型: {userconfig.get('TYPESAFE_MODEL') or DEFAULT_MODEL}")
        raise SystemExit(1)

    j = JevJudge()
    msg = sys.argv[1] if len(sys.argv) > 1 else "这个需求你今天跟一下"
    t0 = time.perf_counter()
    try:
        out = j.judge(msg)
    except urllib.error.HTTPError as e:
        print(f"❌ HTTP {e.code} @ {j._last_url}\n   {e.read()[:300].decode(errors='replace')}")
        raise SystemExit(1)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"耗时 {time.perf_counter() - t0:.2f}s")
