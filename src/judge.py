"""Judge: local Jev-shaped model reads a Chinese message and returns intent + risk.

One forward pass answers both slots (decider's documented multi-question layout:
append further `Question k: ... Answer k: (` blocks and read logits at each slot).

Measured on 22 real Chinese workplace messages, zero-shot: 86% intent accuracy
against a 13.6% majority baseline.
"""

from __future__ import annotations

import os
import subprocess
import threading

import numpy as np

import userconfig

# 描述保持这个长度是有实测依据的，别为了省 prefill 时间去瘦身：两轮压缩措辞
# （保语义锚点、每条砍 ~1/3 字符）在 22 条回归上分别是 81.8% 和 77.3%，都低于
# 原文的 86.4%——批评/要解释 的边界对措辞极敏感。省下的 ~100 ms 判断又藏在
# 停稳窗口里基本不可见，不划算（2026-09 实测，judge_zh_test.py 已改为直接
# import 这份 INTENTS，改这里必须重跑回归）。
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


class LowMemoryError(RuntimeError):
    """The memory guard refused to load the local model (see low_memory_reason)."""


class ModelNotDownloadedError(RuntimeError):
    """The local model is not cached and the user has not opted into downloading (#38).

    The 7 GB download used to start silently on first launch — or, worse, mid-session
    when the cloud judge hiccuped and FallbackJudge reached for the local model. The
    refusal text is written for the user and names both ways out; callers display it
    verbatim (see hud._run_analysis / _warm).
    """


def model_cache_dir(repo: str = "Mapika/decider-2b") -> str:
    """The model's HF cache root, following huggingface_hub's resolution order
    (HF_HUB_CACHE > HF_HOME/hub > ~/.cache/huggingface/hub) without importing the hub:
    judge stays import-light so the CLI self-tests keep working on machines without
    transformers. Also the directory the settings window deletes (#38).
    """
    base = os.environ.get("HF_HUB_CACHE")
    if not base:
        home = os.environ.get("HF_HOME")
        base = os.path.join(home, "hub") if home else os.path.expanduser(
            "~/.cache/huggingface/hub")
    return os.path.join(base, "models--" + repo.replace("/", "--"))


def model_cached(repo: str = "Mapika/decider-2b") -> bool:
    """True when the HF cache already holds the model weights — never touches the network.

    A snapshot counts only when it actually contains weights: isfile() resolves the
    snapshot's symlinks into blobs/, so an interrupted or partially-cleaned download
    (dangling weight links, blob-less snapshot) reads as not cached — otherwise the gate
    would let from_pretrained silently re-download all ~7 GB (#38).
    """
    snapshots = os.path.join(model_cache_dir(repo), "snapshots")
    try:
        for entry in os.listdir(snapshots):
            path = os.path.join(snapshots, entry)
            if not os.path.isdir(path):
                continue
            try:
                if any(f.endswith((".safetensors", ".bin"))
                       and os.path.isfile(os.path.join(path, f))
                       for f in os.listdir(path)):
                    return True
            except OSError:
                continue
    except OSError:
        pass
    return False


def model_disk_usage(repo: str = "Mapika/decider-2b") -> int:
    """Bytes the cached model actually occupies on disk, 0 when absent.

    Every file in the model directory is resolved to its real file first, then counted
    once (dedup by realpath): snapshot entries are symlinks into the blob store, and the
    Xet cache layout keeps the real blobs OUTSIDE the model directory (hub/blobs/<2-char
    prefix>/), so walking the model dir alone reads 0 GB for a fully downloaded model.
    Dedup is also what keeps symlink-following from double-counting — the failure mode
    of `du -L`, which reports ~2x. Dangling links (interrupted downloads) stat-fail and
    are skipped. Settings shows this number.
    """
    root = model_cache_dir(repo)
    seen: set[str] = set()
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            try:
                real = os.path.realpath(path)
                if real in seen:
                    continue
                size = os.stat(path).st_size   # follows symlinks; dangling -> OSError
            except OSError:
                continue
            seen.add(real)
            total += size
    return total


