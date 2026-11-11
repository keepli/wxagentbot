import dataclasses
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from wxagentbot.wechat_types import ChatTarget, WechatOptions
from wxagentbot.wechat import AtspiWechat, AppLookupError, DesktopUnavailable, NotSent, SendUncertain, human_controls_desktop


class Node:
    def __init__(self, name="", role="label", rect=(317, 47, 100, 18), text=None, children=None, attr=None):
        self.name, self.role, self.rect, self.text = name, role, rect, text
        self.children, self.attr = children or [], attr or {}
        self.parent = None
        for child in self.children: child.parent = self

    def get_name(self): return self.name
    def get_role_name(self): return self.role
    def get_child_count(self): return len(self.children)
    def get_child_at_index(self, i): return self.children[i]
    def get_attributes(self): return self.attr
    def get_parent(self): return self.parent
    def get_component_iface(self): return self
    def get_extents(self, *args): return types.SimpleNamespace(**dict(zip(("x", "y", "width", "height"), self.rect)))
    def get_text_iface(self):
        if self.text is None: raise RuntimeError("no text interface")
        return self
    def get_character_count(self): return len(self.text)
    def get_text(self, start, end): return self.text[start:end]
    def get_editable_text_iface(self): return self
    def set_text_contents(self, text): self.text = text; return True


class ApplicationLookupTests(unittest.TestCase):
    class Application(Node):
        def __init__(self, pid=42, name="wechat", defunct=False, children=None):
            super().__init__(name, "application", children=children if children is not None else [Node("微信", "frame")])
            self.pid, self.defunct, self.refreshes = pid, defunct, 0

        def clear_cache_single(self): self.refreshes += 1
        def clear_cache(self): self.refreshes += 1
        def get_process_id(self): return self.pid
        def get_state_set(self): return types.SimpleNamespace(contains=lambda state: self.defunct)

    def driver(self, *apps):
        desktop = Node(children=list(apps))
        desktop.clear_cache_single = lambda: None
        driver = AtspiWechat(WechatOptions())
        driver.deadline = float("inf")
        driver.Atspi = types.SimpleNamespace(get_desktop=lambda n: desktop,
                                            StateType=types.SimpleNamespace(DEFUNCT="defunct"))
        driver._foreground = driver._click = driver._human_control = lambda *args: self.fail("lookup must stay read-only")
        return driver, desktop

    def test_refreshes_registry_and_names_before_matching(self):
        app = self.Application(name="old-name")
        driver, desktop = self.driver()
        desktop.clear_cache_single = lambda: setattr(desktop, "children", [app])
        app.clear_cache_single = lambda: setattr(app, "name", " WeChat ")
        self.assertIs(driver._app(), app)
        self.assertEqual(driver.app_diagnostics["selected_pid"], 42)

    def test_ignores_defunct_unreadable_and_windowless_entries(self):
        dead = self.Application(pid=10, defunct=True)
        unreadable = self.Application(pid=11)
        def failed(): raise RuntimeError("disconnected")
        unreadable.get_state_set = failed
        empty = self.Application(pid=12, children=[])
        valid = self.Application(pid=13)
        driver, _ = self.driver(dead, unreadable, empty, valid)
        self.assertIs(driver._app(), valid)
        report = driver.app_diagnostics
        self.assertEqual(report["matching_entries"], 4)
        self.assertEqual(report["candidate_pids"], [13])
        self.assertEqual([a["state"] for a in report["applications"]],
                         ["defunct", "unavailable", "no_windows", "available"])

    def test_duplicate_process_prefers_root_with_main_window(self):
        proxy = self.Application(children=[Node("夜伴", "frame")])
        root = self.Application(children=[Node("微信", "frame"), Node("夜伴", "frame")])
        driver, _ = self.driver(proxy, root)
        self.assertIs(driver._app(), root)
        self.assertEqual(driver.app_diagnostics["candidate_pids"], [42])

    def test_multiple_real_clients_are_rejected_without_guessing(self):
        driver, _ = self.driver(self.Application(pid=42), self.Application(pid=43))
        with self.assertRaisesRegex(AppLookupError, "多个可用微信进程"):
            driver._app()
        self.assertEqual(driver.app_diagnostics["candidate_pids"], [42, 43])
        self.assertIsNone(driver.app_diagnostics["selected_pid"])

    def test_missing_app_reports_actual_names_and_can_recover_next_scan(self):
        driver, desktop = self.driver(self.Application(name="Thunar"))
        with self.assertRaisesRegex(AppLookupError, "同名条目=0.*Thunar"):
            driver._app()
        self.assertNotIn("请先登录", driver.app_diagnostics["error"])
        desktop.children = [self.Application()]
        driver._app()
        self.assertNotIn("error", driver.app_diagnostics)

    def test_unknown_process_id_is_not_used_as_a_unique_identity(self):
        for pid in (0, -1, 2 ** 32 - 1):
            with self.subTest(pid=pid):
                driver, _ = self.driver(self.Application(pid=pid))
                with self.assertRaisesRegex(AppLookupError, "同名条目=1.*可用进程=0"):
                    driver._app()

    def test_minimized_app_does_not_need_showing_state_for_lookup(self):
        app = self.Application()
        driver, _ = self.driver(app)
        driver._showing = lambda n: False
        self.assertIs(driver._app(), app)

    def test_broken_registry_is_a_discovery_error(self):
        driver, desktop = self.driver()
        def failed(): raise RuntimeError("registry disconnected")
        desktop.get_child_count = failed
        with self.assertRaisesRegex(AppLookupError, "无法读取桌面 AT-SPI 应用列表"):
            driver._app()


class NativeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.route = ChatTarget("yeiban", "夜伴", "private")
        self.settings = WechatOptions(listen_mode="active")
        self.driver = AtspiWechat(self.settings)
        self.driver.Atspi = types.SimpleNamespace(CoordType=types.SimpleNamespace(SCREEN=0))
        self.driver._showing = lambda n: True
        self.driver._screen = lambda: None
        self.driver.deadline = float("inf")

    def test_real_observed_header_geometry(self):
        li = Node("消息", "list", (301, 81, 578, 418))
        self.assertTrue(self.driver._header([Node("夜伴")], li, self.route))
        self.assertFalse(self.driver._header([Node("Other")], li, self.route))
        with self.assertRaises(NotSent):
            self.driver._header([Node("夜伴(5)")], li, self.route)

    def silent_tree(self):
        self.driver.settings = dataclasses.replace(self.settings, listen_mode="independent")
        self.driver._begin = lambda: None
        li = Node("消息", "list", (1, 81, 596, 409), children=[
            Node("新消息", "list item", attr={"direction": "incoming"})])
        header = Node("夜伴", "label", (17, 47, 28, 18))
        frame = Node("夜伴", "frame", children=[header, li])
        group = Node("测试群(5)", "frame")
        app = Node("wechat", "application", children=[Node("微信", "frame"), frame, group])
        self.driver._app = lambda: app
        self.driver._visible = lambda target: target.children
        self.driver._window_screen = lambda target: None
        return frame, li, app

    def test_silent_read_has_no_activation_click_or_selection_even_during_human_control(self):
        self.silent_tree()
        def forbidden(*args, **kwargs):
            self.fail("silent reads must not interact with the desktop")
        self.driver._foreground = self.driver._click = self.driver._select = forbidden
        self.driver._human_control = forbidden
        self.driver._screen = forbidden
        self.assertEqual(self.driver.read(self.route)[0].text, "新消息")

    def test_missing_silent_window_never_falls_back_to_main_window(self):
        self.silent_tree()
        self.driver._foreground = lambda *args: self.fail("must not activate")
        with self.assertRaisesRegex(DesktopUnavailable, "独立聊天窗口"):
            self.driver.read(dataclasses.replace(self.route, name="不存在"))

    def test_capture_failure_never_falls_back_to_foreground_screenshot(self):
        from wxagentbot.capture import CaptureUnavailable
        self.silent_tree()
        def unavailable(*args):
            raise CaptureUnavailable("后台捕获不可用")
        self.driver._window_screen = unavailable
        self.driver._foreground = self.driver._screen = lambda *args: self.fail("must stay silent")
        with self.assertRaises(CaptureUnavailable):
            self.driver.read(self.route)

    def test_background_direction_uses_only_the_chat_window_pixels(self):
        from PIL import Image, ImageDraw
        from wxagentbot.capture import WindowPixels
        frame, li, app = self.silent_tree()
        frame.children[0].rect = (117, 247, 28, 18)
        li.rect = (101, 281, 596, 409)
        li.children[0].rect = (101, 300, 596, 75)
        li.children[0].attr = {}
        self.driver._screen = lambda: self.fail("must not read the covering desktop")
        for left, expected in ((17, "incoming"), (541, "outgoing")):
            pixels = Image.new("RGB", (598, 631), (250, 250, 250))
            ImageDraw.Draw(pixels).rectangle((left, 108, left + 39, 147), fill=(40, 40, 40))
            self.driver._window_screen = lambda frame: WindowPixels(pixels, 100, 200)
            self.assertEqual(self.driver.read(self.route)[0].direction, expected)

    def test_reply_selects_and_activates_the_destination_independent_window(self):
        frame, li, app = self.silent_tree()
        calls = []
        self.driver._foreground = lambda app, title: calls.append(title)
        nodes, selected = self.driver._select(self.route)
        self.assertIs(selected, li)
        self.assertIs(self.driver.chat_frame, frame)
        self.assertEqual(calls, ["夜伴"])

    def test_preparing_existing_windows_does_not_click(self):
        self.silent_tree()
        group = dataclasses.replace(self.route, name="测试群", kind="group")
        self.driver._select_main = lambda *args: self.fail("already open")
        self.driver._human_control = lambda *args: self.fail("no input needed")
        self.driver.prepare_windows([self.route, group])

    def test_preparing_a_missing_window_double_clicks_once_and_reuses_it(self):
        frame, li, app = self.silent_tree()
        app.children.remove(frame)
        row = Node("夜伴\n最新消息", "list item")
        calls = []
        self.driver._select_main = lambda route: ([row], li)
        self.driver._human_control = lambda: None
        def click(node, *, double=False):
            self.assertIs(node, row)
            self.assertTrue(double)
            calls.append(node)
            app.children.append(frame)
        self.driver._click = click
        with patch("wxagentbot.wechat.time.sleep"):
            self.driver.prepare_windows([self.route])
            self.driver.prepare_windows([self.route])
        self.assertEqual(len(calls), 1)

    def test_disconnection_after_window_input_is_not_a_retryable_preflight_error(self):
        frame, li, app = self.silent_tree()
        app.children.remove(frame)
        row = Node("夜伴\n最新消息", "list item")
        calls, lookups = [], []
        def lookup():
            lookups.append(True)
            if len(lookups) == 3:
                raise AppLookupError("AT-SPI 临时不可用")
            return app
        self.driver._app = lookup
        self.driver._select_main = lambda route: ([row], li)
        self.driver._human_control = lambda: None
        self.driver._click = lambda node, **kw: calls.append(node)
        with patch("wxagentbot.wechat.time.sleep"), self.assertRaises(DesktopUnavailable) as error:
            self.driver.prepare_windows([self.route])
        self.assertNotIsInstance(error.exception, AppLookupError)
        self.assertEqual(calls, [row])

    def test_main_frame_is_scoped_separately_from_independent_windows(self):
        frame, li, app = self.silent_tree()
        self.assertEqual(self.driver._main_frame(app).name, "微信")
        app.children.append(Node("夜伴", "frame"))
        with self.assertRaises(DesktopUnavailable):
            self.driver._independent_frame(app, self.route)

    def test_plain_message_without_direction_is_never_treated_as_incoming(self):
        li = Node("消息", "list", children=[Node("你好", "list item")])
        messages = self.driver._messages(li, self.route)
        self.assertEqual(messages[0].text, "你好")
        self.assertEqual(messages[0].direction, "unknown")

    def test_group_probe_preserves_observed_metadata_in_a_private_file(self):
        import json
        import stat
        self.driver.settings = dataclasses.replace(self.settings, diagnostics_dir=Path(self.tmp.name) / "diagnostics")
        group = dataclasses.replace(self.route, kind="group")
        node = Node("@我 任务", attr={"sender": "张三", "direction": "incoming"})
        node.get_description = lambda: "observed description"
        li = Node(children=[node])
        messages = self.driver._messages(li, group)
        self.driver._group_report(group, li, messages)
        path = Path(self.driver.last_diagnostics[group.id]["report"])
        report = json.loads(path.read_text())
        self.assertEqual(report["messages"][0]["sender"], "张三")
        self.assertEqual(report["nodes"][0]["description"], "observed description")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_empty_editable_text_is_not_its_accessibility_name(self):
        self.assertEqual(self.driver._text(Node("夜伴", "text", text="")), "")
        self.assertEqual(self.driver._text(Node("你好", "list item", text="")), "你好")

    def test_gi_interface_method_collision_does_not_create_a_false_draft(self):
        class GiNode(Node):
            # Accessible.get_text() returns an interface and takes no offsets.
            def get_text(self):
                return self
        editor = GiNode("夜伴", "text", text="")
        self.driver.Atspi.Text = types.SimpleNamespace(
            get_character_count=lambda node: len(node.text),
            get_text=lambda node, start, end: node.text[start:end])
        self.assertEqual(self.driver._text(editor), "")
        self.assertEqual(self.driver._editor_text(editor), "")
        editor.text = "真实草稿"
        self.assertEqual(self.driver._editor_text(editor), "真实草稿")

    def test_empty_editor_does_not_call_get_text_with_zero_length(self):
        editor = Node("夜伴", "text", text="")
        def invalid_get_text(*args):
            self.fail("empty Qt editor must be handled using character count")
        editor.get_text = invalid_get_text
        self.assertEqual(self.driver._editor_text(editor), "")

    def test_unreadable_editor_is_not_misreported_as_a_real_draft(self):
        with self.assertRaisesRegex(NotSent, "无法读取输入框内容"):
            self.driver._editor_text(Node("夜伴", "text", text=None))

    def test_takeover_heartbeat_expiry_matches_desktop_proxy(self):
        marker = Path(self.tmp.name) / "control"
        self.assertFalse(human_controls_desktop(marker, now=200))
        marker.write_text("human owner 200\n")
        self.assertTrue(human_controls_desktop(marker, now=229))
        self.assertFalse(human_controls_desktop(marker, now=231))
        marker.write_text("agent human 200\n")
        self.assertFalse(human_controls_desktop(marker, now=201))
        marker.write_text("human owner invalid\n")
        self.assertFalse(human_controls_desktop(marker, now=201))

    def test_sender_and_mention_metadata_or_configured_regex(self):
        group = dataclasses.replace(self.route, kind="group")
        n = Node("@我 任务", "list item", attr={"sender": "Alice", "direction": "incoming", "mentions": '["我"]'})
        msg = self.driver._messages(Node(children=[n]), group)[0]
        self.assertTrue(msg.mention_verified)
        self.assertEqual(msg.sender, "Alice")
        self.assertEqual(msg.mentions, ("我",))
        self.driver.settings = dataclasses.replace(self.settings, sender_regex=r"^(?P<sender>[^\n]+)\n(?P<text>[\s\S]+)$")
        msg = self.driver._messages(Node(children=[Node("Alice\n任务", attr={"direction": "incoming"})]), group)[0]
        self.assertEqual((msg.sender, msg.text), ("Alice", "任务"))

    def test_draft_blocks_send(self):
        editor = Node("夜伴", "text", (321, 508, 542, 79), text="用户草稿")
        button = Node("发送", "button", (811, 597, 48, 24))
        li = Node("消息", "list", (301, 81, 578, 418))
        self.driver._begin = lambda: None
        self.driver._select = lambda r: ([editor, button], li)
        self.driver._messages = lambda *args: []
        self.driver._click = lambda n: self.fail("must not click")
        with self.assertRaisesRegex(NotSent, "未发送的草稿"):
            self.driver.send(self.route, "自动回复")
        self.assertEqual(editor.text, "用户草稿")

    def test_failure_before_actual_click_clears_only_generated_draft(self):
        editor = Node("夜伴", "text", (321, 508, 542, 79), text="")
        button = Node("发送", "button", (811, 597, 48, 24))
        li = Node("消息", "list", (301, 81, 578, 418))
        self.driver._begin = lambda: None
        self.driver._select = lambda r: ([editor, button], li)
        self.driver._messages = lambda *args: []
        self.driver._human_control = lambda: None
        self.driver._app = lambda: None
        self.driver._visible = lambda app: []
        self.driver._header = lambda *args: True
        def before_click(node, on_invoke=None):
            raise NotSent("微信未置前，尚未尝试输入")
        self.driver._click = before_click
        with self.assertRaisesRegex(NotSent, "微信未置前"):
            self.driver.send(self.route, "自动回复")
        self.assertEqual(editor.text, "")

    def test_independent_editor_named_input_is_found_by_its_real_layout(self):
        li = Node("消息", "list", (1, 81, 596, 409))
        editor = Node("输入", "text", (21, 499, 560, 79), text="")
        button = Node("发送", "button", (529, 588, 48, 24))
        search = Node("搜索", "text", (21, 46, 300, 22), text="")
        named_search = Node("夜伴", "text", (21, 46, 300, 22), text="")
        selected = self.driver._send_controls([search, named_search, editor, button], li)
        self.assertIs(selected[0], editor)
        self.assertIs(selected[1], button)
        editor.name = ""
        self.assertIs(self.driver._send_controls([editor, button], li)[0], editor)

    def test_ambiguous_or_outside_input_is_rejected_without_writing(self):
        li = Node("消息", "list", (1, 81, 596, 409))
        editor = Node("输入", "text", (21, 499, 560, 79), text="")
        second = Node("输入", "text", (21, 499, 560, 79), text="草稿")
        button = Node("发送", "button", (529, 588, 48, 24))
        with self.assertRaisesRegex(NotSent, "输入框"):
            self.driver._send_controls([editor, second, button], li)
        editor.rect = (601, 499, 560, 79)
        with self.assertRaisesRegex(NotSent, "输入框"):
            self.driver._send_controls([editor, button], li)
        self.assertEqual(second.text, "草稿")

    def test_independent_send_fills_input_clicks_and_confirms_outgoing_bubble(self):
        from wxagentbot.domain import Message
        li = Node("消息", "list", (1, 81, 596, 409))
        editor = Node("输入", "text", (21, 499, 560, 79), text="")
        button = Node("发送", "button", (529, 588, 48, 24))
        nodes = [editor, button, li]
        messages, clicked = [], []
        self.driver._begin = lambda: None
        self.driver._select = lambda route: (nodes, li)
        self.driver._messages = lambda *args: list(messages)
        self.driver._human_control = lambda: None
        self.driver._app = lambda: None
        self.driver._visible = lambda frame: nodes
        self.driver._header = lambda *args: True
        def click(target, on_invoke=None):
            self.assertIs(target, button)
            self.assertEqual(editor.text, "自动回复")
            on_invoke()
            clicked.append(True)
            messages.append(Message("自动回复", direction="outgoing"))
            editor.text = ""
        self.driver._click = click
        with patch("wxagentbot.wechat.time.sleep"):
            self.driver.send(self.route, "自动回复")
        self.assertEqual(clicked, [True])

    def test_independent_real_draft_is_preserved(self):
        li = Node("消息", "list", (1, 81, 596, 409))
        editor = Node("输入", "text", (21, 499, 560, 79), text="未发送的内容")
        button = Node("发送", "button", (529, 588, 48, 24))
        self.driver._begin = lambda: None
        self.driver._select = lambda route: ([editor, button], li)
        self.driver._messages = lambda *args: []
        self.driver._click = lambda *args, **kwargs: self.fail("must preserve draft")
        with self.assertRaisesRegex(NotSent, "未发送的草稿"):
            self.driver.send(self.route, "回复")
        self.assertEqual(editor.text, "未发送的内容")

    def test_bus_probe_prevents_native_library_abort(self):
        d = AtspiWechat(self.settings)
        with patch("wxagentbot.wechat.probe_desktop", return_value=["D-Bus 不可访问"]), self.assertRaises(DesktopUnavailable):
            d._init()

    def test_semantic_click_is_never_repeated_on_uncertainty(self):
        calls = []
        class Action:
            def get_n_actions(self): return 1
            def get_action_name(self, i): return "press"
            def do_action(self, i): calls.append(i); return False
        n = Node()
        n.get_action_iface = lambda: Action()
        self.driver._human_control = lambda: None
        with self.assertRaises(SendUncertain):
            self.driver._click(n)
        self.assertEqual(calls, [0])

    def test_visual_side_classifier(self):
        class Screen:
            width, height = 880, 640
            def __init__(self, left): self.left = left
            def getpixel(self, xy):
                x, y = xy
                ink = (317 <= x <= 357) if self.left else (823 <= x <= 863)
                return (40, 40, 40) if ink else (250, 250, 250)
        n = Node(rect=(301, 122, 578, 57))
        self.assertEqual(self.driver._visual_direction(n, Screen(True)), "incoming")
        self.assertEqual(self.driver._visual_direction(n, Screen(False)), "outgoing")

    def test_clipped_long_row_cannot_borrow_header_pixels_as_an_avatar(self):
        from PIL import Image, ImageDraw
        screen = Image.new('RGB', (600, 500), (250, 250, 250))
        ImageDraw.Draw(screen).rectangle((16, 0, 56, 70), fill=(40, 40, 40))
        row = Node(rect=(0, -200, 600, 420))
        self.assertEqual(self.driver._visual_direction(row, screen, (0, 100, 600, 400)), 'unknown')

    def test_long_bubble_direction_uses_avatar_area_only(self):
        from PIL import Image, ImageDraw
        screen = Image.new('RGB', (600, 600), (250, 250, 250))
        draw = ImageDraw.Draw(screen)
        draw.rectangle((544, 128, 584, 174), fill=(40, 40, 40))
        draw.rectangle((16, 200, 56, 350), fill=(40, 40, 40))
        row = Node(rect=(0, 120, 600, 300))
        self.assertEqual(self.driver._visual_direction(row, screen, (0, 100, 600, 400)), 'outgoing')
