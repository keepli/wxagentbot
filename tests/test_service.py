import asyncio
import dataclasses
import tempfile
import unittest
from pathlib import Path

from wxagentbot.config import ConfigError, Conversation, Settings
from wxagentbot.demo import DemoAgent, DemoWechat
from wxagentbot.domain import Message
from wxagentbot.service import Bridge
from wxagentbot.store import Store
from wxagentbot.wechat import AppLookupError, NotSent, SendUncertain


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.route = Conversation("alice", "Alice", "private", self.root / "work", True, True)
        self.settings = Settings(self.root, self.root / "config.yml", (self.route,), max_reply_chars=40)
        self.ui = DemoWechat()
        self.bridge = Bridge(self.settings, store=Store(":memory:"), wechat=self.ui, agent=DemoAgent())

    async def asyncTearDown(self):
        await self.bridge.stop()
        self.tmp.cleanup()

    async def incoming(self, text="任务"):
        self.ui.messages["alice"] = [Message("历史", "Alice", "incoming")]
        await self.bridge.scan_once()
        self.ui.messages["alice"].append(Message(text, "Alice", "incoming"))
        await self.bridge.scan_once()

    async def test_full_round_trip_and_no_feedback(self):
        await self.incoming()
        self.assertTrue(await self.bridge.process_one_task())
        self.assertTrue(await self.bridge.process_one_delivery())
        await self.bridge.scan_once()
        self.assertFalse(await self.bridge.process_one_task())
        self.assertEqual(self.ui.sent, [("alice", "演示任务已完成：任务")])

    async def test_external_push_does_not_invoke_codex(self):
        row = self.bridge.enqueue_push("Alice", "主动推送", "push-1")
        await self.bridge.process_one_delivery()
        self.assertEqual(self.ui.sent, [("alice", "主动推送")])
        self.assertEqual(self.bridge.store.get("deliveries", row["id"])["status"], "sent")
        self.assertFalse(await self.bridge.process_one_task())

    async def test_push_validation_and_allowlist(self):
        for name, message, key in (("unknown", "x", None), ("Alice", " ", None), ("Alice", "x", "task:evil"), ("Alice", 3, None)):
            with self.subTest(name=name), self.assertRaises(ConfigError):
                self.bridge.enqueue_push(name, message, key)
        self.bridge.settings = dataclasses.replace(self.settings, conversations=(dataclasses.replace(self.route, allow_push=False),))
        with self.assertRaises(ConfigError):
            self.bridge.enqueue_push("Alice", "x")

    async def test_result_saved_before_send_and_not_recomputed(self):
        await self.incoming()
        await self.bridge.process_one_task()
        task = self.bridge.store.recent("tasks")[0]
        self.assertEqual(task["result"], "演示任务已完成：任务")
        self.assertIsNotNone(task["delivery_id"])
        self.assertFalse(await self.bridge.process_one_task())

    async def test_model_output_is_formatted_only_for_delivery_and_raw_result_is_kept(self):
        class MarkdownAgent:
            async def run(self, route, task):
                return "**广州**：13～20℃\n[来源](https://example.test)"
        self.bridge.agent = MarkdownAgent()
        await self.incoming()
        await self.bridge.process_one_task()
        task = self.bridge.store.recent("tasks")[0]
        self.assertTrue(task["result"].startswith("**广州**"))
        delivery = self.bridge.store.get("deliveries", task["delivery_id"])
        self.assertEqual(delivery["content"], "广州：13～20℃\n来源：https://example.test")

    async def test_raw_model_mode_and_external_push_keep_exact_markdown(self):
        class MarkdownAgent:
            async def run(self, route, task):
                return "**原文**"
        self.bridge.agent = MarkdownAgent()
        self.bridge.settings = dataclasses.replace(self.settings, reply_format="raw")
        await self.incoming()
        await self.bridge.process_one_task()
        task = self.bridge.store.recent("tasks")[0]
        self.assertEqual(self.bridge.store.get("deliveries", task["delivery_id"])["content"], "**原文**")
        self.bridge.settings = self.settings
        push = self.bridge.enqueue_push("Alice", "**外部消息**")
        self.assertEqual(push["content"], "**外部消息**")

    async def test_uncertain_delivery_is_not_retried(self):
        def uncertain(*args):
            raise SendUncertain("maybe sent")
        self.ui.send = uncertain
        row = self.bridge.enqueue_push("Alice", "out")
        await self.bridge.process_one_delivery()
        self.assertEqual(self.bridge.store.get("deliveries", row["id"])["status"], "unknown")
        self.assertFalse(await self.bridge.process_one_delivery())

    async def test_known_failure_is_reported(self):
        def failed(*args):
            raise NotSent("draft exists")
        self.ui.send = failed
        row = self.bridge.enqueue_push("Alice", "out")
        await self.bridge.process_one_delivery()
        self.assertEqual(self.bridge.store.get("deliveries", row["id"])["status"], "failed")

    async def test_changed_destination_never_sends(self):
        row = self.bridge.enqueue_push("Alice", "out")
        self.bridge.settings = dataclasses.replace(self.settings, conversations=(dataclasses.replace(self.route, name="Other"),))
        await self.bridge.process_one_delivery()
        self.assertEqual(self.ui.sent, [])
        self.assertEqual(self.bridge.store.get("deliveries", row["id"])["status"], "failed")

    async def test_cancel_queued_task(self):
        await self.incoming()
        task = self.bridge.store.recent("tasks")[0]
        self.bridge.cancel(task["id"])
        self.assertFalse(await self.bridge.process_one_task())
        self.assertEqual(self.bridge.store.get("tasks", task["id"])["status"], "cancelled")

    async def test_cancel_running_task(self):
        started = asyncio.Event()
        class SlowAgent:
            async def run(self, route, task):
                started.set()
                await asyncio.sleep(1000)
        self.bridge.agent = SlowAgent()
        await self.incoming()
        process = asyncio.create_task(self.bridge.process_one_task())
        await started.wait()
        uid = self.bridge.current_task_id
        self.bridge.cancel(uid)
        await process
        self.assertEqual(self.bridge.store.get("tasks", uid)["status"], "cancelled")

    async def test_task_failure_has_no_raw_error_in_reply(self):
        class Broken:
            async def run(self, route, task):
                raise RuntimeError("secret-in-error")
        self.bridge.agent = Broken()
        await self.incoming()
        await self.bridge.process_one_task()
        await self.bridge.process_one_delivery()
        self.assertNotIn("secret-in-error", self.ui.sent[0][1])

    async def test_ui_read_send_are_serial(self):
        import threading
        import time
        active, peak, lock = 0, 0, threading.Lock()
        def work():
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with lock:
                active -= 1
        await asyncio.gather(*(self.bridge.ui(work) for _ in range(5)))
        self.assertEqual(peak, 1)

    async def test_silent_watch_prepares_once_then_only_reads(self):
        class SilentWechat(DemoWechat):
            prepared, pumped = 0, 0
            def prepare_windows(self, routes):
                self.prepared += 1
            def pump(self):
                self.pumped += 1
                if self.pumped == 2:
                    raise asyncio.CancelledError
                return True
        self.ui = SilentWechat()
        self.bridge.wechat = self.ui
        self.bridge.settings = dataclasses.replace(self.settings, listen_mode="independent", poll_seconds=0.1)
        self.bridge.windows_prepared = False
        with self.assertRaises(asyncio.CancelledError):
            await self.bridge.watch_loop()
        self.assertEqual(self.ui.prepared, 1)

    async def test_failed_silent_setup_is_not_repeated_in_a_click_loop(self):
        class BrokenSetup(DemoWechat):
            prepared = 0
            def prepare_windows(self, routes):
                self.prepared += 1
                raise RuntimeError("无法打开独立窗口")
        self.ui = BrokenSetup()
        self.bridge.wechat = self.ui
        self.bridge.settings = dataclasses.replace(self.settings, listen_mode="independent", poll_seconds=0.1)
        self.bridge.windows_prepared = False
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(self.bridge.watch_loop(), 0.35)
        self.assertEqual(self.ui.prepared, 1)
        self.assertEqual(self.bridge.status()["desktop"], "degraded")

    async def test_temporary_app_lookup_failure_retries_before_window_input(self):
        class TemporaryFailure(DemoWechat):
            prepared, pumped = 0, 0
            def prepare_windows(self, routes):
                self.prepared += 1
                if self.prepared == 1:
                    raise AppLookupError("AT-SPI 应用暂未注册")
            def pump(self):
                self.pumped += 1
                if self.pumped == 2:
                    raise asyncio.CancelledError
                return True
        self.ui = TemporaryFailure()
        self.bridge.wechat = self.ui
        self.bridge.settings = dataclasses.replace(self.settings, listen_mode="independent", poll_seconds=0.01)
        self.bridge.windows_prepared = False
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(self.bridge.watch_loop(), 2)
        self.assertEqual(self.ui.prepared, 2)
        self.assertTrue(self.bridge.windows_prepared)
        self.assertIsNone(self.bridge.window_setup_error)

    async def test_one_failed_chat_is_visible_when_another_chat_succeeds(self):
        second = dataclasses.replace(self.route, id="bob", name="Bob", workspace=self.root / "bob")
        self.bridge.settings = dataclasses.replace(self.settings, conversations=(second, self.route))
        original = self.ui.read
        def read(route):
            if route.id == "bob":
                raise RuntimeError("请打开独立窗口")
            return original(route)
        self.ui.read = read
        await self.bridge.scan_once()
        self.assertIn("bob", self.bridge.status()["scan_errors"])
        self.assertEqual(self.bridge.status()["desktop"], "degraded")

    async def test_group_only_mention_triggers_and_replies_original_group(self):
        group = dataclasses.replace(self.route, kind="group", self_names=("我",), mention_mode="text")
        self.bridge.settings = dataclasses.replace(self.settings, conversations=(group,))
        self.ui.messages["alice"] = [Message("旧消息", "张三", "incoming")]
        await self.bridge.scan_once()
        self.ui.messages["alice"] += [Message("普通聊天", "张三", "incoming"), Message("@我 帮忙", "张三", "incoming")]
        await self.bridge.scan_once()
        self.assertEqual(len(self.bridge.store.recent("tasks")), 1)
        await self.bridge.process_one_task()
        await self.bridge.process_one_delivery()
        self.assertEqual(self.ui.sent[0], ("alice", "演示任务已完成：帮忙"))

    async def test_private_and_group_are_watched_and_reply_to_their_own_chats(self):
        group = Conversation("group", "测试群", "group", self.root / "group", True,
                             self_names=("我",), mention_mode="text")
        self.bridge.settings = dataclasses.replace(self.settings, conversations=(self.route, group))
        self.ui.messages["alice"] = [Message("历史", "Alice", "incoming")]
        self.ui.messages["group"] = [Message("历史", "张三", "incoming")]
        await self.bridge.scan_once()
        self.ui.messages["alice"].append(Message("私聊任务", "Alice", "incoming"))
        self.ui.messages["group"] += [Message("普通消息", "张三", "incoming"),
                                      Message("@我 群任务", "张三", "incoming")]
        await self.bridge.scan_once()
        tasks = self.bridge.store.recent("tasks")
        self.assertEqual({t["conversation"] for t in tasks}, {"alice", "group"})
        self.assertEqual(len(tasks), 2)
        for _ in range(2):
            await self.bridge.process_one_task()
            await self.bridge.process_one_delivery()
        self.assertCountEqual(self.ui.sent, [("alice", "演示任务已完成：私聊任务"),
                                             ("group", "演示任务已完成：群任务")])

    async def test_group_new_resets_shared_context_even_for_legacy_queued_task(self):
        group = Conversation("group", "测试群", "group", self.root / "group", True,
                             self_names=("我",), mention_mode="text", missing_sender_policy="shared")
        self.bridge.settings = dataclasses.replace(self.settings, conversations=(self.route, group))
        self.ui.messages[group.id] = [Message("历史", direction="incoming")]
        await self.bridge.scan_once()
        for context in ("group:policy", "group:old-member:policy", "alice:policy", "other-group:policy"):
            self.bridge.store.save_thread(context, "saved-" + context)
        self.ui.messages[group.id].append(Message("@我 /new", direction="incoming"))
        await self.bridge.scan_once()
        task = self.bridge.store.recent("tasks")[0]
        # A task queued before the upgrade may still carry a member/event key.
        with self.bridge.store.db:
            self.bridge.store.db.execute("UPDATE tasks SET context='group:old-member' WHERE id=?", (task["id"],))
        self.assertTrue(await self.bridge.process_one_task())
        self.assertIsNone(self.bridge.store.thread_id("group:policy"))
        self.assertIsNone(self.bridge.store.thread_id("group:old-member:policy"))
        self.assertIsNotNone(self.bridge.store.thread_id("alice:policy"))
        self.assertIsNotNone(self.bridge.store.thread_id("other-group:policy"))
        self.assertEqual(self.bridge.store.get("tasks", task["id"])["status"], "completed")

    async def test_real_group_mention_without_sender_replies_while_private_still_works(self):
        group = Conversation("group", "AAA猛攻建材批发", "group", self.root / "group", True,
                             self_names=("小小🤖",), mention_mode="text", missing_sender_policy="shared")
        self.bridge.settings = dataclasses.replace(self.settings, conversations=(self.route, group))
        self.ui.messages["alice"] = [Message("历史", "Alice", "incoming")]
        self.ui.messages["group"] = [Message("历史", direction="incoming")]
        await self.bridge.scan_once()
        self.ui.messages["alice"].append(Message("私聊", "Alice", "incoming"))
        self.ui.messages["group"] += [Message("@小小🤖\u2005你好", direction="incoming"),
                                      Message("还不行，有bug", direction="incoming")]
        await self.bridge.scan_once()
        self.assertEqual(len(self.bridge.store.recent("tasks")), 2)
        for _ in range(2):
            await self.bridge.process_one_task()
            await self.bridge.process_one_delivery()
        self.assertCountEqual(self.ui.sent, [("alice", "演示任务已完成：私聊"),
                                             ("group", "演示任务已完成：你好")])
        await self.bridge.scan_once()
        self.assertFalse(await self.bridge.process_one_task())
