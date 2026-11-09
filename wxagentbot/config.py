from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from .wechat_types import ChatTarget, WechatOptions


class ConfigError(ValueError):
    pass


class _ConfigLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        keys = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise ConfigError("YAML 配置项名称必须是字符串")
            if key in keys:
                raise ConfigError(f"YAML 配置项重复: {key}")
            keys.add(key)
        return super().construct_mapping(node, deep=deep)


@dataclasses.dataclass(frozen=True)
class Conversation:
    id: str
    name: str
    kind: str
    workspace: Path
    enabled: bool = False
    allow_push: bool = False
    self_names: tuple[str, ...] = ()
    mention_mode: str = "metadata"
    allowed_senders: tuple[str, ...] = ()
    missing_sender_policy: str = "ignore"

    def wechat_target(self) -> ChatTarget:
        return ChatTarget(self.id, self.name, self.kind, metadata={
            "mention_mode": self.mention_mode, "missing_sender_policy": self.missing_sender_policy})


@dataclasses.dataclass(frozen=True)
class Settings:
    root: Path
    path: Path
    conversations: tuple[Conversation, ...]
    host: str = "127.0.0.1"
    port: int = 8765
    token_file: Path = Path(".secrets/api-token")
    database: Path = Path("data/bridge.sqlite3")
    app_name: str = "wechat"
    listen_mode: str = "active"
    poll_seconds: float = 3
    rescan_seconds: float = 15
    operation_timeout_seconds: float = 20
    use_events: bool = True
    use_screenshot_direction: bool = True
    max_nodes: int = 6000
    max_reply_chars: int = 1500
    sender_regex: str = ""
    model: str | None = None
    reasoning_effort: str | None = None
    binary: str | None = None
    timeout_seconds: float = 600
    startup_timeout_seconds: float = 40
    max_parallel_conversations: int = 3
    sandbox: str = "workspace_write"
    approval_mode: str = "deny_all"
    continuity: bool = True
    instructions: str = "请完成任务，用简洁中文回复最终结果。"
    instructions_file: Path | None = None
    reply_format: str = "plain"
    schedule_timezone: str = "Asia/Shanghai"
    schedule_poll_seconds: float = 1
    schedule_grace_seconds: float = 300

    def wechat_options(self) -> WechatOptions:
        return WechatOptions(app_name=self.app_name, listen_mode=self.listen_mode,
                             operation_timeout_seconds=self.operation_timeout_seconds,
                             use_events=self.use_events, use_screenshot_direction=self.use_screenshot_direction,
                             max_nodes=self.max_nodes, sender_regex=self.sender_regex,
                             diagnostics_dir=self.database.parent / "diagnostics")

    def resolve(self, name_or_id: str) -> Conversation:
        matches = [c for c in self.conversations if name_or_id in (c.id, c.name)]
        if len(matches) != 1:
            raise ConfigError("会话不存在或名称不唯一，请使用唯一的配置 id")
        return matches[0]


def _construct(cls, raw: dict[str, Any], **extra):
    fields = {f.name for f in dataclasses.fields(cls)}
    unknown = raw.keys() - fields
    if unknown:
        raise ConfigError(f"未知配置项: {', '.join(sorted(unknown))}")
    return cls(**raw, **extra)


