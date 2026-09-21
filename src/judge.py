"""Judge: local Jev-shaped model reads a Chinese message and returns intent + risk.

One forward pass answers both slots (decider's documented multi-question layout:
append further `Question k: ... Answer k: (` blocks and read logits at each slot).

Measured on 22 real Chinese workplace messages, zero-shot: 86% intent accuracy
against a 13.6% majority baseline.
"""

from __future__ import annotations

import threading

import numpy as np

INTENTS = {
    "派活": "对方要我做一件事或接一个任务",
    "催进度": "对方在催促我尽快完成某个已在办的事",
    "问进度": "对方在询问某件事的进展或状态",
    "批评": "对方对我的工作或结果表达不满、指出错误",
    "要解释": "对方要求我说明原因或给出解释",
    "闲聊": "对方只是在聊天、分享或表达感受，没有具体要求",
    "约会议": "对方想安排一次会议或通话",
    "夸奖": "对方在肯定、称赞我的成果",
}

RISK_LEVELS = [
    "完全没风险，怎么回都行",
    "基本没风险",
    "平淡，正常回就好",
    "需要稍微留神",
    "有点敏感，措辞注意",
    "需要谨慎，可能被挑刺",
    "比较危险，容易得罪人或踩坑",
    "很危险，说错要出问题",
    "非常危险，涉及责任或利益",
    "极度危险，先别回，想清楚再说",
]

# V0: actions are a static derivation, no generation involved
ACTION_MAP = {
    "派活": ["接住", "问清交付标准和期限", "先给个时间点"],
    "催进度": ["先给当前状态", "给明确的完成时间", "别解释太多"],
    "问进度": ["直接说事实", "给下个节点", "有卡点就说卡点"],
    "批评": ["先认下来", "别急着辩解", "给补救方案"],
    "要解释": ["说清原因", "别找借口", "给改进措施"],
    "闲聊": ["轻松回应", "可以互动", "不用当真"],
    "约会议": ["确认时间", "说清议程", "准备好材料"],
    "夸奖": ["接住并感谢", "别过度谦虚", "可以顺带提下一步"],
}


