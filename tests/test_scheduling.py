import asyncio
import dataclasses
import datetime as dt
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from wxagentbot.config import Conversation, Settings
from wxagentbot.demo import DemoWechat
from wxagentbot.domain import Message
from wxagentbot.scheduling import ScheduleError, next_occurrence, parse_schedule, parse_scheduled_result
from wxagentbot.service import Bridge
from wxagentbot.store import Store


ZONE = ZoneInfo("Asia/Shanghai")
NOW = dt.datetime(2026, 11, 9, 13, 47, tzinfo=ZONE).timestamp()


class ScheduleParsingTests(unittest.TestCase):
    def test_create_daily_task_with_natural_language_wrapper_preserves_conditions(self):
        action = "查询广州天河和海珠的天气，如果第二天有下雨，则发送提醒，不下雨不需要，如果第二天的温差有相差10度以上的也提醒一下"
        for prefix in ("帮我设置个每天晚上8点执行的定时任务，", "设置一个定时任务：每天晚上8点，",
                       "创建一个每天20:00运行的任务：", "安排一个在每天20:00执行的定时任务，",
                       "每天20:00的定时任务，"):
            with self.subTest(prefix=prefix):
                plan = parse_schedule(prefix + action, now=NOW)
                self.assertEqual(plan.prompt, action)
                self.assertEqual(plan.recurrence, {"kind": "calendar", "hour": 20, "minute": 0, "second": 0})
                self.assertEqual(dt.datetime.fromtimestamp(plan.due_at, ZONE).isoformat(), "2026-11-09T20:00:00+08:00")

    def test_scheduled_decision_requires_boolean_and_valid_message(self):
        for raw in ('null', '[]', '{}', '{"notify":"false","message":"","reason":""}',
                    '{"notify":true,"message":" ","reason":""}',
                    '{"notify":false,"message":null,"reason":""}',
                    '{"notify":false,"message":"","reason":3}',
                    '{"notify":false,"message":"","reason":"","other":0}',
                    '{"notify":true,"notify":false,"message":"","reason":""}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_scheduled_result(raw)

    def test_explicit_chinese_times_dates_and_relative_delays(self):
        cases = (
            ("13点50分播报广州天气", 180, "播报广州天气"),
            ("请帮我在今天下午一点五十分播报广州天气", 180, "播报广州天气"),
            ("在13:50的时候帮我查天气", 180, "帮我查天气"),
            ("明天13:50播报天气", 86400 + 180, "播报天气"),
            ("周五13:50播报天气", 4 * 86400 + 180, "播报天气"),
            ("下周一13:50播报天气", 7 * 86400 + 180, "播报天气"),
            ("2026年11月10日13:50播报天气", 86400 + 180, "播报天气"),
            ("2026-11-10 13:50播报天气", 86400 + 180, "播报天气"),
            ("十分钟后提醒我喝水", 600, "提醒我喝水"),
            ("半小时后提醒我休息", 1800, "提醒我休息"),
            ("一个小时后提醒我休息", 3600, "提醒我休息"),
            ("一个半小时后提醒我休息", 5400, "提醒我休息"),
            ("1小时30分钟后提醒我休息", 5400, "提醒我休息"),
            ("过10分钟提醒我喝水", 600, "提醒我喝水"),
            ("再过10分钟提醒我喝水", 600, "提醒我喝水"),
            ("5秒后1加1等于几", 5, "1加1等于几"),
            ("提醒我在明天下午一点半喝水", 86400 - 1020, "提醒我喝水"),
        )
        for text, delay, prompt in cases:
            with self.subTest(text=text):
                plan = parse_schedule(text, now=NOW)
                self.assertEqual(plan.due_at, NOW + delay)
                self.assertEqual(plan.prompt, prompt)
                self.assertIsNone(plan.recurrence)

    def test_forecasts_log_timestamps_and_code_requests_are_immediate(self):
        for text in ("查一下广州天气", "明天广州天气怎么样", "查一下明天13:50的天气",
                     "帮我设置Python环境", "帮我创建一个文件夹",
                     "帮我创建一个每天20:00执行的脚本", "创建一个13点50分播报天气的程序",
                     "在广州查天气", "13:50的日志帮我分析一下", "在13:50有什么节目", "10分钟后的广州天气",
                     "帮我写一个13点50分播报天气的程序", '“13点50分播报天气”是什么意思',
                     "解释这段代码：```python\nsleep(30)\n```"):
            with self.subTest(text=text):
                self.assertIsNone(parse_schedule(text, now=NOW))

    def test_invalid_past_missing_or_ambiguous_times_need_clarification(self):
        for text in ("13:00播报天气", "25:00播报天气", "13:99播报天气", "13:500播报天气",
                     "2026-02-30 14:00播报天气", "明天提醒我交费", "每天播报天气",
                     "0秒后提醒我喝水", "13:50", "晚点提醒我喝水", "晚上12点提醒我休息",
                     "每隔5秒提醒我休息"):
            with self.subTest(text=text), self.assertRaises(ScheduleError):
                parse_schedule(text, now=NOW)

    def test_daily_weekly_and_interval_recurrences_skip_missed_occurrences(self):
        daily = parse_schedule("每天下午一点半播报天气", now=NOW)
        self.assertEqual(dt.datetime.fromtimestamp(daily.due_at, ZONE).isoformat(), "2026-11-10T13:30:00+08:00")
        weekly = parse_schedule("每周一早上8点提醒我开会", now=NOW)
        self.assertEqual(dt.datetime.fromtimestamp(weekly.due_at, ZONE).isoformat(), "2026-11-16T08:00:00+08:00")
        interval = parse_schedule("每隔10分钟提醒我喝水", now=NOW)
        self.assertEqual(interval.due_at, NOW + 600)
        self.assertEqual(next_occurrence(interval.recurrence, NOW + 2500, interval.timezone,
                                         previous=interval.due_at), NOW + 3000)

    def test_timezone_is_explicit_and_independent_of_machine_timezone(self):
        plan = parse_schedule("13:50播报天气", now=NOW, timezone="UTC")
        self.assertEqual(dt.datetime.fromtimestamp(plan.due_at, dt.timezone.utc).isoformat(), "2026-11-09T13:50:00+00:00")


class RecordingAgent:
    def __init__(self):
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False

    async def run(self, route, task):
        self.calls.append(dict(task))
        if self.block:
            self.started.set()
            await self.release.wait()
        return "执行结果"


class SchedulingPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.route = Conversation("group", "测试群", "group", root / "group", True,
                                  self_names=("我",), mention_mode="text", missing_sender_policy="shared")
        self.other = Conversation("other", "其他群", "group", root / "other", True,
                                  self_names=("我",), mention_mode="text", missing_sender_policy="shared")
        self.settings = Settings(root, root / "config.yml", (self.route, self.other), database=root / "db",
                                 max_parallel_conversations=1)
        self.now = NOW
        self.agent = RecordingAgent()
        self.ui = DemoWechat()
        self.bridge = Bridge(self.settings, store=Store(self.settings.database), wechat=self.ui,
                             agent=self.agent, clock=lambda: self.now)
        self.histories = {}

    async def asyncTearDown(self):
        await self.bridge.stop()
        self.tmp.cleanup()

    def enqueue(self, text, route=None):
        route = route or self.route
        if route.id not in self.histories:
            self.histories[route.id] = [Message("历史", direction="incoming")]
            self.bridge.store.ingest(route, self.histories[route.id])
        self.histories[route.id].append(Message("@我 " + text, direction="incoming"))
        self.bridge.store.ingest(route, self.histories[route.id])
        return self.bridge.store.db.execute("SELECT id FROM tasks ORDER BY rowid DESC LIMIT 1").fetchone()[0]

    def register(self, text="13点50分播报广州天气"):
        uid = self.enqueue(text)
        self.bridge.process_schedule_requests()
        row = self.bridge.store.db.execute("SELECT id FROM schedules WHERE source_task_id=?", (uid,)).fetchone()
        self.assertIsNotNone(row)
        return uid, self.bridge.store.get_schedule(row[0])

    def state(self, uid):
        return self.bridge.store.get("tasks", uid)["status"]

    async def test_timer_acknowledges_and_same_group_question_runs_before_due(self):
        source, schedule = self.register()
        self.assertEqual(self.state(source), "completed")
        self.assertEqual(schedule["status"], "scheduled")
        self.assertFalse(self.agent.calls)
        await self.bridge.process_one_delivery()
        self.assertIn("已安排", self.ui.sent[0][1])
        self.assertIn("13:50", self.ui.sent[0][1])
        question = self.enqueue("1加1等于几")
        await self.bridge.process_one_task()
        self.assertEqual(self.state(question), "completed")
        self.assertEqual([call["prompt"] for call in self.agent.calls], ["1加1等于几"])
        self.assertFalse(self.bridge.active_jobs)
        self.assertEqual(self.bridge.process_schedules(), 0)

    async def test_conditional_daily_task_silently_completes_then_sends_on_later_match(self):
        _, schedule = self.register("帮我设置个每天晚上8点执行的定时任务，查询广州天河和海珠天气，有雨或温差至少10℃才提醒，否则不提醒")
        await self.bridge.process_one_delivery()  # Registration is always acknowledged.
        outcomes = iter((parse_scheduled_result('{"notify":false,"message":"","reason":"无雨，温差8℃"}'),
                         parse_scheduled_result('{"notify":true,"message":"明天天河有雨，请带伞。","reason":"满足下雨条件"}')))

        class ConditionalAgent:
            async def run(self, route, task):
                self.last_prompt = task["prompt"]
                return next(outcomes)

        self.bridge.agent = ConditionalAgent()
        self.now = schedule["due_at"]
        self.assertEqual(self.bridge.process_schedules(), 1)
        first = self.bridge.store.get_schedule(schedule["id"])["last_task_id"]
        await self.bridge.process_one_task()
        task = self.bridge.store.get("tasks", first)
        self.assertEqual(task["status"], "completed")
        self.assertIsNone(task["delivery_id"])
        self.assertFalse(json.loads(task["result"])["notify"])
        self.assertFalse(await self.bridge.process_one_delivery())
        self.assertEqual(len(self.ui.sent), 1)
        self.assertIn("2026-11-09 20:00:00", self.bridge.agent.last_prompt)
        self.assertIn("北京时间", self.bridge.agent.last_prompt)
        upcoming = self.bridge.store.get_schedule(schedule["id"])
        self.assertEqual(upcoming["status"], "scheduled")
        self.now = upcoming["due_at"]
        self.assertEqual(self.bridge.process_schedules(), 1)
        await self.bridge.process_one_task()
        await self.bridge.process_one_delivery()
        self.assertEqual(self.ui.sent[-1], (self.route.id, "明天天河有雨，请带伞。"))
        self.assertNotIn('"notify"', self.ui.sent[-1][1])

    async def test_one_shot_can_complete_without_notification(self):
        _, schedule = self.register()
        await self.bridge.process_one_delivery()

        class SilentAgent:
            async def run(self, route, task):
                return parse_scheduled_result('{"notify":false,"message":"","reason":"明确通知条件未满足"}')

        self.bridge.agent = SilentAgent()
        self.now = schedule["due_at"]
        self.bridge.process_schedules()
        await self.bridge.process_one_task()
        self.assertEqual(self.bridge.store.get_schedule(schedule["id"])["status"], "completed")
        self.assertFalse(await self.bridge.process_one_delivery())

    async def test_failed_weather_check_is_reported_instead_of_silent(self):
        _, schedule = self.register()
        await self.bridge.process_one_delivery()

        class FailedCheckAgent:
            async def run(self, route, task):
                return parse_scheduled_result('{"notify":true,"message":"天气数据查询失败，无法判断明天是否需要提醒。","reason":"缺少必要数据"}')

        self.bridge.agent = FailedCheckAgent()
        self.now = schedule["due_at"]
        self.bridge.process_schedules()
        await self.bridge.process_one_task()
        await self.bridge.process_one_delivery()
        self.assertIn("查询失败", self.ui.sent[-1][1])

    async def test_background_loops_run_chat_before_timer_and_trigger_automatically(self):
        self.ui.messages = self.histories
        self.bridge.settings = dataclasses.replace(self.settings, schedule_poll_seconds=0.1,
                                                   poll_seconds=0.1, rescan_seconds=0.1)
        source = self.enqueue("1分钟后播报广州天气")
        await self.bridge.start()
        self.assertEqual(self.state(source), "completed")
        schedule = self.bridge.store.list_schedules()[0]
        question = self.enqueue("1加1等于几")
        async with asyncio.timeout(2):
            while self.state(question) != "completed":
                await asyncio.sleep(0.01)
        self.assertEqual(len(self.agent.calls), 1)
        self.assertIn("已安排", self.bridge.store.get("tasks", source)["result"])
        self.now = schedule["due_at"]
        async with asyncio.timeout(2):
            while self.bridge.store.get_schedule(schedule["id"])["status"] != "completed":
                await asyncio.sleep(0.01)
        self.assertEqual(len(self.agent.calls), 2)
        self.assertIn("预约已到期", self.agent.calls[-1]["prompt"])

    async def test_registration_and_ack_bypass_a_busy_same_group_worker(self):
        busy = self.enqueue("耗时任务")
        self.agent.block = True
        process = asyncio.create_task(self.bridge.process_one_task())
        try:
            await asyncio.wait_for(self.agent.started.wait(), 1)
            source, _ = self.register()
            self.assertEqual(self.state(busy), "running")
            self.assertEqual(self.state(source), "completed")
            self.assertEqual(len(self.bridge.active_jobs), 1)
            await self.bridge.process_one_delivery()
            self.assertIn("已安排", self.ui.sent[0][1])
        finally:
            self.agent.release.set()
            await process

    async def test_due_request_is_enqueued_once_and_executes_now(self):
        _, schedule = self.register()
        self.now = schedule["due_at"]
        self.assertEqual(self.bridge.process_schedules(), 1)
        self.assertEqual(self.bridge.process_schedules(), 0)
        triggered = self.bridge.store.get_schedule(schedule["id"])
        task = self.bridge.store.get("tasks", triggered["last_task_id"])
        self.assertEqual(task["prompt"], "播报广州天气")
        self.assertEqual(task["schedule_id"], schedule["id"])
        await self.bridge.process_one_task()
        self.assertIn("预约已到期", self.agent.calls[0]["prompt"])
        self.assertNotIn("13点50分", self.agent.calls[0]["prompt"])
        self.assertEqual(self.bridge.store.get_schedule(schedule["id"])["status"], "completed")
        self.assertEqual(len(self.agent.calls), 1)

    async def test_restart_retains_waiting_schedule_and_does_not_double_dispatch(self):
        _, schedule = self.register()
        await self.bridge.stop()
        self.bridge = Bridge(self.settings, store=Store(self.settings.database), wechat=self.ui,
                             agent=self.agent, clock=lambda: self.now)
        self.bridge.store.recover()
        self.assertEqual(self.bridge.store.get_schedule(schedule["id"])["status"], "scheduled")
        self.now = schedule["due_at"]
        self.assertEqual(self.bridge.process_schedules(), 1)
        self.bridge.store.recover()
        self.assertEqual(self.bridge.process_schedules(), 0)
        await self.bridge.process_one_task()
        self.assertEqual(len(self.agent.calls), 1)

    async def test_registration_and_acknowledgement_are_atomic(self):
        source = self.enqueue("13:50播报天气")
        with patch.object(self.bridge.store, "_enqueue", side_effect=RuntimeError("write failed")):
            with self.assertRaises(RuntimeError):
                self.bridge.process_schedule_requests()
        self.assertEqual(self.state(source), "queued")
        self.assertEqual(self.bridge.store.list_schedules(), [])
        self.bridge.process_schedule_requests()
        self.bridge.process_schedule_requests()
        self.assertEqual(len(self.bridge.store.list_schedules()), 1)
        self.assertEqual(len(self.bridge.store.recent("deliveries")), 1)

    async def test_missed_once_is_reported_without_running_old_weather_query(self):
        _, schedule = self.register()
        self.now = schedule["due_at"] + self.settings.schedule_grace_seconds + 1
        self.assertEqual(self.bridge.process_schedules(), 0)
        self.assertEqual(self.bridge.process_schedules(), 0)
        self.assertEqual(self.bridge.store.get_schedule(schedule["id"])["status"], "missed")
        self.assertEqual(len(self.bridge.store.recent("deliveries")), 2)
        self.assertFalse(await self.bridge.process_one_task())

    async def test_already_queued_occurrence_can_expire_before_model_execution(self):
        _, schedule = self.register()
        self.now = schedule["due_at"]
        self.bridge.process_schedules()
        task_id = self.bridge.store.get_schedule(schedule["id"])["last_task_id"]
        self.now += self.settings.schedule_grace_seconds + 1
        self.assertFalse(await self.bridge.process_one_task())
        self.assertEqual(self.state(task_id), "missed")
        self.assertFalse(self.agent.calls)

    async def test_recurring_missed_times_are_skipped_not_replayed_in_bulk(self):
        _, schedule = self.register("每天13:50播报广州天气")
        self.now = schedule["due_at"] + 2 * 86400 + 600
        self.assertEqual(self.bridge.process_schedules(), 0)
        latest = self.bridge.store.get_schedule(schedule["id"])
        self.assertEqual(latest["status"], "scheduled")
        self.assertGreater(latest["due_at"], self.now)
        self.assertEqual(len(self.bridge.store.recent("tasks")), 1)

    async def test_recurring_job_does_not_accumulate_when_previous_is_still_queued(self):
        _, schedule = self.register("每隔1分钟提醒我喝水")
        self.now = schedule["due_at"]
        self.assertEqual(self.bridge.process_schedules(), 1)
        self.now += 60
        self.assertEqual(self.bridge.process_schedules(), 0)
        self.assertEqual(len(self.bridge.store.recent("tasks")), 2)
        self.assertGreater(self.bridge.store.get_schedule(schedule["id"])["due_at"], self.now)

    async def test_cancellation_and_listing_are_scoped_to_original_conversation(self):
        _, schedule = self.register()
        denied = self.enqueue("/cancel-schedule " + schedule["id"][:8], self.other)
        self.bridge.process_schedule_requests()
        self.assertIn("没有找到", self.bridge.store.get("tasks", denied)["result"])
        self.assertEqual(self.bridge.store.get_schedule(schedule["id"])["status"], "scheduled")
        empty = self.enqueue("/schedules", self.other)
        self.bridge.process_schedule_requests()
        self.assertIn("没有等待执行", self.bridge.store.get("tasks", empty)["result"])
        command = self.enqueue("/cancel-schedule " + schedule["id"][:8])
        self.bridge.process_schedule_requests()
        self.assertIn("已取消", self.bridge.store.get("tasks", command)["result"])
        self.now = schedule["due_at"]
        self.assertEqual(self.bridge.process_schedules(), 0)

    async def test_cancellation_command_bypasses_running_scheduled_task(self):
        _, schedule = self.register()
        self.now = schedule["due_at"]
        self.bridge.process_schedules()
        self.agent.block = True
        process = asyncio.create_task(self.bridge.process_one_task())
        await asyncio.wait_for(self.agent.started.wait(), 1)
        command = self.enqueue("取消定时任务 " + schedule["id"][:8])
        self.bridge.process_schedule_requests()
        await process
        self.assertEqual(self.state(command), "completed")
        self.assertEqual(self.bridge.store.get_schedule(schedule["id"])["status"], "cancelled")
        self.assertFalse(self.bridge.active_jobs)
        self.assertEqual(len(self.bridge.store.recent("deliveries")), 2)

    async def test_changed_group_name_or_permissions_disable_timer(self):
        _, schedule = self.register()
        changed = dataclasses.replace(self.route, missing_sender_policy="ignore")
        self.bridge.settings = dataclasses.replace(self.settings, conversations=(changed, self.other))
        self.now = schedule["due_at"]
        self.assertEqual(self.bridge.process_schedules(), 0)
        self.assertEqual(self.bridge.store.get_schedule(schedule["id"])["status"], "disabled")
        self.assertFalse(self.agent.calls)

    async def test_past_time_requests_clarify_without_calling_model(self):
        source = self.enqueue("13:00播报广州天气")
        self.bridge.process_schedule_requests()
        self.assertEqual(self.state(source), "completed")
        self.assertIn("已经过去", self.bridge.store.get("tasks", source)["result"])
        self.assertEqual(self.bridge.store.list_schedules(), [])
        self.assertFalse(await self.bridge.process_one_task())


class SchedulingMigrationTests(unittest.TestCase):
    def test_old_task_table_is_upgraded_without_losing_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.sqlite3"
            db = sqlite3.connect(path)
            db.executescript("""CREATE TABLE tasks (
                id TEXT PRIMARY KEY, event_key TEXT UNIQUE NOT NULL, conversation TEXT NOT NULL,
                destination TEXT NOT NULL, context TEXT NOT NULL, sender TEXT NOT NULL,
                prompt TEXT NOT NULL, status TEXT NOT NULL, result TEXT, error TEXT,
                delivery_id TEXT, created REAL NOT NULL, updated REAL NOT NULL);
                INSERT INTO tasks VALUES('old','g:1','g','target','ctx','','历史任务','completed','历史结果',NULL,NULL,1,1);
            """)
            db.close()
            store = Store(path)
            try:
                row = store.get("tasks", "old")
                self.assertEqual(row["result"], "历史结果")
                self.assertIsNone(row["schedule_id"])
                self.assertEqual(row["intake_checked"], 0)
                self.assertEqual(store.list_schedules(), [])
            finally:
                store.close()
