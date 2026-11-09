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
        outer = self

        class Turn:
            async def run(self):
                outer.calls.append(("run",))
                if outer.slow:
                    await asyncio.sleep(100)
                return types.SimpleNamespace(final_response="最终答案", items=["隐藏工具日志"])

            async def interrupt(self):
                outer.calls.append(("interrupt",))

        class Thread:
            id = "thread-test"

            async def turn(self, text):
                outer.calls.append(("turn", text))
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
        await self.agent.run(self.route, task)
        resume = next(c for c in self.calls if c[0] == "resume")
        self.assertEqual(resume[1], "thread-test")
        self.assertFalse(resume[2]["include_turns"])
        self.assertEqual(sum(c[0] == "close" for c in self.calls), 2)

    async def test_timeout_interrupts_turn_and_closes_runtime(self):
        self.slow = True
        self.agent.settings = dataclasses.replace(self.settings, timeout_seconds=0.01)
        with self.assertRaises(TimeoutError):
            await self.agent.run(self.route, {"context": "a", "sender": "Alice", "prompt": "task"})
        self.assertIn(("interrupt",), self.calls)
        self.assertIn(("close",), self.calls)

    async def test_group_senders_have_separate_filesystem_and_threads(self):
        group = dataclasses.replace(self.route, kind="group")
        for sender in ("Alice", "Bob"):
            await self.agent.run(group, {"context": "a:" + sender, "sender": sender, "prompt": "task"})
        dirs = [c[1]["cwd"] for c in self.calls if c[0] == "start"]
        self.assertEqual(len(set(dirs)), 2)
        self.assertTrue(all(Path(d).is_relative_to(group.workspace) for d in dirs))

    async def test_unidentified_group_tasks_have_separate_directories_without_continuity(self):
        group = dataclasses.replace(self.route, kind="group", missing_sender_policy="isolated")
        for context in ("a:task:one", "a:task:two"):
            await self.agent.run(group, {"context": context, "sender": "", "prompt": "task"})
        dirs = [c[1]["cwd"] for c in self.calls if c[0] == "start"]
        self.assertEqual(len(set(dirs)), 2)
        self.assertTrue(all(Path(d).is_relative_to(group.workspace / "tasks") for d in dirs))
        self.assertFalse(any(c[0] == "resume" for c in self.calls))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)

    async def test_unidentified_group_task_is_rejected_if_policy_or_allowlist_forbids_it(self):
        group = dataclasses.replace(self.route, kind="group")
        restricted = dataclasses.replace(group, missing_sender_policy="isolated", allowed_senders=("Alice",))
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
        self.assertEqual(self.agent.effective_instructions(), self.settings.instructions)
