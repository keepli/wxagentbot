"""Reusable Linux WeChat transport and an optional Codex task bridge."""

__version__ = "0.1.0"

from .wechat import AtspiWechat as WechatClient
from .wechat import DesktopBusy, DesktopUnavailable, NotSent, SendUncertain
from .wechat_types import ChatTarget, Message, WechatOptions

__all__ = ["WechatClient", "WechatOptions", "ChatTarget", "Message", "DesktopBusy",
           "DesktopUnavailable", "NotSent", "SendUncertain"]
