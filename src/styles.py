"""Built-in 话术 presets — one label + one instruction per tone.

These are what the generation prompt asks the model to write in; the user picks up to
MAX_SLOTS of them from the HUD. Keeping them here (rather than inline in the prompt) means
one place to add a tone, and lets the parser strip any label the model echoes back.

Each `prompt` is written as a direct instruction to the model: say what the tone IS, and
what it must not become. Vague one-word tones ("幽默") produce generic replies — the
useful part is the constraint.
"""

from __future__ import annotations

import re

import userconfig

# How many candidates one generation call can produce. The HUD shows exactly this many
# dropdowns; the panel's candidate area is built for this many rows.
MAX_SLOTS = 3

# Candidates per tone. Each tone gets its own request (they run concurrently), and the
# replies in one response are the same voice at different levels of nerve. The HUD builds
# the maximum number of rows once, then exposes only the live configured count.
DEFAULT_PER_TONE = 2
MIN_PER_TONE = 1
MAX_PER_TONE = 5


def candidate_count(raw: str | None) -> int:
    """Return a safe candidate count, falling back for malformed startup config."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_PER_TONE
    return value if MIN_PER_TONE <= value <= MAX_PER_TONE else DEFAULT_PER_TONE


def validate_candidate_count(raw: str) -> int:
    """Validate a value written by the settings UI, with a user-facing error."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ValueError("每种话术候选数必须是 1 到 5 的整数") from None
    if not MIN_PER_TONE <= value <= MAX_PER_TONE:
        raise ValueError("每种话术候选数必须是 1 到 5 的整数")
    return value


PER_TONE = MAX_PER_TONE

# label -> instruction. Order here is the order shown in the dropdowns.
#
# Each entry is written as a *persona plus its verbal tics*, not as a description of a mood.
# "语气放松、带一点幽默" gives the model nothing to hold on to and every tone drifts toward
# the same bland helpfulness; naming who is talking and which words they reach for is what
# actually separates the voices. The trailing constraint matters as much as the rest: a tone
# with no ceiling slides back into generic politeness by the second line.
BUILTIN: dict[str, str] = {
    "高情商话术": (
        "像公司里那个谁都说好的老同事：先接住对方情绪（「我理解」「确实」），再说事实和下一步，"
        "拒绝也带替代方案加一个具体时间点。不说教、不绕圈子、句尾不堆「呢/哦/啦」。"
    ),
    "自然沟通": (
        "像本人在认真回微信：先回应对方说的具体情况，需要时自然带出自己的感受或需要，"
        "再给清楚、可执行的回应或请求。参考非暴力沟通，但不机械套「观察、感受、需要、请求」四步。"
        "保留口语、个人习惯和真实的不确定，不编造事实，不说教，不端着。"
        "去掉 AI 腔：不用「不是…而是…」、「一句话讲清楚」、自问自答、强行排比、空洞总结、夸张比喻、"
        "书面套话、装饰性引号或破折号。句子长短自然，每句都要有新信息。"
    ),
    "贴吧老哥 v1.0": (
        "贴吧老哥：一口网感口语，「有一说一」「绷不住了」「搁这」「这就去整」随手就来，"
        "自称我、管对方叫「哥/兄弟」，可以自嘲玩梗甚至摆烂，但不骂人。"
        "禁止「您好」「感谢」这类书面客套。"
    ),
    "拒绝加班": (
        "态度平和但把话说死：明确今天做不完，**不给**「我尽量」「看情况」这种会被继续压的口子；"
        "必须给一个具体替代时间（比如「明早九点前」），并说清不用等今晚。"
        "道歉不超过一句，理由不超过一句。"
    ),
    "卑微乙方": (
        "极度卑微的乙方：「好的好的」「收到收到」「实在抱歉」「麻烦您了」张口就来，全程称「您」，"
        "任何问题先认在自己头上，随叫随到。夸张到一眼看出是梗，但整句仍然能直接发出去。"
    ),
    "稳如老狗": (
        "十年老工程师那种稳：不解释、不铺垫、不道歉，只给结论加一个时间点，句子短、"
        "主语是事不是情绪（「三点前给你」「已确认，没问题」），让对方觉得事情已经稳了。"
    ),
    "已读乱回": (
        "敷衍但不失礼：一到六个字把对方接住（「在忙，你说」「嗯嗯」「好」），"
        "不承诺、不展开、不给时间点，让对方觉得回了又没法接着追问。"
    ),
    "鱼塘主": (
        "海王海后式回消息：我是塘主，对方只是鱼塘里的一条鱼。先推后拉——先淡淡降一句、"
        "再轻轻给个甜头；惜字如金，不解释、不道歉、不讨好；事情不说死、留点悬念，"
        "收尾自带先撤感（「先这样」）。嘴甜心硬，不主动不拒绝不负责——不揽活、不否认、不背锅。"
        "分寸在高冷从容，不油腻、不暧昧，不是撩。"
    ),
    "职场黑话": (
        "把简单的事说得很专业：对齐、抓手、闭环、颗粒度、拉通、复盘、赋能、沉淀、打法轮着用，"
        "一句话里至少两个；但整句要能看懂，不要堆到不知所云。"
    ),
    "阴阳怪气": (
        "表面客气、话里带刺：多用「哦」「呢」「那就」「辛苦你了」配反问或夸张的客气，"
        "让对方不好发作又不能说你没礼貌。不要升级成直接骂人或人身攻击。"
    ),
    "理科直男": (
        "只回答被问到的：零寒暄、零情绪、零修饰、零表情，能两个字说清就不用五个字，"
        "像一个不太会说话但很靠谱的工程师。不做任何延伸，也不表示关心。"
    ),
    "夸夸": (
        "像夸夸群里的金牌群友：夸人夸具体——抓住对方消息里的细节往高了夸（眼光、效率、"
        "品位都行），语气真诚热络，「绝了」「这也太强了」「服了」随手就来，可以带感叹号；"
        "夸完顺势把正事接住（该答应的答应、该给时间的给时间）。"
        "不空泛、不谄媚、不连用三个感叹号，别把夸说成阴阳怪气。"
    ),
    # 恋爱向三件套（#70），方法论取自狗头军师（goutoujunshi，MIT）：先接住情绪、一句话只做
    # 一件事、吸引不是讨好、给台阶不欺骗、不考验人性——浓缩成三个版本，正好各自占一个话术槽。
    # 不搬它的知识库与档案脚本：生成层是单次请求出 ≤30 字候选，承接分析的是判断层，这里只留
    # 「这句怎么回」的军师口吻。
    "狗头军师": (
        "清醒的恋爱军师·稳健版：先接住对方的情绪或话头（累了先心疼、分享先接住），"
        "再把关系往前推一小步——一个具体的问题或低压力的邀约，一句话只做一件事。"
        "像个真实清醒的朋友：不舔不跪、不查户口式追问、不写小作文，邀约给对方能拒绝的台阶。"
    ),
    "狗头军师·会撩": (
        "清醒的恋爱军师·会撩版：带一点张力——用调侃、反差或画面感先轻轻推一下，"
        "再把球抛回去留个对方能接的出口；欣赏就夸具体的细节，不空夸。"
        "分寸在敢推进但不油腻：不忽冷忽热玩套路、不贬低、不硬撩，对方不接就自然收。"
    ),
    "狗头军师·抽离": (
        "清醒的恋爱军师·抽离版：对方冷淡、敷衍或只回「哈哈」时，不追问、不解释、不二次挽尊——"
        "一句话体面收住当轮（「先去忙，想聊再找我」），把主动权留给下次。"
        "不阴阳怪气、不赌气、不发小作文式告别，收得干脆但留得住体面。"
    ),
}