class Judge:
    """Wraps a decoder-only decision model; lazy-loads on first use."""

    def __init__(self, repo: str = "Mapika/decider-2b", device: str | None = None):
        import torch

        self.torch = torch
        if device is None:
            device = "mps" if torch.backends.mps.is_available() else "cpu"
        self.device = device
        self.repo = repo
        self.temperature = 1.3
        self._loaded = False
        # RLock, not Lock: warm() holds it across the whole dummy forward, and judge()
        # inside that same call re-enters _load(). One lock guards both the load and the
        # first forward, so a warm-up and a real judgment can never run a forward at the
        # same time — they queue up instead.
        self._load_lock = threading.RLock()

    def _load(self):
        if self._loaded:
            return
        # Double-checked: the warm-up thread and the first real message can both get here
        # at once, and two concurrent from_pretrained calls would load the model twice.
        # The loser of the race just waits on the lock until the winner is done.
        with self._load_lock:
            if self._loaded:
                return
            from transformers import AutoModelForCausalLM, AutoTokenizer

            t = self.torch
            self.tok = AutoTokenizer.from_pretrained(self.repo)
            # float16, not bfloat16: MPS takes the slow path for bf16 (limited op coverage) and
            # it costs exactly 2x here — measured on this model, same prompt, three runs each:
            # bf16 1352/1393/1467 ms vs fp16 734/745/827 ms. The judge is the single biggest
            # steady-state cost in the pipeline, so this is the difference between a ~3 s and a
            # ~4 s reply. CPU has no fp16 win, so it stays fp32.
            dtype = t.float16 if self.device == "mps" else t.float32
            self.model = AutoModelForCausalLM.from_pretrained(self.repo, dtype=dtype).to(self.device).eval()
            self._letters = [self.tok.encode(c, add_special_tokens=False)[0]
                             for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]
            self._loaded = True

    def warm(self) -> None:
        """Load the model and run one real-shaped forward, so no real message pays for it.

        decider-2b's first load costs 9-15 s and lands inside whichever judge() call gets
        there first — the HUD starts this in the background right after launch, so that
        call is ours, not the user's first message. The whole thing runs under the load
        lock: if a real message arrives mid-warm-up, its judge() blocks here until the
        warm-up is done, then runs at steady state.
        """
        with self._load_lock:
            self._load()
            self.judge("预热")

    def _slot_probs(self, logits_by_slot: list, n_options: int, slot: int) -> np.ndarray:
        logits = logits_by_slot[slot]
        ids = self._letters[:n_options]
        probs = self.torch.softmax(logits[ids].float() / self.temperature, -1)
        return probs.cpu().numpy()

    def _forward(self, prompt: str, n_slots: int):
        """One forward pass; returns (logits, [token index per 'Answer: (' slot]).

        logits[i] is the distribution for position i+1, so reading at the token that
        contains "(" gives the letter distribution for that slot.
        """
        import re

        ids = self.tok(prompt, return_tensors="pt", return_offsets_mapping=True).to(self.device)
        offsets = ids.pop("offset_mapping")[0].tolist()
        with self.torch.no_grad():
            out = self.model(**ids)
        slot_token_idx = []
        for m in re.finditer(r"Answer: \(", prompt):
            char_pos = m.start() + len("Answer: ")
            for i, (s, e) in enumerate(offsets):
                if s <= char_pos < e:
                    slot_token_idx.append(i)
                    break
        if len(slot_token_idx) < n_slots:
            raise RuntimeError(f"expected {n_slots} answer slots, found {len(slot_token_idx)}")
        return out.logits[0], slot_token_idx

    def rank_candidates(self, message: str, intent: str,
                        candidates: list[str]) -> list[dict]:
        """Rank reply candidates by asking which one fits best.

        The candidates are the options, so one forward pass yields the distribution the
        phone demo shows as 89% / 9% / 2%.
        """
        self._load()
        letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        prompt = f"Context:\n收到：「{message}」\n判断出的意图：{intent}\n\n"
        prompt += "Question: 哪一条回复最合适？\nOptions:\n"
        for i, c in enumerate(candidates):
            prompt += f"({letters[i]}) {c}\n"
        prompt += "Answer: ("

        logits, slots = self._forward(prompt, 1)
        probs = self._slot_probs([logits[slots[0]]], len(candidates), 0)
        ranked = sorted(
            ({"text": c, "prob": float(p)} for c, p in zip(candidates, probs)),
            key=lambda r: -r["prob"])
        return ranked

    def judge(self, message: str, context: str | None = None) -> dict:
        self._load()
        intents = list(INTENTS)
        letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

        prompt = f"Context:\n{context + chr(10) + chr(10) if context else ''}{message}\n\n"
        # slot 0: intent
        prompt += "Question: 这句话的真实意图是什么？\nOptions:\n"
        for i, name in enumerate(intents):
            prompt += f"({letters[i]}) {name} - {INTENTS[name]}\n"
        prompt += "Answer: ("
        # slot 1: risk
        prompt += "\n\nQuestion: 如果直接回复这句话，风险有多大？\nOptions:\n"
        for i, lv in enumerate(RISK_LEVELS):
            prompt += f"({letters[i]}) {lv}\n"
        prompt += "Answer: ("

        logits, slot_token_idx = self._forward(prompt, 2)

        intent_probs = self._slot_probs([logits[slot_token_idx[0]]], len(intents), 0)
        risk_probs = self._slot_probs([logits[slot_token_idx[1]]], len(RISK_LEVELS), 0)

        intent_idx = int(np.argmax(intent_probs))
        risk_value = float((np.arange(len(RISK_LEVELS)) * risk_probs).sum())

        return {
            "intent": intents[intent_idx],
            "confidence": float(intent_probs[intent_idx]),
            "intent_probs": {n: float(p) for n, p in zip(intents, intent_probs)},
            "risk": round(risk_value, 1),
            "risk_probs": {str(i): float(p) for i, p in enumerate(risk_probs)},
            "actions": ACTION_MAP.get(intents[intent_idx], []),
            "message": message,
        }


if __name__ == "__main__":
    import json
    import sys

    j = Judge()
    msg = sys.argv[1] if len(sys.argv) > 1 else "这个需求你今天跟一下"
    print(json.dumps(j.judge(msg), ensure_ascii=False, indent=1))


class FallbackJudge:
    """Prefer the official Jev API; drop to the local model if it fails.

    A judgment layer that dies because a key expired or a gateway hiccuped would take the
    whole panel down, so the first failure switches permanently to the local model and the
    verdict carries which backend produced it.
    """

    def __init__(self):
        import judge_jev
        self.primary = judge_jev.JevJudge()
        self.local = None
        self.fell_back = False
        self.reason = ""

    def _fallback(self):
        if self.local is None:
            self.local = Judge()
        return self.local

    def judge(self, message: str, context: str | None = None) -> dict:
        if not self.fell_back:
            try:
                return self.primary.judge(message, context)
            except Exception as e:
                self.fell_back = True
                self.reason = f"{type(e).__name__}: {str(e)[:80]}"
        out = self._fallback().judge(message, context)
        out["backend"] = f"local (Jev 不可用: {self.reason})"
        return out

    def rank_candidates(self, message: str, intent: str, candidates: list[str]) -> list[dict]:
        if not self.fell_back:
            try:
                return self.primary.rank_candidates(message, intent, candidates)
            except Exception as e:
                self.fell_back = True
                self.reason = f"{type(e).__name__}: {str(e)[:80]}"
        return self._fallback().rank_candidates(message, intent, candidates)

    def warm(self) -> None:
        return None


def make_judge():
    """Jev when a key is configured, otherwise the local decider-2b."""
    try:
        import judge_jev
        if judge_jev.jev_configured():
            return FallbackJudge()
    except Exception:
        pass
    return Judge()
