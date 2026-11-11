from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Conversation
from .wechat_types import Message  # Re-export for existing business-layer imports.


class HistoryGap(RuntimeError):
    pass


def new_tail(previous: list[str], current: list[str]) -> list[int]:
    """Align the longest old suffix; preserve repeated identical messages.

    Earliest matching occurrence is intentional: old=[A], new=[A,A] has one
    new message. With no overlap, fail closed instead of replaying history.
    """
    if not previous or not current:
        return []
    _, start, n = tail_overlap(previous, current)
    return list(range(start + n, len(current)))


def tail_overlap(previous: list[str], current: list[str]) -> tuple[int, int, int]:
    """Return old start, new start and length of the same suffix anchor."""
    for n in range(min(len(previous), len(current)), 0, -1):
        suffix = previous[-n:]
        for start in range(len(current) - n + 1):
            if current[start:start + n] == suffix:
                return len(previous) - n, start, n
    raise HistoryGap("消息窗口与上次记录没有重叠，已重新建立基线，避免重放历史")


@dataclass(frozen=True)
class MessageDecision:
    prompt: str | None
    reason: str
    pending: bool = False


def message_decision(route: Conversation, message: Message) -> MessageDecision:
    if not route.enabled:
        return MessageDecision(None, "会话未启用")
    if message.kind != "text":
        return MessageDecision(None, "非文本消息")
    text = message.text.strip()
    if not text:
        return MessageDecision(None, "正文为空")
    if message.direction == "outgoing":
        return MessageDecision(None, "本机发出")
    if message.direction not in ("incoming", "unknown"):
        return MessageDecision(None, "非收信消息")
    # A missing textual mention cannot become eligible by learning direction.
    if route.kind == "group" and route.mention_mode == "text":
        patterns = [r"@" + re.escape(name) + r"(?=$|[\s\u2005\u2006\u2009\u200b])" for name in route.self_names]
        if not any(re.search(p, text) for p in patterns):
            return MessageDecision(None, "未提及本账号")
    if message.direction == "unknown":
        return MessageDecision(None, "收发方向待识别", True)
    if route.kind == "group":
        if not message.sender and (route.missing_sender_policy not in ("shared", "isolated") or route.allowed_senders):
            return MessageDecision(None, "发送人待识别", True)
        if route.allowed_senders and message.sender not in route.allowed_senders:
            return MessageDecision(None, "发送人不在允许列表")
        if route.mention_mode == "metadata":
            if not message.mention_verified:
                return MessageDecision(None, "提及信息待识别", True)
            if not set(route.self_names).intersection(message.mentions):
                return MessageDecision(None, "未提及本账号")
        for name in route.self_names:
            text = re.sub(r"@" + re.escape(name) + r"(?=$|[\s\u2005\u2006\u2009\u200b])", "", text)
    return MessageDecision(text.strip() or None, "已入队" if text.strip() else "提及后没有任务内容")


def task_prompt(route: Conversation, message: Message) -> str | None:
    return message_decision(route, message).prompt


def context_key(route: Conversation, message: Message, *, event_key: str | None = None) -> str:
    """Private chats and groups each have one context, independent of sender."""
    return route.id


def split_reply(text: str, limit: int) -> list[str]:
    text = text.strip()
    if not text:
        raise ValueError("回复为空")
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit + 1)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:]
    if text:
        chunks.append(text)
    return chunks