def load_settings(path: str | Path) -> Settings:
    path = Path(path).expanduser().resolve()
    try:
        with path.open(encoding="utf-8") as f:
            doc = yaml.load(f, Loader=_ConfigLoader)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        location = f"（第 {mark.line + 1} 行，第 {mark.column + 1} 列）" if mark else ""
        raise ConfigError(f"YAML 配置格式错误{location}，请检查缩进和语法") from e
    if doc is None:
        doc = {}
    if not isinstance(doc, dict):
        raise ConfigError("YAML 配置顶层必须是映射")
    if set(doc) - {"server", "wechat", "codex", "storage", "scheduler", "conversations"}:
        raise ConfigError("存在未知的配置节")
    if not isinstance(doc.get("conversations", []), list):
        raise ConfigError("conversations 必须是 YAML 列表")
    root = path.parent

    def absolute(value):
        p = Path(value).expanduser()
        return (root / p).resolve() if not p.is_absolute() else p.resolve()

    routes = []
    for raw in doc.get("conversations", []):
        if not isinstance(raw, dict):
            raise ConfigError("conversations 中的每项必须是映射")
        raw = dict(raw)
        raw["workspace"] = absolute(raw.get("workspace", f"workspaces/{raw.get('id', '')}"))
        for key in ("self_names", "allowed_senders"):
            if not isinstance(raw.get(key, []), list):
                raise ConfigError(f"{key} 必须是字符串列表")
            raw[key] = tuple(raw.get(key, []))
            if not all(isinstance(x, str) and x.strip() for x in raw[key]):
                raise ConfigError(f"{key} 必须是非空字符串列表")
        c = _construct(Conversation, raw)
        if not all(isinstance(v, str) for v in (c.id, c.name, c.kind, c.mention_mode, c.missing_sender_policy)):
            raise ConfigError("id、name、kind、mention_mode、missing_sender_policy 必须是字符串")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", c.id):
            raise ConfigError("会话 id 只能含英文字母、数字、下划线或连字符")
        if not c.name.strip() or c.kind not in ("private", "group"):
            raise ConfigError("需要有效会话名称和 kind=private/group")
        if c.mention_mode not in ("metadata", "text"):
            raise ConfigError("mention_mode 必须为 metadata 或 text")
        if c.missing_sender_policy not in ("ignore", "shared", "isolated"):
            raise ConfigError("missing_sender_policy 必须为 ignore 或 shared（isolated 为旧配置兼容值）")
        if c.enabled and c.kind == "group" and not c.self_names:
            raise ConfigError("启用的群聊必须填写 self_names")
        if not isinstance(c.enabled, bool) or not isinstance(c.allow_push, bool):
            raise ConfigError("enabled/allow_push 必须是布尔值")
        routes.append(c)
    ids = [c.id for c in routes]
    if len(ids) != len(set(ids)):
        raise ConfigError("会话 id 重复")
    aliases: dict[str, str] = {}
    for c in routes:
        for alias in (c.id, c.name):
            if alias in aliases and aliases[alias] != c.id:
                raise ConfigError("会话名称或 id 冲突，请给微信会话设置唯一名称")
            aliases[alias] = c.id
    kwargs = {}
    for section in ("server", "wechat", "codex", "storage"):
        if not isinstance(doc.get(section, {}), dict):
            raise ConfigError(f"{section} 必须是配置表")
        overlap = kwargs.keys() & doc.get(section, {}).keys()
        if overlap:
            raise ConfigError("配置项出现在多个节中")
        kwargs.update(doc.get(section, {}))
    scheduler = doc.get("scheduler", {})
    scheduler_fields = {"timezone": "schedule_timezone", "poll_seconds": "schedule_poll_seconds",
                        "misfire_grace_seconds": "schedule_grace_seconds"}
    if not isinstance(scheduler, dict) or set(scheduler) - scheduler_fields.keys():
        raise ConfigError("scheduler 只接受 timezone、poll_seconds、misfire_grace_seconds")
    for key, value in scheduler.items():
        field = scheduler_fields[key]
        if field in kwargs:
            raise ConfigError("定时配置项出现在多个节中")
        kwargs[field] = value
    kwargs["token_file"] = absolute(kwargs.get("token_file", ".secrets/api-token"))
    kwargs["database"] = absolute(kwargs.get("database", "data/bridge.sqlite3"))
    if kwargs.get("instructions_file") is not None:
        if not isinstance(kwargs["instructions_file"], str):
            raise ConfigError("instructions_file 必须是文件路径字符串")
        kwargs["instructions_file"] = absolute(kwargs["instructions_file"])
        if not kwargs["instructions_file"].is_file():
            raise ConfigError("instructions_file 指定的提示词文件不存在")
    s = _construct(Settings, kwargs, root=root, path=path, conversations=tuple(routes))
    if not isinstance(s.port, int) or isinstance(s.port, bool) or not 1 <= s.port <= 65535:
        raise ConfigError("port 必须在 1..65535")
    for key in ("poll_seconds", "rescan_seconds", "operation_timeout_seconds", "timeout_seconds", "startup_timeout_seconds",
                "schedule_poll_seconds", "schedule_grace_seconds"):
        value = getattr(s, key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.1 <= value <= 86400:
            raise ConfigError(f"{key} 必须在 0.1..86400 秒")
    for key in ("use_events", "use_screenshot_direction", "continuity"):
        if not isinstance(getattr(s, key), bool):
            raise ConfigError(f"{key} 必须是布尔值")
    for key in ("max_nodes", "max_reply_chars"):
        if not isinstance(getattr(s, key), int) or isinstance(getattr(s, key), bool):
            raise ConfigError(f"{key} 必须是整数")
    if (isinstance(s.max_parallel_conversations, bool)
            or not isinstance(s.max_parallel_conversations, int)
            or not 1 <= s.max_parallel_conversations <= 16):
        raise ConfigError("max_parallel_conversations 必须是 1..16 的整数")
    for key in ("host", "app_name", "sender_regex", "sandbox", "approval_mode", "instructions", "schedule_timezone"):
        if not isinstance(getattr(s, key), str):
            raise ConfigError(f"{key} 必须是字符串")
    for key in ("binary", "model"):
        if getattr(s, key) is not None and not isinstance(getattr(s, key), str):
            raise ConfigError(f"{key} 必须是字符串")
    if s.reasoning_effort is not None and (
            not isinstance(s.reasoning_effort, str)
            or s.reasoning_effort not in ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")):
        raise ConfigError("reasoning_effort 必须为 none/minimal/low/medium/high/xhigh/max/ultra 或 null；可用档位取决于模型")
    if not 200 <= s.max_nodes <= 30000 or not 32 <= s.max_reply_chars <= 5000:
        raise ConfigError("max_nodes 或 max_reply_chars 超出范围")
    if s.sandbox not in ("read_only", "workspace_write"):
        raise ConfigError("sandbox 只支持 read_only/workspace_write")
    if s.listen_mode not in ("active", "independent"):
        raise ConfigError("listen_mode 必须为 active 或 independent")
    if s.approval_mode not in ("deny_all", "auto_review"):
        raise ConfigError("approval_mode 只支持 deny_all/auto_review")
    if s.reply_format not in ("plain", "raw"):
        raise ConfigError("reply_format 必须为 plain 或 raw")
    try:
        ZoneInfo(s.schedule_timezone)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ConfigError("scheduler.timezone 必须是有效时区，例如 Asia/Shanghai") from e
    if s.sender_regex:
        p = re.compile(s.sender_regex)
        if not {"sender", "text"} <= p.groupindex.keys():
            raise ConfigError("sender_regex 必须包含 sender 和 text 命名组")
    # A cwd alone is not a sandbox. Avoid accidental shared writable workspaces.
    active = [c for c in routes if c.enabled]
    for i, a in enumerate(active):
        if a.workspace == root or a.workspace in root.parents:
            raise ConfigError("工作目录不能是项目目录或其父目录")
        for b in active[i + 1:]:
            if a.workspace == b.workspace or a.workspace in b.workspace.parents or b.workspace in a.workspace.parents:
                raise ConfigError("不同会话的工作目录不能重叠")
    return s
