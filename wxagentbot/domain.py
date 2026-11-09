from __future__ import annotations

import re

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
    for n in range(min(len(previous), len(current)), 0, -1):
        suffix = previous[-n:]
        for start in range(len(current) - n + 1):
            if current[start:start + n] == suffix:
                return list(range(start + n, len(current)))
    raise HistoryGap("消息窗口与上次记录没有重叠，已重新建立基线，避免重放历史")


def task_prompt(route: Conversation, message: Message) -> str | None:
    if not route.enabled or message.direction != "incoming" or message.kind != "text":
        return None
    text = message.text.strip()
    if not text:
        return None
    if route.kind == "group":
        if not message.sender and route.missing_sender_policy not in ("shared", "isolated"):
            return None
        if route.allowed_senders and message.sender not in route.allowed_senders:
            return None
        if route.mention_mode == "metadata":
            if not message.mention_verified or not set(route.self_names).intersection(message.mentions):
                return None
        else:
            patterns = [r"@" + re.escape(name) + r"(?=$|[\s\u2005\u2006\u2009\u200b])" for name in route.self_names]
            if not any(re.search(p, text) for p in patterns):
                return None
        for name in route.self_names:
            text = re.sub(r"@" + re.escape(name) + r"(?=$|[\s\u2005\u2006\u2009\u200b])", "", text)
    return text.strip() or None


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
