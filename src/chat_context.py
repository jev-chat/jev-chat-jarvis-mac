"""Local conversation data and one bounded model input shared by every reply path."""
from __future__ import annotations

CONTEXT_CHARS = 8192


def model_message(text: str, context: str | None = None) -> str:
    return text[:max(0, CONTEXT_CHARS - len(context or ""))]


def context_text(messages, target: int, limit: int = 20, background: str = "") -> str | None:
    """Messages are (text, side, sender); target is an instance, not a text match.

    ponytail: character budget (not provider tokens); revisit for smaller model windows.
    History loses whole oldest messages first; persisted originals are never shortened.
    """
    current = messages[target]
    prior = [m for i, m in enumerate(messages) if i != target][-(limit - 1):] if limit > 1 else []
    header = (f"会话背景：\n{background}\n\n" if background else "")
    header += f"当前待回复发言人：{current[2] or '对方'}"
    lines = [f"{m[2] or {'me': '我', 'them': '对方'}.get(m[1], '方向未确认')}: {m[0]}"
             for m in prior]
    # If the two priority fields alone overflow, reserve up to half for the message;
    # a shorter field leaves its unused space to the other.
    if len(current[0]) + len(header) > CONTEXT_CHARS:
        message_size = min(len(current[0]), max(CONTEXT_CHARS // 2, CONTEXT_CHARS - len(header)))
        header = header[:CONTEXT_CHARS - message_size]
    else:
        message_size = len(current[0])
    budget = CONTEXT_CHARS - message_size
    while lines and len(header) + 1 + sum(len(line) + 1 for line in lines) > budget:
        lines.pop(0)
    return header + ("\n" + "\n".join(lines) if lines else "")


class Conversations:
    """Atomic, private local data; no network and no writes from model workers."""

    def __init__(self, path=None):
        from pathlib import Path
        self._anchors = {}
        self.data = {}
        self.error = ""
        self.revision = 0
        self._stamp = object()
        # One home for source runs and the packaged app alike — the directory that already
        # holds the venv and the env fallback (userconfig.config_dirs); tests pass paths.
        self.path = path or Path.home() / 'Library/Application Support/jev-jarvis/conversations.json'
        self.reload()

    def _file_stamp(self, fd=None):
        import os
        try:
            stat = self.path.stat() if fd is None else os.fstat(fd)
        except FileNotFoundError:
            return None
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns

    def reload(self):
        """Refresh external edits, including deletion; never retain unreadable private data."""
        import json
        stamp = ()
        error = ""
        try:
            stamp = self._file_stamp()
            if stamp == self._stamp and not self.error:
                return False
            raw = self.path.read_text(encoding='utf-8') if stamp is not None else ''
            data = json.loads(raw) if raw.strip() else {}
            if not isinstance(data, dict) or any(
                not isinstance(title, str) or not title.strip() or not isinstance(chat, dict)
                or not isinstance(chat.get('background', ''), str)
                or not isinstance(chat.get('messages', []), list)
                or not isinstance(chat.get('breaks', []), list)
                or any(type(i) is not int or not 0 < i < len(chat.get('messages', []))
                       for i in chat.get('breaks', []))
                or any(not isinstance(m, list) or len(m) != 3
                       or not all(isinstance(v, str) for v in m)
                       for m in chat.get('messages', []))
                for title, chat in data.items()
            ):
                raise ValueError('会话数据格式无效')
        except (OSError, ValueError):
            data = {}
            error = '本地会话文件读取失败或格式无效，已暂停写入；请修复文件或检查权限后重试。'
        changed = stamp != self._stamp or error != self.error
        if changed:
            self.data = data
            self._anchors.clear()
            self.revision += 1
        self._stamp, self.error = stamp, error
        return changed

    def _save(self, data):
        import json
        import os
        import tempfile
        if self.error:
            raise ValueError(self.error)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix='.conversations-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as out:
                json.dump(data, out, ensure_ascii=False)
                out.flush()
                os.fsync(out.fileno())
                # An external edit during preparation must not be overwritten by this snapshot.
                if self._file_stamp() != self._stamp:
                    self.reload()
                    raise OSError('会话文件已在外部修改，请重试')
                os.replace(name, self.path)
                self._stamp = self._file_stamp(out.fileno())
        finally:
            if os.path.exists(name):
                os.unlink(name)
        self.data = data

    def background(self, title):
        self.reload()
        return self.data.get(title, {}).get('background', '') if title else ''

    def save_background(self, title, text):
        if not title or not title.strip():
            raise ValueError('尚未确认聊天标题')
        self.reload()
        chat = dict(self.data.get(title, {}), background=text)
        self._save(dict(self.data, **{title: chat}))

    def history(self, title):
        self.reload()
        return [tuple(m) for m in self.data.get(title, {}).get('messages', [])][-100:] if title else []

    def observe(self, title, visible, record=True):
        """Return a reliable observed sequence and this frame's starting index.

        ponytail: exact sequence overlap only; OCR corrections or disjoint frames may
        underrecord. Add better alignment only with evidence from synthetic regressions.
        """
        if not title or not title.strip() or not visible:
            return visible, 0
        old = self.history(title)
        breaks = sorted(set(self.data.get(title, {}).get('breaks', [])))
        matches = [i for i in range(len(old) - len(visible) + 1)
                   if old[i:i + len(visible)] == visible
                   and not any(i < boundary < i + len(visible) for boundary in breaks)]
        if matches:
            if record:
                self._anchors.pop(title, None)
            if len(matches) != 1:
                return visible, 0
            start = matches[0]
            segment_start = max([0] + [i for i in breaks if i <= start])
            return old[segment_start:start + len(visible)], start - segment_start
        segment_start = breaks[-1] if breaks else 0
        tail = old[segment_start:]
        overlap = next((n for n in range(min(len(tail), len(visible)), 0, -1)
                        if tail[-n:] == visible[:n]), 0)
        if old and not overlap:
            # A gap is not evidence of chronology. Wait for a subsequent advancing
            # frame, then retain it as a separate observation segment, never model
            # context spanning the unknown gap. No anchor can survive an explicit clear.
            anchor = self._anchors.get(title, [])
            overlap = next((n for n in range(min(len(anchor), len(visible)), 0, -1)
                            if anchor[-n:] == visible[:n]), 0)
            if not record or not overlap or overlap == len(visible):
                if record:
                    self._anchors[title] = visible
                return visible, 0
            segment_start = len(old)
            breaks.append(segment_start)
            tail = anchor
        combined_segment = tail + visible[overlap:]
        start = len(tail) - overlap
        combined = old[:segment_start] + combined_segment
        if record:
            removed = max(0, len(combined) - 100)
            chat = dict(self.data.get(title, {}), messages=combined[-100:],
                        breaks=[i - removed for i in breaks if i > removed])
            self._save(dict(self.data, **{title: chat}))
            self._anchors.pop(title, None)
        return combined_segment, start

    def clear_history(self, title=None):
        if title is not None and not title.strip():
            raise ValueError('尚未确认聊天标题')
        self.reload()
        self._save({name: dict(chat, messages=[], breaks=[]) if title is None or name == title else chat
                    for name, chat in self.data.items()})
        if title is None:
            self._anchors.clear()
        else:
            self._anchors.pop(title, None)


def message_limit(value: str) -> int:
    if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 100:
        raise ValueError('上下文条数必须是 1—100 的整数')
    return int(value)
