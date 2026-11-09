import asyncio
import dataclasses
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from wxagentbot.codex_agent import CodexAgent
from wxagentbot.config import Conversation, Settings
from wxagentbot.store import Store
from wxagentbot.scheduling import EXECUTION_RULES
from wxagentbot.scheduling import SCHEDULE_OUTPUT_SCHEMA, ScheduledResult


class OfficialSdkContractTests(unittest.IsolatedAsyncioTestCase):
    """Contract doubles match the inspected official SDK; no model calls here."""
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.route = Conversation("a", "Alice", "private", root / "work", True)
        self.settings = Settings(root, root / "config.yml", (self.route,))
        self.store = Store(":memory:")
        self.calls = []
        self.slow = False
        self.final_response = "最终答案"
        self.turn_options = []
        outer = self

        class Turn:
            async def run(self):
                outer.calls.append(("run",))
                if outer.slow:
                    await asyncio.sleep(100)
                return types.SimpleNamespace(final_response=outer.final_response, items=["隐藏工具日志"])

            async def interrupt(self):
                outer.calls.append(("interrupt",))

        class Thread:
            id = "thread-test"

            async def turn(self, text, **kwargs):
                outer.calls.append(("turn", text))
                outer.turn_options.append(kwargs)
                return Turn()

        class Client:
            def __init__(self, config):
                outer.calls.append(("config", config))

            async def __aenter__(self):
                return self

            async def thread_start(self, **kwargs):
                outer.calls.append(("start", kwargs))
                return Thread()

            async def thread_resume(self, thread_id, **kwargs):
                outer.calls.append(("resume", thread_id, kwargs))
                return Thread()

            async def close(self):
                outer.calls.append(("close",))

        sdk = types.SimpleNamespace(
            AsyncCodex=Client, CodexConfig=lambda **kw: kw,
            Sandbox=types.SimpleNamespace(workspace_write="workspace_write", read_only="read_only"),
            ApprovalMode=types.SimpleNamespace(deny_all="deny_all", auto_review="auto_review"))
        self.patch = patch.dict("sys.modules", {"openai_codex": sdk})
        self.patch.start()
        self.agent = CodexAgent(self.settings, self.store)

    async def asyncTearDown(self):
        self.patch.stop()
        self.store.close()
        self.tmp.cleanup()

    async def test_official_signature_final_response_and_resume(self):
        task = {"context": "a", "sender": "Alice", "prompt": "帮我整理"}
        self.assertEqual(await self.agent.run(self.route, task), "最终答案")
        options = next(c[1] for c in self.calls if c[0] == "start")
        self.assertEqual(options["sandbox"], "workspace_write")
        self.assertEqual(options["approval_mode"], "deny_all")
        self.assertEqual(options["cwd"], str(self.route.workspace))
        self.assertNotIn("model", options)
        self.assertNotIn("config", options)
        await self.agent.run(self.route, task)
        resume = next(c for c in self.calls if c[0] == "resume")
        self.assertEqual(resume[1], "thread-test")
        self.assertFalse(resume[2]["include_turns"])
        self.assertNotIn("config", resume[2])
        self.assertEqual(sum(c[0] == "close" for c in self.calls), 2)

    async def test_model_and_effort_are_passed_on_start_and_resume(self):
        self.agent.settings = dataclasses.replace(self.settings, model="gpt-6-luna", reasoning_effort="medium")
        task = {"context": "a", "sender": "Alice", "prompt": "你好"}
        await self.agent.run(self.route, task)
        await self.agent.run(self.route, task)
        options = [c[-1] for c in self.calls if c[0] in ("start", "resume")]
        self.assertEqual(len(options), 2)
        for value in options:
            self.assertEqual(value["model"], "gpt-6-luna")
            self.assertEqual(value["config"], {"model_reasoning_effort": "medium"})
        for call in self.calls:
            if call[0] == "config":
                self.assertNotIn("env", call[1])
                self.assertNotIn("config_overrides", call[1])

    async def test_effort_change_continues_existing_thread(self):
        self.agent.settings = dataclasses.replace(self.settings, reasoning_effort="medium")
        key = self.agent.session_key("a", self.route.workspace)
        task = {"context": "a", "sender": "Alice", "prompt": "继续"}
        await self.agent.run(self.route, task)
        self.agent.settings = dataclasses.replace(self.agent.settings, reasoning_effort="high")
        self.assertEqual(key, self.agent.session_key("a", self.route.workspace))
        await self.agent.run(self.route, task)
        self.assertEqual(sum(c[0] == "start" for c in self.calls), 1)
        resume = next(c for c in self.calls if c[0] == "resume")
        self.assertEqual(resume[1], "thread-test")
        self.assertEqual(resume[2]["config"], {"model_reasoning_effort": "high"})

    async def test_timeout_interrupts_turn_and_closes_runtime(self):
        self.slow = True
        self.agent.settings = dataclasses.replace(self.settings, timeout_seconds=0.01)
        with self.assertRaises(TimeoutError):
            await self.agent.run(self.route, {"context": "a", "sender": "Alice", "prompt": "task"})
        self.assertIn(("interrupt",), self.calls)
        self.assertIn(("close",), self.calls)

    async def test_group_members_share_directory_and_thread_even_without_sender(self):
        group = dataclasses.replace(self.route, kind="group", missing_sender_policy="shared")
        for sender in ("Alice", "Bob", ""):
            await self.agent.run(group, {"context": "a:" + sender, "sender": sender, "prompt": "task"})
        options = [c[-1] for c in self.calls if c[0] in ("start", "resume")]
        self.assertEqual({c["cwd"] for c in options}, {str(group.workspace)})
        self.assertEqual(sum(c[0] == "start" for c in self.calls), 1)
        self.assertEqual(sum(c[0] == "resume" for c in self.calls), 2)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)

    async def test_legacy_isolated_policy_now_uses_shared_group_context(self):
        group = dataclasses.replace(self.route, kind="group", missing_sender_policy="isolated")
        for context in ("a:task:one", "a:task:two"):
            await self.agent.run(group, {"context": context, "sender": "", "prompt": "task"})
        self.assertEqual(sum(c[0] == "start" for c in self.calls), 1)
        self.assertEqual(sum(c[0] == "resume" for c in self.calls), 1)
        self.assertFalse((group.workspace / "tasks").exists())
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)

    async def test_different_groups_start_separate_threads(self):
        group = dataclasses.replace(self.route, kind="group", missing_sender_policy="shared")
        other = dataclasses.replace(group, id="b", workspace=Path(self.tmp.name) / "other")
        task = {"context": "old-member-context", "sender": "Alice", "prompt": "task"}
        await self.agent.run(group, task)
        await self.agent.run(other, task)
        self.assertEqual(sum(c[0] == "start" for c in self.calls), 2)
        self.assertFalse(any(c[0] == "resume" for c in self.calls))
        self.assertEqual({c[1]["cwd"] for c in self.calls if c[0] == "start"},
                         {str(group.workspace), str(other.workspace)})

    async def test_shared_group_session_is_resumed_after_reopening_database(self):
        self.store.close()
        path = Path(self.tmp.name) / "db.sqlite3"
        self.store = Store(path)
        self.agent = CodexAgent(self.settings, self.store)
        group = dataclasses.replace(self.route, kind="group", missing_sender_policy="shared")
        task = {"context": group.id, "sender": "", "prompt": "记住这个任务"}
        await self.agent.run(group, task)
        self.store.close()
        self.store = Store(path)
        self.agent = CodexAgent(self.settings, self.store)
        await self.agent.run(group, dict(task, sender="Bob", prompt="继续刚才的任务"))
        self.assertEqual(sum(c[0] == "start" for c in self.calls), 1)
        self.assertEqual(sum(c[0] == "resume" for c in self.calls), 1)

    async def test_shared_group_respects_disabled_continuity(self):
        self.agent.settings = dataclasses.replace(self.settings, continuity=False)
        group = dataclasses.replace(self.route, kind="group", missing_sender_policy="shared")
        for _ in range(2):
            await self.agent.run(group, {"context": group.id, "sender": "", "prompt": "task"})
        self.assertEqual(sum(c[0] == "start" for c in self.calls), 2)
        self.assertFalse(any(c[0] == "resume" for c in self.calls))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)

    async def test_scheduled_occurrence_does_not_replace_shared_group_context(self):
        group = dataclasses.replace(self.route, kind="group", missing_sender_policy="shared")
        task = {"context": group.id, "sender": "", "prompt": "task"}
        await self.agent.run(group, task)
        self.final_response = '{"notify":true,"message":"到期结果","reason":"正常任务"}'
        await self.agent.run(group, dict(task, context="a:schedule:one", schedule_id="one"))
        self.final_response = "最终答案"
        await self.agent.run(group, task)
        self.assertEqual(sum(c[0] == "start" for c in self.calls), 2)
        self.assertEqual(sum(c[0] == "resume" for c in self.calls), 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 2)

    async def test_scheduled_turn_uses_schema_and_returns_notification_decision(self):
        self.final_response = '{"notify":false,"message":"","reason":"已查询，明天无雨且温差小于10℃"}'
        result = await self.agent.run(self.route, {"context": "a:schedule:one", "schedule_id": "one",
                                                  "sender": "Alice", "prompt": "查询天气，条件不符不提醒"})
        self.assertIsInstance(result, ScheduledResult)
        self.assertFalse(result.notify)
        self.assertEqual(result.raw, self.final_response)
        self.assertEqual(self.turn_options[-1], {"output_schema": SCHEDULE_OUTPUT_SCHEMA})
        instructions = next(c[1]["developer_instructions"] for c in self.calls if c[0] == "start")
        self.assertIn("数据缺失", instructions)
        self.assertIn("不要当作条件不满足", instructions)

    async def test_invalid_scheduled_decision_is_rejected_and_runtime_closed(self):
        self.final_response = "天气正常，不用提醒"
        with self.assertRaises(ValueError):
            await self.agent.run(self.route, {"context": "a:schedule:one", "schedule_id": "one",
                                             "sender": "Alice", "prompt": "查询天气"})
        self.assertIn(("close",), self.calls)

    async def test_normal_chat_does_not_use_scheduled_output_schema(self):
        self.final_response = '{"notify":false,"message":"这是用户要的JSON"}'
        result = await self.agent.run(self.route, {"context": "a", "sender": "Alice", "prompt": "输出JSON"})
        self.assertEqual(result, self.final_response)
        self.assertEqual(self.turn_options, [{}])

    async def test_unidentified_group_task_is_rejected_if_policy_or_allowlist_forbids_it(self):
        group = dataclasses.replace(self.route, kind="group")
        restricted = dataclasses.replace(group, missing_sender_policy="shared", allowed_senders=("Alice",))
        for route in (group, restricted):
            with self.subTest(route=route), self.assertRaises(ValueError):
                await self.agent.run(route, {"context": "a:task:one", "sender": "", "prompt": "task"})
        self.assertFalse(self.calls)

    async def test_policy_change_does_not_resume_old_session(self):
        first = self.agent.session_key("a", self.route.workspace)
        self.agent.settings = dataclasses.replace(self.settings, sandbox="read_only")
        self.assertNotEqual(first, self.agent.session_key("a", self.route.workspace))

    async def test_prompt_file_is_read_for_each_task_and_passed_to_official_sdk(self):
        prompt = Path(self.tmp.name) / "prompt.txt"
        prompt.write_text("自定义语气一", encoding="utf-8")
        self.agent.settings = dataclasses.replace(self.settings, instructions_file=prompt)
        task = {"context": "a", "sender": "Alice", "prompt": "你好"}
        await self.agent.run(self.route, task)
        first = self.agent.session_key("a", self.route.workspace)
        prompt.write_text("自定义语气二", encoding="utf-8")
        await self.agent.run(self.route, task)
        self.assertNotEqual(first, self.agent.session_key("a", self.route.workspace))
        starts = [c[1]["developer_instructions"] for c in self.calls if c[0] == "start"]
        self.assertEqual(len(starts), 2)
        self.assertIn("自定义语气一", starts[0])
        self.assertIn("自定义语气二", starts[1])
        self.assertIn("不要使用 Markdown", starts[1])

    async def test_raw_reply_mode_does_not_add_plain_text_rules(self):
        self.agent.settings = dataclasses.replace(self.settings, reply_format="raw")
        self.assertEqual(self.agent.effective_instructions(), self.settings.instructions + "\n\n" + EXECUTION_RULES)

    async def parallel_failure(self, failure):
        started = {name: asyncio.Event() for name in ("first", "other")}
        release = {name: asyncio.Event() for name in started}
        interrupted, closed, clients = [], [], []

        class Turn:
            def __init__(self, prompt):
                self.prompt = prompt

            async def run(self):
                started[self.prompt].set()
                await release[self.prompt].wait()
                if self.prompt == "first" and failure == "timeout":
                    raise TimeoutError
                return types.SimpleNamespace(final_response=self.prompt + " final")

            async def interrupt(self):
                interrupted.append(self.prompt)

        class Thread:
            def __init__(self, uid):
                self.id = uid

            async def turn(self, prompt):
                return Turn(prompt)

        class Client:
            def __init__(self, config):
                self.uid = "client-" + str(len(clients))
                clients.append(self)

            async def __aenter__(self):
                return self

            async def thread_start(self, **kwargs):
                return Thread(self.uid)

            async def close(self):
                closed.append(self.uid)

        sdk = types.SimpleNamespace(
            AsyncCodex=Client, CodexConfig=lambda **kw: kw,
            Sandbox=types.SimpleNamespace(workspace_write="workspace_write"),
            ApprovalMode=types.SimpleNamespace(deny_all="deny_all"))
        other_route = dataclasses.replace(self.route, id="b", workspace=Path(self.tmp.name) / "other")
        with patch.dict("sys.modules", {"openai_codex": sdk}):
            first = asyncio.create_task(self.agent.run(self.route, {"context": "a", "sender": "Alice", "prompt": "first"}))
            other = asyncio.create_task(self.agent.run(other_route, {"context": "b", "sender": "Bob", "prompt": "other"}))
            try:
                await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())), 2)
                if failure == "cancel":
                    first.cancel()
                else:
                    release["first"].set()
                with self.assertRaises(asyncio.CancelledError if failure == "cancel" else TimeoutError):
                    await first
                self.assertEqual(interrupted, ["first"])
                self.assertFalse(other.done())
                self.assertEqual(len(clients), 2)
                self.assertEqual(closed, [clients[0].uid])
                release["other"].set()
                self.assertEqual(await other, "other final")
                self.assertCountEqual(closed, [client.uid for client in clients])
            finally:
                for job in (first, other):
                    if not job.done():
                        job.cancel()
                await asyncio.gather(first, other, return_exceptions=True)

    async def test_parallel_cancellation_interrupts_only_its_own_sdk_turn(self):
        await self.parallel_failure("cancel")

    async def test_parallel_timeout_interrupts_only_its_own_sdk_turn(self):
        await self.parallel_failure("timeout")
