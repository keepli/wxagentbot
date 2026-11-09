from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib
from pathlib import Path

from .config import Conversation, Settings
from .store import Store
from .formatting import PLAIN_TEXT_RULES
from .scheduling import (EXECUTION_RULES, SCHEDULE_OUTPUT_SCHEMA, SCHEDULE_RESULT_RULES,
                         ScheduledResult, parse_scheduled_result)


class CodexAgent:
    """Uses the official SDK, never scrapes CLI terminal output."""

    def __init__(self, settings: Settings, store: Store):
        self.settings, self.store = settings, store

    def effective_instructions(self) -> str:
        parts = [self.settings.instructions]
        if self.settings.instructions_file is not None:
            parts.append(self.settings.instructions_file.read_text(encoding="utf-8"))
        if self.settings.reply_format == "plain":
            parts.append(PLAIN_TEXT_RULES)
        parts.append(EXECUTION_RULES)
        return "\n\n".join(p.strip() for p in parts if p.strip())

    def session_key(self, context: str, workspace: Path, *, instructions=None) -> str:
        instructions = self.effective_instructions() if instructions is None else instructions
        policy = f"{workspace}:{self.settings.sandbox}:{self.settings.model}:{instructions}"
        return context + ":" + hashlib.sha256(policy.encode()).hexdigest()[:16]

    async def run(self, route: Conversation, task: dict) -> str | ScheduledResult:
        try:
            sdk = importlib.import_module("openai_codex")
        except ImportError as e:
            raise RuntimeError("缺少官方 openai-codex SDK，请先运行 ./install.sh") from e
        route.workspace.mkdir(parents=True, exist_ok=True)
        # All members of a group share its context and working directory.
        cwd = route.workspace
        if route.kind == "group" and not task["sender"]:
            if route.missing_sender_policy not in ("shared", "isolated") or route.allowed_senders:
                raise ValueError("未识别发送人的群任务不符合当前会话策略")
        s = self.settings
        instructions = self.effective_instructions()
        scheduled = bool(task.get("schedule_id"))
        if scheduled:
            instructions += "\n\n" + SCHEDULE_RESULT_RULES
        continuity = s.continuity
        config = sdk.CodexConfig(codex_bin=s.binary, cwd=str(cwd), client_name="wxagentbot")
        client = sdk.AsyncCodex(config=config)
        turn = None
        try:
            await asyncio.wait_for(client.__aenter__(), s.startup_timeout_seconds)
            options = dict(cwd=str(cwd), sandbox=getattr(sdk.Sandbox, s.sandbox),
                           approval_mode=getattr(sdk.ApprovalMode, s.approval_mode),
                           developer_instructions=instructions)
            if s.model:
                options["model"] = s.model
            if s.reasoning_effort is not None:
                # Override only this SDK thread, including resumed conversations.
                options["config"] = {"model_reasoning_effort": s.reasoning_effort}
            # Also normalize queued tasks created by older member/task routing.
            # Scheduled occurrences retain their independent execution context.
            context = route.id if route.kind == "group" and not task.get("schedule_id") else task["context"]
            key = self.session_key(context, cwd, instructions=instructions)
            saved = self.store.thread_id(key) if continuity else None
            async with asyncio.timeout(s.timeout_seconds):
                if saved:
                    thread = await client.thread_resume(saved, include_turns=False, **options)
                else:
                    thread = await client.thread_start(**options)
                if continuity:
                    self.store.save_thread(key, thread.id)
                # The configured conversation authorizes the task; quoted material
                # and fetched documents retain their lower authority in Codex.
                turn_options = {"output_schema": SCHEDULE_OUTPUT_SCHEMA} if scheduled else {}
                turn = await thread.turn(task["prompt"], **turn_options)
                result = await turn.run()
            final = result.final_response
            if not isinstance(final, str) or not final.strip():
                raise RuntimeError("Codex 没有返回最终文本，请查看本地任务日志")
            return parse_scheduled_result(final.strip()) if scheduled else final.strip()
        except (TimeoutError, asyncio.CancelledError):
            if turn is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(turn.interrupt(), 5)
            raise
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.close(), 5)
