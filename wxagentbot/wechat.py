from __future__ import annotations

import contextlib
import importlib
import json
import os
import re
import socket
import tempfile
import time
from pathlib import Path

from .capture import CompositeCapture
from .wechat_types import ChatTarget, Message, WechatOptions
from .window import X11Window


class DesktopUnavailable(RuntimeError):
    pass


class AppLookupError(DesktopUnavailable):
    """Read-only application discovery failed; no desktop input was attempted."""


class NotSent(RuntimeError):
    """Failure before the send action was invoked."""


class DesktopBusy(NotSent):
    pass


class SendUncertain(RuntimeError):
    """Send may have happened. Automatic retry would risk duplication."""


def human_controls_desktop(marker: Path, now: float | None = None) -> bool:
    """Match instabox's 30-second takeover heartbeat without modifying it."""
    try:
        words = marker.read_text().split()
        if len(words) < 3 or words[0] != "human":
            return False
        since = int(words[2])
    except (OSError, ValueError):
        return False
    return (time.time() if now is None else now) - since <= 30


def probe_desktop() -> list[str]:
    """Probe first: libatspi can abort the process if the bus is inaccessible."""
    errors = []
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    match = re.search(r"(?:^|;)unix:path=([^,;]+)", address)
    if not match:
        errors.append("缺少 unix:path 格式的 DBUS_SESSION_BUS_ADDRESS")
    else:
        try:
            with socket.socket(socket.AF_UNIX) as s:
                s.settimeout(2)
                s.connect(match.group(1))
        except OSError as e:
            errors.append(f"无法访问桌面 D-Bus: {e.strerror}；请在同一桌面用户的 Terminal 中运行")
    if not os.environ.get("DISPLAY"):
        errors.append("缺少 DISPLAY（此机器通常为 :1）")
    return errors


def attributes(node) -> dict[str, str]:
    with contextlib.suppress(Exception):
        data = node.get_attributes()
        if isinstance(data, dict):
            return data
        return dict(x.split(":", 1) for x in data if ":" in x)
    return {}


