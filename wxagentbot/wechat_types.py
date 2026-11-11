"""Transport-only types: no model, task queue, database or business configuration."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from pathlib import Path


@dataclasses.dataclass(frozen=True)
class ChatTarget:
    id: str
    name: str
    kind: str = "private"
    metadata: dict[str, str] = dataclasses.field(default_factory=dict, compare=False)

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.id):
            raise ValueError("会话 id 只能含英文字母、数字、下划线或连字符")
        if not self.name.strip() or self.kind not in ("private", "group"):
            raise ValueError("需要有效会话名称和 private/group 类型")


@dataclasses.dataclass(frozen=True)
class WechatOptions:
    app_name: str = "wechat"
    listen_mode: str = "independent"
    operation_timeout_seconds: float = 20
    use_events: bool = True
    use_screenshot_direction: bool = True
    max_nodes: int = 6000
    sender_regex: str = ""
    diagnostics_dir: Path | None = None

    def __post_init__(self):
        if self.listen_mode not in ("active", "independent"):
            raise ValueError("listen_mode 必须为 active 或 independent")
        if not 0.1 <= self.operation_timeout_seconds <= 86400 or not 200 <= self.max_nodes <= 30000:
            raise ValueError("桌面操作超时或 max_nodes 超出范围")
        if self.sender_regex and not {"sender", "text"} <= re.compile(self.sender_regex).groupindex.keys():
            raise ValueError("sender_regex 需要 sender 和 text 命名组")


@dataclasses.dataclass(frozen=True)
class Message:
    text: str
    sender: str = ""
    direction: str = "unknown"
    kind: str = "text"
    mentions: tuple[str, ...] = ()
    mention_verified: bool = False

    def fingerprint(self) -> str:
        payload = dataclasses.asdict(self)
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def content_fingerprint(self) -> str:
        """Stable snapshot anchor; direction/sender/mention metadata can settle later."""
        payload = {"text": self.text, "kind": self.kind}
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