def download_block_reason(repo: str = "Mapika/decider-2b") -> str | None:
    """Why the local model must not be (down)loaded right now, or None when allowed.

    JUDGE_BACKEND is the user's choice from the first-run dialog: `local` opts into the
    download explicitly, `cloud` rules the local model out entirely, `skip` postponed the
    choice, and unset/auto keeps the historical behaviour for machines that already have
    the model while refusing to start a surprise download for the rest (#38).
    """
    pref = userconfig.get("JUDGE_BACKEND").strip().lower()
    if pref == "local":
        return None                      # explicit opt-in: download is the point
    if pref == "cloud":
        return ("已选择在线判断（JUDGE_BACKEND=cloud），本地模型未使用 · "
                "如需离线判断，可在模型设置的「判断 · Jev」页启用")
    if model_cached(repo):
        return None                      # auto/skip: keep the historical behaviour
    return ("离线判断模型尚未下载（约 7 GB）· 可配置 TYPESAFE_API_KEY 走云端判断，"
            "或在模型设置的「判断 · Jev」页启用离线模型")


# decider-2b at fp16 is ~4.4 GB of weights, and the load path peaks well above that
# (transformers materializes the weights before the .to(device) copy). #37 reports a
# 16 GB M4 killed by memory pressure right after the first load. Both thresholds are
# derived from the weight size, not measured across machines — tune on more hardware
# before tightening further. Failing open (probe error -> None) is deliberate: the
# guard may only refuse what it can actually see.
MIN_TOTAL_BYTES = 12 * 2**30


def _memory_pressure_level() -> int | None:
    """jetsam's own pressure level (1 normal / 2 warning / 3 critical), None if unknown.

    kern.memorystatus_vm_pressure_level is the exact signal macOS kills on, so it is a
    closer proxy for "will this load get us killed" than free-page arithmetic — macOS
    reclaims aggressively before those numbers look alarming. One sysctl subprocess on
    a background warm-up path is irrelevant; judge() only pays it while still unloaded.
    """
    try:
        out = subprocess.run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
                             capture_output=True, text=True, timeout=2).stdout.strip()
        return int(out)
    except Exception:
        return None


def low_memory_reason() -> str | None:
    """Why loading the local model should be skipped, or None when it looks safe.

    Checked at the top of every _load() attempt, so a guard that fired once gets
    re-probed on the next message — memory freed up in the meantime lets the local
    model come back without a restart.
    """
    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError):
        total = 0
    if total and total < MIN_TOTAL_BYTES:
        return (f"系统总内存不足（{total / 2**30:.0f}GB），加载本地判断模型可能被系统终止 · "
                "可配置 TYPESAFE_API_KEY 走云端判断")
    level = _memory_pressure_level()
    if level is not None and level >= 2:
        return ("系统内存压力已处于警告档，加载本地判断模型可能被系统终止 · "
                "可配置 TYPESAFE_API_KEY 走云端判断")
    return None


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
            # Refuse before spending ~5 GB on a machine that cannot hold it (#37): a
            # load killed mid-flight by jetsam takes the whole app down with no Python
            # exception to catch, so this check must happen BEFORE from_pretrained.
            reason = low_memory_reason()
            if reason:
                raise LowMemoryError(reason)
            # Refuse before silently downloading ~7 GB (#38): the first-run dialog or the
            # settings window is where that decision belongs. Same placement — the check
            # must happen BEFORE from_pretrained, which is what downloads.
            reason = download_block_reason(self.repo)
            if reason:
                raise ModelNotDownloadedError(reason)
            from transformers import AutoModelForCausalLM, AutoTokenizer

            t = self.torch
            self.tok = AutoTokenizer.from_pretrained(self.repo)
            # float16, not bfloat16: MPS takes the slow path for bf16 (limited op coverage) and
            # it costs exactly 2x here — measured on this model, same prompt, three runs each:
            # bf16 1352/1393/1467 ms vs fp16 734/745/827 ms. The judge is the single biggest
            # steady-state cost in the pipeline, so this is the difference between a ~3 s and a
            # ~4 s reply. CPU has no fp16 win, so it stays fp32.
            dtype = t.float16 if self.device == "mps" else t.float32
            # low_cpu_mem_usage: stream weights layer-by-layer via the meta device instead
            # of materializing a full CPU copy first — it flattens the load-time memory
            # peak, which is exactly what killed #37's machine. Load gets a bit slower;
            # steady-state inference is untouched.
            self.model = AutoModelForCausalLM.from_pretrained(
                self.repo, dtype=dtype, low_cpu_mem_usage=True).to(self.device).eval()
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