class AtspiWechat:
    """All methods run on one UI executor thread; widgets are re-found per action."""

    def __init__(self, settings: WechatOptions):
        self.settings = settings
        self.Atspi = None
        self.GLib = None
        self.listeners = []
        self.dirty = True
        self.event_error = None
        self.deadline = 0.0
        self.last_diagnostics = {}
        self.app_diagnostics = {}
        self.window = X11Window()
        self.capture = CompositeCapture()
        self.chat_frame = None

    def _init(self):
        if self.Atspi:
            return
        errors = probe_desktop()
        if errors:
            raise DesktopUnavailable("；".join(errors))
        try:
            gi = importlib.import_module("gi")
            gi.require_version("Atspi", "2.0")
            self.Atspi = importlib.import_module("gi.repository.Atspi")
            self.GLib = importlib.import_module("gi.repository.GLib")
            self.Atspi.set_timeout(1200, 3000)
        except ImportError as e:
            raise DesktopUnavailable("缺少系统 python3-gi / gir1.2-atspi-2.0；虚拟环境需启用 system-site-packages") from e
        if self.settings.use_events:
            try:
                listener = self.Atspi.EventListener.new(self._event, None)
                for kind in ("object:children-changed", "object:text-changed", "object:property-change:accessible-name"):
                    if not listener.register(kind):
                        raise DesktopUnavailable("事件订阅未成功，使用定时扫描")
                self.listeners.append(listener)
            except Exception as e:
                self.event_error = type(e).__name__

    def _event(self, event, *args):
        with contextlib.suppress(Exception):
            if event.source.get_application().get_name().strip().casefold() == self.settings.app_name.strip().casefold():
                self.dirty = True

    def pump(self) -> bool:
        self._init()
        ctx = self.GLib.MainContext.default()
        for _ in range(30):
            if not ctx.pending():
                break
            ctx.iteration(False)
        dirty, self.dirty = self.dirty, False
        return dirty

    def _begin(self):
        self._init()
        self.deadline = time.monotonic() + self.settings.operation_timeout_seconds

    def _check(self):
        if time.monotonic() > self.deadline:
            raise DesktopUnavailable("微信界面操作超时")

    def _walk(self, root, depth=0):
        count = 0
        pending = [(root, depth)]
        while pending:
            self._check()
            node, d = pending.pop()
            count += 1
            if count > self.settings.max_nodes:
                raise DesktopUnavailable("控件树超过 max_nodes，请调整配置")
            yield node, d
            if d >= 64:
                continue
            with contextlib.suppress(Exception):
                pending.extend((node.get_child_at_index(i), d + 1) for i in reversed(range(node.get_child_count())))

    def _showing(self, node):
        with contextlib.suppress(Exception):
            return node.get_state_set().contains(self.Atspi.StateType.SHOWING)
        return False

    def _rect(self, node):
        r = node.get_component_iface().get_extents(self.Atspi.CoordType.SCREEN)
        return r.x, r.y, r.width, r.height

    def _name(self, node):
        with contextlib.suppress(Exception):
            return node.get_name() or ""
        return ""

    def _role(self, node):
        with contextlib.suppress(Exception):
            return node.get_role_name()
        return ""

    def _app(self):
        # Refresh before matching names: clearing only the chosen app leaves
        # stale names/registry entries capable of preventing any selection.
        report = {"configured_name": self.settings.app_name, "updated": time.time(),
                  "applications": [], "matching_entries": 0, "candidate_pids": [],
                  "registry_errors": 0, "selected_pid": None}
        self.app_diagnostics = report
        candidates = {}
        try:
            self._check()
            desktop = self.Atspi.get_desktop(0)
            refresh = getattr(desktop, "clear_cache_single", None) or desktop.clear_cache
            refresh()
            count = desktop.get_child_count()
            if not 0 <= count <= self.settings.max_nodes:
                raise RuntimeError("无效的 AT-SPI 应用数量")
            for i in range(count):
                self._check()
                try:
                    app = desktop.get_child_at_index(i)
                    if app is None:
                        report["registry_errors"] += 1
                        continue
                    refresh = getattr(app, "clear_cache_single", None) or app.clear_cache
                    refresh()
                    name = app.get_name() or ""
                except Exception:
                    report["registry_errors"] += 1
                    continue
                entry = {"name": name}
                report["applications"].append(entry)
                if name.strip().casefold() != self.settings.app_name.strip().casefold():
                    continue
                report["matching_entries"] += 1
                try:
                    if app.get_state_set().contains(self.Atspi.StateType.DEFUNCT):
                        entry["state"] = "defunct"
                        continue
                    pid = app.get_process_id()
                    if not 0 < pid < 2 ** 31:
                        raise RuntimeError("无法确定微信 PID")
                    entry["pid"] = pid
                    child_count = app.get_child_count()
                    if not 0 <= child_count <= self.settings.max_nodes:
                        raise RuntimeError("无效的微信窗口数量")
                    frames = []
                    for j in range(child_count):
                        self._check()
                        frame = app.get_child_at_index(j)
                        if self._role(frame) == "frame":
                            frames.append(frame)
                    entry["windows"] = len(frames)
                    if not frames:
                        entry["state"] = "no_windows"
                        continue
                    entry["state"] = "available"
                    # Duplicate proxies for one process are not two clients.
                    # Prefer the root exposing the main window and more frames.
                    rank = (any(self._name(f) == "微信" for f in frames), len(frames))
                    if pid not in candidates or rank > candidates[pid][0]:
                        candidates[pid] = (rank, app)
                except Exception as e:
                    entry["state"], entry["error"] = "unavailable", type(e).__name__
            report["candidate_pids"] = sorted(candidates)
            self._check()
        except Exception as e:
            report["error"] = f"无法读取桌面 AT-SPI 应用列表（{type(e).__name__}）"
            raise AppLookupError(report["error"]) from e
        if not candidates:
            observed = ", ".join(e["name"][:80] for e in report["applications"][:20]) or "空"
            report["error"] = (f"AT-SPI 未发现可用微信应用（名称={self.settings.app_name!r}，"
                               f"同名条目={report['matching_entries']}，可用进程=0，"
                               f"读取失败={report['registry_errors']}，应用列表={observed}）；"
                               "请检查桌面会话和微信无障碍接口，此结果不能判断是否已登录")
            raise AppLookupError(report["error"])
        if len(candidates) > 1:
            report["error"] = (f"AT-SPI 发现多个可用微信进程（PID={report['candidate_pids']}，"
                               f"同名条目={report['matching_entries']}），无法唯一确定目标")
            raise AppLookupError(report["error"])
        pid, (_, app) = next(iter(candidates.items()))
        try:
            app.clear_cache()
        except Exception as e:
            report["error"] = f"无法刷新微信控件缓存（PID={pid}，{type(e).__name__}）"
            raise AppLookupError(report["error"]) from e
        report["selected_pid"] = pid
        return app

    def check_application(self):
        """Read-only health check; never activates windows or reads chat text."""
        self._begin()
        self._app()
        return self.app_diagnostics

    def _visible(self, app):
        app.clear_cache()
        return [n for n, _ in self._walk(app) if self._showing(n)]

    def _human_control(self):
        marker = Path(os.environ.get("XDG_RUNTIME_DIR", "/run/user/1000")) / "instabox-control"
        if human_controls_desktop(marker):
            raise DesktopBusy("用户正在接管桌面，请在 Computer 面板交还控制后继续")

    def _foreground(self, app, title="微信"):
        self._check()
        return self.window.ensure_active(app.get_process_id(), title, self._human_control)

    def _frame_of(self, node):
        for _ in range(64):
            if self._role(node) == "frame":
                return node
            node = node.get_parent()
            if node is None:
                break
        raise DesktopUnavailable("无法定位聊天所属窗口")

    def _main_frame(self, app):
        frames = [app.get_child_at_index(i) for i in range(app.get_child_count())]
        frames = [n for n in frames if self._role(n) == "frame" and self._name(n) == "微信"]
        if len(frames) != 1:
            raise DesktopUnavailable("未找到唯一微信主窗口")
        return frames[0]

    def _independent_frame(self, app, route, *, missing_ok=False):
        frames = [app.get_child_at_index(i) for i in range(app.get_child_count())]
        pattern = re.escape(route.name) + (r"(?:[（(]\d+[)）])?" if route.kind == "group" else "")
        frames = [n for n in frames if self._role(n) == "frame" and re.fullmatch(pattern, self._name(n))]
        if not frames and missing_ok:
            return None
        if len(frames) != 1:
            raise DesktopUnavailable(f"请先双击会话列表，打开唯一的独立聊天窗口：{route.name}")
        return frames[0]

    def _window_screen(self, frame):
        app = frame.get_application()
        window = self.window.find(app.get_process_id(), self._name(frame))
        return self.capture.capture(window)

    def _click(self, node, on_invoke=None, *, double=False):
        self._check()
        self._human_control()
        action = None
        chosen = None
        with contextlib.suppress(Exception):
            action = node.get_action_iface()
            for i in range(action.get_n_actions()):
                if action.get_action_name(i).lower() in ("click", "press", "activate"):
                    chosen = i
                    break
        if chosen is not None and not double:
            # Never try a second click if a semantic action had an ambiguous result.
            if on_invoke is not None:
                on_invoke()
            if not action.do_action(chosen):
                raise SendUncertain("控件动作未确认成功")
            return
        # Qt widget focus alone does not activate its top-level X11 window.
        with contextlib.suppress(Exception):
            node.get_component_iface().grab_focus()
        self._foreground(node.get_application(), self._name(self._frame_of(node)))
        self._human_control()
        x, y, w, h = self._rect(node)
        if w <= 0 or h <= 0 or not self._showing(node):
            raise NotSent("目标控件当前不可见")
        if on_invoke is not None:
            on_invoke()
        if not self.Atspi.generate_mouse_event(x + w // 2, y + h // 2, "b1d" if double else "b1c"):
            raise NotSent("桌面未接受点击操作")

    def _message_list(self, nodes):
        lists = [n for n in nodes if self._role(n) == "list" and self._name(n) == "消息"]
        if len(lists) != 1:
            raise DesktopUnavailable("未找到唯一可见的消息列表；可能未打开聊天、已锁定或客户端版本不同")
        return lists[0]

    def _header(self, nodes, msg_list, route):
        x, y, w, _ = self._rect(msg_list)
        labels = []
        for n in nodes:
            if self._role(n) != "label":
                continue
            nx, ny, nw, nh = self._rect(n)
            if x <= nx < x + w and y - 65 <= ny < y and nw > 0 and nh > 0:
                labels.append(self._name(n))
        title_ok = any(name == route.name or (name.startswith(route.name + "(") and re.search(r"\(\d+\)$", name)) or (name.startswith(route.name + "（") and re.search(r"（\d+）$", name)) for name in labels)
        if not title_ok:
            return False
        group = any(re.search(r"[（(]\d+[)）]$", name) for name in labels)
        if (route.kind == "group") != group:
            raise NotSent("聊天标题与配置的私人/群聊类型不符，请校准配置")
        return True

    def _select(self, route):
        app = self._app()
        if self.settings.listen_mode == "independent":
            frame = self._independent_frame(app, route)
            self._foreground(app, self._name(frame))
            self.chat_frame = frame
            nodes = self._visible(frame)
            li = self._message_list(nodes)
            if not self._header(nodes, li, route):
                raise NotSent("独立聊天窗口标题校验失败")
            return nodes, li
        return self._select_main(route)

    def _select_main(self, route):
        app = self._app()
        # Pixel-based direction detection must see WeChat, not covering terminals.
        self._foreground(app)
        frame = self._main_frame(app)
        self.chat_frame = frame
        nodes = self._visible(frame)
        with contextlib.suppress(DesktopUnavailable):
            msg_list = self._message_list(nodes)
            if self._header(nodes, msg_list, route):
                return nodes, msg_list
        # Prefer the current conversation list, whose entries have exact names.
        lists = [n for n in nodes if self._role(n) == "list" and self._name(n) == "会话"]
        candidates = []
        for li in lists:
            for i in range(li.get_child_count()):
                n = li.get_child_at_index(i)
                if self._showing(n) and self._name(n).split("\n")[0].strip() == route.name:
                    candidates.append(n)
        if not candidates:
            # Search only in the sidebar, never in a hidden web/search panel.
            sidebar = []
            for n in nodes:
                if self._role(n) == "text" and self._name(n) == "搜索":
                    x, y, w, h = self._rect(n)
                    if y < 150 and w > 40:
                        sidebar.append((x, n))
            if not sidebar:
                raise NotSent("找不到侧栏搜索框")
            search = min(sidebar, key=lambda pair: pair[0])[1]
            self._human_control()
            if not search.get_editable_text_iface().set_text_contents(route.name):
                raise NotSent("无法填写微信搜索框")
            time.sleep(0.5)
            nodes = self._visible(frame)
            candidates = [n for n in nodes if self._role(n) == "list item" and self._name(n).split("\n")[0].strip() == route.name]
        if len(candidates) != 1:
            raise NotSent("无法唯一定位会话（名称重复、折叠或搜索结果格式不支持）")
        self._click(candidates[0])
        time.sleep(0.25)
        nodes = self._visible(frame)
        msg_list = self._message_list(nodes)
        if not self._header(nodes, msg_list, route):
            raise NotSent("切换后聊天标题校验失败")
        return nodes, msg_list

    def prepare_windows(self, routes):
        """One-time setup only; regular silent reads never call this method."""
        self._begin()
        self._app()  # A failed preflight is safe to retry: no input yet.
        try:
            self._prepare_windows(routes)
        except AppLookupError as e:
            # Input may already have happened. Do not restart the click sequence.
            raise DesktopUnavailable(f"准备独立窗口时微信连接中断，请检查窗口后重启：{e}") from e

    def _prepare_windows(self, routes):
        for route in routes:
            self._begin()
            if self._independent_frame(self._app(), route, missing_ok=True) is not None:
                continue
            self._human_control()
            nodes, _ = self._select_main(route)
            rows = [n for n in nodes if self._role(n) == "list item" and self._showing(n)
                    and self._name(n).split("\n")[0].strip() == route.name]
            if len(rows) != 1:
                raise DesktopUnavailable(f"无法唯一定位用于打开独立窗口的会话：{route.name}")
            self._click(rows[0], double=True)
            frame = None
            for _ in range(8):
                time.sleep(0.15)
                self._check()
                frame = self._independent_frame(self._app(), route, missing_ok=True)
                if frame is not None:
                    break
            if frame is None:
                raise DesktopUnavailable(f"未能打开独立窗口，请手动双击会话：{route.name}")

    def _text(self, node):
        with contextlib.suppress(Exception):
            iface = node.get_text_iface()
            text_api = getattr(self.Atspi, "Text", None)
            if text_api is not None:
                # PyGObject returns an Accessible wrapper for get_text_iface().
                # Its get_text() means 'get the interface', not Text.GetText.
                # Call the interface methods explicitly to avoid the collision.
                count = text_api.get_character_count(iface)
                text = "" if count == 0 else text_api.get_text(iface, 0, min(count, 16000))
            else:
                count = iface.get_character_count()
                text = "" if count == 0 else iface.get_text(0, min(count, 16000))
            if isinstance(text, str) and (text or self._role(node) in ("text", "entry", "password text")):
                return text
        return self._name(node)

    def _editor_text(self, node):
        """Draft detection must never substitute the accessible chat name."""
        try:
            iface = node.get_text_iface()
            api = getattr(self.Atspi, "Text", None)
            count = api.get_character_count(iface) if api is not None else iface.get_character_count()
            if count == 0:
                return ""
            if count < 0 or count > 16000:
                raise ValueError("invalid editor text length")
            value = api.get_text(iface, 0, count) if api is not None else iface.get_text(0, count)
            if not isinstance(value, str):
                raise ValueError("text interface returned no text")
            return value
        except Exception as e:
            raise NotSent(f"无法读取输入框内容（{type(e).__name__}），已停止本次发送") from e

    def _send_controls(self, nodes, li):
        """Find controls inside the already verified chat frame, by layout."""
        buttons = [n for n in nodes if self._role(n) in ("push button", "button")
                   and self._name(n) == "发送"]
        if len(buttons) != 1:
            raise NotSent("无法唯一定位当前聊天窗口的发送按钮")
        lx, ly, lw, lh = self._rect(li)
        _, send_y, _, _ = self._rect(buttons[0])
        editors = []
        for node in nodes:
            if self._role(node) not in ("text", "entry"):
                continue
            x, y, w, h = self._rect(node)
            # Qt names the independent editor '输入', while other versions use
            # the chat name. Search boxes are above the list or in the sidebar.
            if (w >= 80 and h >= 20 and lx <= x and x + w <= lx + lw + 2
                    and y >= ly + lh - 2 and y + h <= send_y + 2):
                editors.append(node)
        if len(editors) != 1:
            raise NotSent("无法唯一定位当前聊天消息列表下方的输入框")
        return editors[0], buttons[0]

    def _screen(self):
        if self.settings.use_screenshot_direction:
            try:
                return importlib.import_module("PIL.ImageGrab").grab(xdisplay=os.environ.get("DISPLAY")).convert("RGB")
            except Exception:
                pass
        return None

    def _visual_direction(self, node, screen):
        if screen is None:
            return "unknown"
        x, y, w, h = self._rect(node)
        if w < 100 or h < 20:
            return "unknown"
        # Only classify sides when avatar-shaped areas have clear contrast.
        # Text recognition and sender identity never come from these pixels.
        def ink(left, right):
            count = 0
            bx, by, br, bb = getattr(screen, "bounds", (0, 0, screen.width, screen.height))
            for xx in range(max(bx, left), min(br, right), 2):
                for yy in range(max(by, y + 8), min(bb, y + h - 4), 2):
                    r, g, b = screen.getpixel((xx, yy))[:3]
                    count += min(r, g, b) < 185
            return count
        left = ink(x + 16, x + 56)
        right = ink(x + w - 56, x + w - 16)
        if left >= 25 and right < 12:
            return "incoming"
        if right >= 25 and left < 12:
            return "outgoing"
        return "unknown"

    def _messages(self, li, route):
        if self.settings.listen_mode == "independent":
            screen = self._window_screen(self._frame_of(li)) if self.settings.use_screenshot_direction else None
        else:
            screen = self._screen()
        result, unknown = [], 0
        for i in range(li.get_child_count()):
            self._check()
            n = li.get_child_at_index(i)
            text = self._text(n).strip()
            if not text:
                continue
            if re.fullmatch(r"(?:(?:昨天|前天|今天|星期.|\d{1,4}[年/.-].*)\s*)?\d{1,2}:\d{2}", text) or "撤回了一条消息" in text:
                result.append(Message(text, direction="system", kind="system"))
                continue
            attr = attributes(n)
            direction = attr.get("message-direction", attr.get("direction", "unknown"))
            if direction not in ("incoming", "outgoing"):
                direction = self._visual_direction(n, screen)
            sender = attr.get("sender", "")
            if self.settings.sender_regex:
                match = re.match(self.settings.sender_regex, text)
                if match:
                    sender, text = match.group("sender"), match.group("text")
            if route.kind == "private" and direction == "incoming":
                sender = sender or route.name
            mentions, verified = (), False
            if "mentions" in attr:
                with contextlib.suppress(ValueError, TypeError):
                    values = json.loads(attr["mentions"])
                    if isinstance(values, list) and all(isinstance(v, str) for v in values):
                        mentions, verified = tuple(values), True
            kind = "media" if text in ("[图片]", "[视频]", "[语音]", "[文件]", "[表情]") else "text"
            result.append(Message(text, sender, direction, kind, mentions, verified))
            unknown += direction == "unknown" or (route.kind == "group" and direction == "incoming" and not sender)
        self.last_diagnostics[route.id] = {**route.metadata, "rows": len(result), "unclassified": unknown,
                                           "event_error": self.event_error,
                                           "listen_mode": self.settings.listen_mode,
                                           "input_locator": "layout"}
        return result

    def read(self, route: ChatTarget) -> list[Message]:
        self._begin()
        if self.settings.listen_mode == "independent":
            # Deliberately no focus, selection, mapping, mouse or keyboard calls.
            frame = self._independent_frame(self._app(), route)
            nodes = self._visible(frame)
            li = self._message_list(nodes)
            if not self._header(nodes, li, route):
                raise DesktopUnavailable("后台聊天标题校验失败")
        else:
            self._human_control()
            _, li = self._select(route)
        messages = self._messages(li, route)
        if route.kind == "group" and self.settings.diagnostics_dir is not None:
            self._group_report(route, li, messages)
        return messages

    def _group_report(self, route, li, messages):
        """Keep a private local report for calibrating real group metadata."""
        rows = []
        for i in range(max(0, li.get_child_count() - 20), li.get_child_count()):
            node = li.get_child_at_index(i)
            row = {"name": self._name(node), "attributes": attributes(node)}
            with contextlib.suppress(Exception):
                row["description"] = node.get_description()
            rows.append(row)
        report = {**route.metadata, "conversation": route.id, "name": route.name, "updated": time.time(),
                  "listen_mode": self.settings.listen_mode,
                  "input_locator": "layout",
                  "messages": [dict(text=m.text, sender=m.sender, direction=m.direction,
                                    mentions=m.mentions, mention_verified=m.mention_verified)
                               for m in messages[-20:]], "nodes": rows}
        directory = Path(self.settings.diagnostics_dir)
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        filename = directory / (route.id + ".json")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=directory, encoding="utf-8", delete=False) as f:
                temporary = Path(f.name)
                json.dump(report, f, ensure_ascii=False, indent=2)
            temporary.replace(filename)
            self.last_diagnostics[route.id]["report"] = str(filename)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def send(self, route: ChatTarget, text: str):
        self._begin()
        invoked = False
        wrote = False
        editor = None
        expected = text.strip()
        try:
            nodes, li = self._select(route)
            before_count = sum(m.text == expected and m.direction == "outgoing" for m in self._messages(li, route))
            editor, button = self._send_controls(nodes, li)
            if self._editor_text(editor).strip():
                raise NotSent("聊天输入框有未发送的草稿，已保留草稿")
            self._human_control()
            if not editor.get_editable_text_iface().set_text_contents(text):
                raise NotSent("输入框不支持填写文字")
            wrote = True
            if self._editor_text(editor) != text:
                raise NotSent("输入框内容校验失败")
            # Revalidate the visible title immediately before invoking Send.
            if not self._header(self._visible(self.chat_frame or self._app()), li, route):
                raise NotSent("发送前目标会话发生变化")
            self._human_control()
            def mark_invoked():
                nonlocal invoked
                invoked = True
            self._click(button, on_invoke=mark_invoked)
            for _ in range(8):
                time.sleep(0.25)
                self._check()
                nodes = self._visible(self.chat_frame or self._app())
                li = self._message_list(nodes)
                if not self._header(nodes, li, route):
                    raise SendUncertain("发送后目标会话发生变化")
                outgoing = [m for m in self._messages(li, route) if m.direction == "outgoing" and m.text == expected]
                if len(outgoing) > before_count:
                    return
            raise SendUncertain("未能确认新发出的气泡，请检查微信；不会自动重发")
        except Exception as e:
            if invoked:
                raise SendUncertain(str(e)) from e
            if wrote and editor is not None:
                with contextlib.suppress(Exception):
                    if self._editor_text(editor) == text:
                        editor.get_editable_text_iface().set_text_contents("")
            if isinstance(e, NotSent):
                raise
            raise NotSent(str(e)) from e

    def dump_tree(self):
        self._begin()
        rows = []
        for node, depth in self._walk(self._app()):
            row = {"depth": depth, "role": self._role(node), "name": self._name(node),
                   "showing": self._showing(node), "attributes": attributes(node)}
            with contextlib.suppress(Exception):
                row["rect"] = self._rect(node)
            rows.append(row)
        return rows

    def close(self):
        self.capture.close()
        for listener in self.listeners:
            with contextlib.suppress(Exception):
                listener.deregister("object:")
