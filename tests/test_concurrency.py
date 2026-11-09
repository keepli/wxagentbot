import asyncio
import dataclasses
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path
from unittest.mock import patch

from wxagentbot.config import Conversation, Settings
from wxagentbot.demo import DemoWechat
from wxagentbot.domain import Message
from wxagentbot.service import Bridge
from wxagentbot.store import Conflict, Store


class GatedAgent:
    def __init__(self):
        self.started = defaultdict(asyncio.Event)
        self.release = defaultdict(asyncio.Event)
        self.counts = defaultdict(int)
        self.peaks = defaultdict(int)
        self.cancelled = set()
        self.failures = set()

    async def run(self, route, task):
        prompt = task["prompt"]
        self.counts[route.id] += 1
        self.peaks[route.id] = max(self.peaks[route.id], self.counts[route.id])
        self.started[prompt].set()
        try:
            await self.release[prompt].wait()
            if prompt in self.failures:
                raise TimeoutError
            return "完成：" + prompt
        except asyncio.CancelledError:
            self.cancelled.add(prompt)
            raise
        finally:
            self.counts[route.id] -= 1


class ConversationConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.alice = Conversation("alice", "Alice", "private", root / "alice", True)
        self.group = Conversation("group", "测试群", "group", root / "group", True,
                                  self_names=("我",), mention_mode="text", missing_sender_policy="isolated")
        self.bob = Conversation("bob", "Bob", "private", root / "bob", True)
        self.settings = Settings(root, root / "config.yml", (self.alice, self.group, self.bob),
                                 database=root / "state.sqlite3", max_parallel_conversations=2)
        self.agent = GatedAgent()
        self.ui = DemoWechat()
        self.bridge = Bridge(self.settings, store=Store(self.settings.database), wechat=self.ui, agent=self.agent)
        self.histories = {}
        self.stopped = False

    async def asyncTearDown(self):
        if not self.stopped:
            await self.bridge.stop()
        self.tmp.cleanup()

    def enqueue(self, route, text, sender=""):
        if route.id not in self.histories:
            self.histories[route.id] = [Message("历史", direction="incoming")]
            self.bridge.store.ingest(route, self.histories[route.id])
        body = "@我 " + text if route.kind == "group" else text
        self.histories[route.id].append(Message(body, sender, "incoming"))
        count, _ = self.bridge.store.ingest(route, self.histories[route.id])
        self.assertEqual(count, 1)
        return self.bridge.store.db.execute("SELECT id FROM tasks ORDER BY rowid DESC LIMIT 1").fetchone()[0]

    async def wait_started(self, *prompts):
        await asyncio.wait_for(asyncio.gather(*(self.agent.started[p].wait() for p in prompts)), 2)

    async def wait_for(self, predicate):
        async with asyncio.timeout(2):
            while not predicate():
                await asyncio.sleep(0.01)

    def state(self, uid):
        return self.bridge.store.get("tasks", uid)["status"]

    async def test_slow_private_chat_does_not_block_another_window_or_reply(self):
        first = self.enqueue(self.alice, "慢任务")
        second = self.enqueue(self.alice, "同窗口第二条")
        other = self.enqueue(self.group, "其他窗口")
        await self.bridge.start()
        await self.wait_started("慢任务", "其他窗口")
        self.assertFalse(self.agent.started["同窗口第二条"].is_set())
        self.assertEqual(self.state(second), "queued")
        status = self.bridge.status()
        self.assertEqual(status["max_parallel_conversations"], 2)
        self.assertEqual({t["conversation"] for t in status["active_tasks"]}, {"alice", "group"})
        self.agent.release["其他窗口"].set()
        await self.wait_for(lambda: ("group", "完成：其他窗口") in self.ui.sent)
        self.assertEqual(self.state(other), "completed")
        self.assertEqual(self.state(first), "running")
        self.agent.release["慢任务"].set()
        await self.wait_started("同窗口第二条")
        self.agent.release["同窗口第二条"].set()
        await self.wait_for(lambda: self.state(second) == "completed")
        await self.wait_for(lambda: len([item for item in self.ui.sent if item[0] == "alice"]) == 2)
        self.assertEqual([text for chat, text in self.ui.sent if chat == "alice"],
                         ["完成：慢任务", "完成：同窗口第二条"])
        self.assertEqual(self.agent.peaks["alice"], 1)

    async def test_same_group_is_serial_across_members_and_isolated_contexts(self):
        first = self.enqueue(self.group, "群任务一", "张三")
        second = self.enqueue(self.group, "群任务二", "李四")
        third = self.enqueue(self.group, "未识别发送人的任务")
        self.assertEqual(len({self.bridge.store.get("tasks", uid)["context"]
                              for uid in (first, second, third)}), 3)
        self.enqueue(self.alice, "私聊")
        await self.bridge.start()
        await self.wait_started("群任务一", "私聊")
        self.assertFalse(self.agent.started["群任务二"].is_set())
        self.assertFalse(self.agent.started["未识别发送人的任务"].is_set())
        for prompt, following in (("群任务一", "群任务二"), ("群任务二", "未识别发送人的任务")):
            self.agent.release[prompt].set()
            await self.wait_started(following)
        self.assertEqual(self.agent.peaks["group"], 1)
        self.agent.release["未识别发送人的任务"].set()
        self.agent.release["私聊"].set()
        await self.wait_for(lambda: self.state(third) == "completed")

    async def test_parallel_limit_releases_capacity_for_another_conversation(self):
        self.enqueue(self.alice, "A")
        self.enqueue(self.group, "B")
        third = self.enqueue(self.bob, "C")
        await self.bridge.start()
        await self.wait_started("A", "B")
        self.assertFalse(self.agent.started["C"].is_set())
        self.assertEqual(self.state(third), "queued")
        self.assertEqual(len(self.bridge.status()["active_tasks"]), 2)
        self.agent.release["A"].set()
        await self.wait_started("C")
        self.assertEqual(self.agent.counts["group"], 1)
        self.agent.release["B"].set()
        self.agent.release["C"].set()
        await self.wait_for(lambda: not self.bridge.active_jobs)

    async def test_cancel_one_conversation_preserves_other_running_task(self):
        cancelled = self.enqueue(self.alice, "取消我")
        next_task = self.enqueue(self.alice, "下一条")
        other = self.enqueue(self.group, "继续运行")
        await self.bridge.start()
        await self.wait_started("取消我", "继续运行")
        self.bridge.cancel(cancelled)
        await self.wait_started("下一条")
        self.assertEqual(self.state(cancelled), "cancelled")
        self.assertEqual(self.agent.cancelled, {"取消我"})
        self.assertEqual(self.state(other), "running")
        self.agent.release["下一条"].set()
        self.agent.release["继续运行"].set()
        await self.wait_for(lambda: self.state(next_task) == self.state(other) == "completed")

    async def test_timeout_releases_same_conversation_without_cancelling_others(self):
        failed = self.enqueue(self.group, "超时")
        second = self.enqueue(self.group, "后续任务")
        other = self.enqueue(self.alice, "独立任务")
        self.agent.failures.add("超时")
        await self.bridge.start()
        await self.wait_started("超时", "独立任务")
        self.agent.release["超时"].set()
        await self.wait_started("后续任务")
        self.assertEqual(self.state(failed), "failed")
        self.assertEqual(self.state(other), "running")
        self.agent.release["后续任务"].set()
        self.agent.release["独立任务"].set()
        await self.wait_for(lambda: self.state(second) == self.state(other) == "completed")

    async def test_stop_interrupts_every_active_task_and_preserves_queued_work(self):
        first = self.enqueue(self.alice, "运行一")
        queued = self.enqueue(self.alice, "待运行")
        other = self.enqueue(self.group, "运行二")
        await self.bridge.start()
        await self.wait_started("运行一", "运行二")
        await self.bridge.stop()
        self.stopped = True
        with self.subTest("all_sdk_calls_cancelled"):
            self.assertEqual(self.agent.cancelled, {"运行一", "运行二"})
        self.assertFalse(self.bridge.active_jobs)
        reopened = Store(self.settings.database)
        try:
            self.assertEqual(reopened.get("tasks", first)["status"], "interrupted")
            self.assertEqual(reopened.get("tasks", other)["status"], "interrupted")
            self.assertEqual(reopened.get("tasks", queued)["status"], "queued")
            reopened.recover()
            self.assertEqual(reopened.claim("tasks")["id"], queued)
        finally:
            reopened.close()

    async def test_cancel_before_execution_does_not_leave_conversation_locked(self):
        uid = self.enqueue(self.alice, "尚未执行")
        process = asyncio.create_task(self.bridge.process_one_task())
        await asyncio.sleep(0)  # claim and register the job before its first step
        self.assertIn(uid, self.bridge.active_jobs)
        self.assertFalse(self.agent.started["尚未执行"].is_set())
        self.bridge.cancel(uid)
        self.assertTrue(await process)
        self.assertEqual(self.state(uid), "cancelled")
        self.assertFalse(self.bridge.active_jobs)
        next_uid = self.enqueue(self.alice, "可继续执行")
        self.agent.release["可继续执行"].set()
        await self.bridge.process_one_task()
        self.assertEqual(self.state(next_uid), "completed")

    async def test_reload_remains_blocked_until_every_active_task_finishes(self):
        first = self.enqueue(self.alice, "先结束")
        other = self.enqueue(self.group, "仍在运行")
        await self.bridge.start()
        await self.wait_started("先结束", "仍在运行")
        self.agent.release["先结束"].set()
        await self.wait_for(lambda: self.state(first) == "completed" and first not in self.bridge.active_jobs)
        with patch("wxagentbot.service.load_settings", return_value=self.settings), self.assertRaises(Conflict):
            self.bridge.reload()
        self.assertEqual(self.state(other), "running")
        self.agent.release["仍在运行"].set()
        await self.wait_for(lambda: not self.bridge.active_jobs)
        with patch("wxagentbot.service.load_settings", return_value=self.settings):
            self.assertEqual(self.bridge.reload(), 3)

    async def test_limit_one_keeps_global_serial_behavior(self):
        self.bridge.settings = dataclasses.replace(self.settings, max_parallel_conversations=1)
        self.enqueue(self.alice, "第一个窗口")
        self.enqueue(self.group, "第二个窗口")
        await self.bridge.start()
        await self.wait_started("第一个窗口")
        self.assertFalse(self.agent.started["第二个窗口"].is_set())
        self.agent.release["第一个窗口"].set()
        await self.wait_started("第二个窗口")
        self.agent.release["第二个窗口"].set()
        await self.wait_for(lambda: not self.bridge.active_jobs)