# What the panel starts with. The third slot used to default to 不用; users were found
# opening it and picking 阴阳怪气 by hand every session, so the default now ships it on —
# hud.py caps the list at MAX_SLOTS, so this can grow without touching the panel build.
DEFAULT_SLOTS: list[str] = ["高情商话术", "贴吧老哥 v1.0", "阴阳怪气"]
NONE_LABEL = "不用"          # the third dropdown's way of saying "only two candidates"

CUSTOM_VAR = "JEV_TONES"     # env var holding user-defined tones

# Custom tones under this many characters are refused, not loaded. A two-word
# instruction ("夸我") gives the model nothing to hold on to — the candidates come back
# generic, and the panel wears it as our fault. The built-ins run 60–120 chars; 10 is a
# floor that still admits one honest sentence while blocking pure noise. Refusals land
# in REJECTED_TONES so the startup log can point at the fix.
MIN_TONE_DESC_CHARS = 10
REJECTED_TONES: list[str] = []


def _custom_tones() -> dict[str, str]:
    """Tones the user defined in their env file, as `名字=说明` entries separated by `|`.

        export JEV_TONES="摸鱼大师=像个资深摸鱼选手，把活推得很得体|孙子兵法=用兵法比喻说话"

    A same-named entry overrides the built-in one, so the shipped wording can be tuned
    without touching this file. A tone called 不用 is dropped: that label is the panel's
    sentinel for "this slot is switched off", and letting a tone shadow it would make a
    slot impossible to switch off. Descriptions shorter than MIN_TONE_DESC_CHARS are
    refused and recorded in REJECTED_TONES (see that constant).
    """
    raw = userconfig.get(CUSTOM_VAR)
    out: dict[str, str] = {}
    for part in (raw or "").split("|"):
        name, sep, desc = part.partition("=")
        name, desc = name.strip(), desc.strip()
        if sep and name and name != NONE_LABEL:
            if len(desc) < MIN_TONE_DESC_CHARS:
                REJECTED_TONES.append(
                    f"「{name}」说明仅 {len(desc)} 字（至少 {MIN_TONE_DESC_CHARS} 字，"
                    "写清「什么语气 + 别变成什么」）")
                continue
            out[name] = desc
    return out


CUSTOM: dict[str, str] = _custom_tones()
PRESETS: dict[str, str] = {**BUILTIN, **CUSTOM}

def _label_alternation() -> str:
    """The labels as one regex alternative, with spaces and 「·」 made optional.

    "贴吧老哥 v1.0" is written with a space in the dropdown but the model may echo it
    without one ("贴吧老哥v1.0：") — matching the space loosely costs nothing and avoids a
    label that leaks through only sometimes. The same applies to the middle dot in
    "狗头军师·会撩": the model drops it about as readily as a space, so "·" also matches
    zero-or-one instead of exactly one.
    """
    escaped = (re.escape(k).replace(r"\ ", r"\s*").replace("·", "·?")
               for k in sorted(PRESETS, key=len, reverse=True))
    return "|".join(escaped)


_LABEL_RE = re.compile(
    rf"^[*_#\s]*(?:{_label_alternation()})[^，。！？；、,.!?;：:]{{0,4}}[*_#\s]*[:：]\s*")


def labels() -> list[str]:
    """All preset labels, in dropdown order."""
    return list(PRESETS)


def strip_label(line: str) -> str:
    """Remove a leading preset label the model echoed back, e.g. "贴吧老哥 v1.0：好的哥".

    The model is told not to label its lines, and usually complies — but the prompt itself
    shows it these labels, so now and then it echoes one. Because the labels are data, they
    are stripped from the same place they are defined: adding a tone here cannot silently
    break the parser the way a hardcoded list would.
    """
    return _LABEL_RE.sub("", line)
