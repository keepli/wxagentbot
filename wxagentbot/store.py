from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

from .config import Conversation
from .domain import HistoryGap, Message, context_key, new_tail, split_reply, task_prompt
from .scheduling import SchedulePlan, describe_time


class Conflict(ValueError):
    pass


def destination(route: Conversation) -> str:
    return json.dumps([route.id, route.name, route.kind], ensure_ascii=False)


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        if str(path) != ":memory:":
            Path(path).chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS snapshots (
          conversation TEXT PRIMARY KEY, fingerprints TEXT NOT NULL, seq INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS tasks (
          id TEXT PRIMARY KEY, event_key TEXT UNIQUE NOT NULL,
          conversation TEXT NOT NULL, destination TEXT NOT NULL, context TEXT NOT NULL,
          sender TEXT NOT NULL, prompt TEXT NOT NULL, status TEXT NOT NULL,
          schedule_id TEXT, scheduled_for REAL, intake_checked INTEGER NOT NULL DEFAULT 0,
          result TEXT, error TEXT, delivery_id TEXT, created REAL NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions (context TEXT PRIMARY KEY, thread_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS deliveries (
          id TEXT PRIMARY KEY, conversation TEXT NOT NULL, destination TEXT NOT NULL,
          content TEXT NOT NULL, parts TEXT NOT NULL, next_part INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL, error TEXT, idempotency_key TEXT UNIQUE,
          created REAL NOT NULL, updated REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS tasks_queue ON tasks(status, created);
        CREATE INDEX IF NOT EXISTS deliveries_queue ON deliveries(status, created);
        CREATE TABLE IF NOT EXISTS delivery_retries (
          delivery_id TEXT PRIMARY KEY REFERENCES deliveries(id));
        CREATE TABLE IF NOT EXISTS schedules (
          id TEXT PRIMARY KEY, source_task_id TEXT NOT NULL UNIQUE,
          conversation TEXT NOT NULL, destination TEXT NOT NULL, sender TEXT NOT NULL,
          prompt TEXT NOT NULL, due_at REAL NOT NULL, timezone TEXT NOT NULL,
          recurrence TEXT, status TEXT NOT NULL DEFAULT 'scheduled',
          last_task_id TEXT, last_error TEXT, created REAL NOT NULL, updated REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS schedules_due ON schedules(status, due_at);
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(tasks)")}
        with self.db:
            for name, definition in (("schedule_id", "TEXT"), ("scheduled_for", "REAL"),
                                     ("intake_checked", "INTEGER NOT NULL DEFAULT 0")):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")

    def close(self):
        self.db.close()

    def recover(self):
        # Interrupted coding tasks may already have changed files. Never rerun automatically.
        with self.db:
            self.db.execute("UPDATE tasks SET status='interrupted', error='服务重启，任务未自动重跑', updated=? WHERE status='running'", (time.time(),))
            self.db.execute("UPDATE deliveries SET status='unknown', error='服务在发送过程中中断，请核对微信后处理', updated=? WHERE status='sending'", (time.time(),))
            # Only explicitly requested, still-known-unsent failures are retried.
            self.db.execute("UPDATE deliveries SET status='queued',error=NULL,updated=? WHERE status='failed' AND id IN (SELECT delivery_id FROM delivery_retries)", (time.time(),))
            self.db.execute("DELETE FROM delivery_retries")
            self.db.execute("""UPDATE schedules SET status='interrupted',updated=?
                WHERE status='triggered' AND recurrence IS NULL AND last_task_id IN
                (SELECT id FROM tasks WHERE status='interrupted')""", (time.time(),))

    def ingest(self, route: Conversation, messages: list[Message]) -> tuple[int, str | None]:
        if not messages:
            return 0, None
        fingerprints = [m.fingerprint() for m in messages]
        warning = None
        with self.db:
            old = self.db.execute("SELECT * FROM snapshots WHERE conversation=?", (route.id,)).fetchone()
            seq = old["seq"] if old else 0
            indices = []
            if old:
                try:
                    indices = new_tail(json.loads(old["fingerprints"]), fingerprints)
                except HistoryGap as e:
                    warning = str(e)
            created = 0
            for index in indices:
                seq += 1
                message = messages[index]
                prompt = task_prompt(route, message)
                if not prompt:
                    continue
                now = time.time()
                event_key = f"{route.id}:{seq}"
                self.db.execute("INSERT INTO tasks(id,event_key,conversation,destination,context,sender,prompt,status,created,updated) VALUES(?,?,?,?,?,?,?,'queued',?,?)", (
                    uuid.uuid4().hex, event_key, route.id, destination(route), context_key(route, message, event_key=event_key),
                    message.sender, prompt, now, now))
                created += 1
            self.db.execute("INSERT INTO snapshots VALUES(?,?,?) ON CONFLICT(conversation) DO UPDATE SET fingerprints=excluded.fingerprints, seq=excluded.seq", (route.id, json.dumps(fingerprints), seq))
        return created, warning

    def enqueue(self, route: Conversation, text: str, limit: int, key: str | None = None) -> dict:
        with self.db:
            return self._enqueue(route, text, limit, key)

    def _enqueue(self, route, text, limit, key):
        if key:
            existing = self.db.execute("SELECT * FROM deliveries WHERE idempotency_key=?", (key,)).fetchone()
            if existing:
                if existing["destination"] != destination(route) or existing["content"] != text:
                    raise Conflict("同一个 Idempotency-Key 已用于其他会话或消息")
                return dict(existing)
        now, uid = time.time(), uuid.uuid4().hex
        self.db.execute("INSERT INTO deliveries(id,conversation,destination,content,parts,status,idempotency_key,created,updated) VALUES(?,?,?,?,?,'queued',?,?,?)", (
            uid, route.id, destination(route), text, json.dumps(split_reply(text, limit), ensure_ascii=False), key, now, now))
        return self.get("deliveries", uid)

    def complete_task(self, task: dict, route: Conversation, result: str, limit: int, *, reply: str | None = None):
        # Persist final response and outbound message in one transaction.
        with self.db:
            if task.get("schedule_id"):
                schedule = self.get_schedule(task["schedule_id"])
                if schedule["status"] in ("cancelled", "disabled"):
                    self.finish_task(task["id"], "cancelled", "预约已经取消或停用，结果未发送")
                    return
            delivery = self._enqueue(route, result if reply is None else reply, limit, "task:" + task["id"])
            self.db.execute("UPDATE tasks SET status='completed',result=?,delivery_id=?,updated=? WHERE id=?", (result, delivery["id"], time.time(), task["id"]))
            self.db.execute("""UPDATE schedules SET status='completed',updated=?
                WHERE id=? AND recurrence IS NULL AND status='triggered'""", (time.time(), task.get("schedule_id")))

    def finish_task(self, uid: str, status: str, error: str):
        with self.db:
            self.db.execute("UPDATE tasks SET status=?,error=?,updated=? WHERE id=?", (status, error[:500], time.time(), uid))
            if status in ("failed", "cancelled", "interrupted", "missed"):
                self.db.execute("""UPDATE schedules SET status=?,last_error=?,updated=?
                    WHERE id=(SELECT schedule_id FROM tasks WHERE id=?)
                    AND recurrence IS NULL AND status='triggered'""", (status, error[:500], time.time(), uid))

    def pending_intake(self):
        return [dict(row) for row in self.db.execute("""SELECT * FROM tasks
            WHERE status='queued' AND intake_checked=0 AND schedule_id IS NULL ORDER BY created,rowid""")]

    def intake_checked(self, uid):
        with self.db:
            self.db.execute("UPDATE tasks SET intake_checked=1 WHERE id=?", (uid,))

    def register_schedule(self, task: dict, route: Conversation, plan: SchedulePlan, limit: int):
        with self.db:
            existing = self.db.execute("SELECT id FROM schedules WHERE source_task_id=?", (task["id"],)).fetchone()
            if existing:
                return self.get_schedule(existing[0])
            uid, now = uuid.uuid4().hex, time.time()
            self.db.execute("""INSERT INTO schedules
                (id,source_task_id,conversation,destination,sender,prompt,due_at,timezone,recurrence,created,updated)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (uid, task["id"], route.id, destination(route), task["sender"],
                plan.prompt, plan.due_at, plan.timezone,
                json.dumps(plan.recurrence) if plan.recurrence else None, now, now))
            label = "已安排重复任务，首次执行" if plan.recurrence else "已安排"
            acknowledgement = (f"{label}：{describe_time(plan.due_at, plan.timezone)}。\n"
                               f"任务：{plan.prompt}\n预约编号：{uid[:8]}\n"
                               f"取消请发送：/cancel-schedule {uid[:8]}")
            self.complete_task(task, route, acknowledgement, limit)
            return self.get_schedule(uid)

    def get_schedule(self, uid: str, conversation: str | None = None):
        sql, args = "SELECT * FROM schedules WHERE substr(id,1,?)=?", [len(uid), uid]
        if conversation is not None:
            sql += " AND conversation=?"
            args.append(conversation)
        rows = self.db.execute(sql, args).fetchall()
        if not rows:
            raise KeyError(uid)
        if len(rows) != 1:
            raise Conflict("预约编号不唯一，请使用更长的编号")
        return dict(rows[0])

    def list_schedules(self, conversation: str | None = None, *, active_only=False):
        conditions, args = [], []
        if conversation is not None:
            conditions.append("conversation=?")
            args.append(conversation)
        if active_only:
            conditions.append("status IN ('scheduled','triggered')")
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM schedules" + where + " ORDER BY due_at,created LIMIT 200", args)]

    def due_schedules(self, now: float):
        return [dict(row) for row in self.db.execute("""SELECT * FROM schedules
            WHERE status='scheduled' AND due_at<=? ORDER BY due_at,created LIMIT 100""", (now,))]

    def stale_scheduled_tasks(self, now: float, grace: float):
        return [dict(row) for row in self.db.execute("""SELECT * FROM tasks
            WHERE status='queued' AND schedule_id IS NOT NULL AND scheduled_for<?""", (now - grace,))]

    def expire_scheduled_task(self, task: dict, route: Conversation | None, limit: int):
        error, now = "预约已超过执行时间容忍范围，本次未补跑", time.time()
        with self.db:
            changed = self.db.execute("""UPDATE tasks SET status='missed',error=?,updated=?
                WHERE id=? AND status='queued'""", (error, now, task["id"]))
            if not changed.rowcount:
                return
            self.db.execute("""UPDATE schedules SET status='missed',last_error=?,updated=?
                WHERE id=? AND recurrence IS NULL AND status='triggered'""", (error, now, task["schedule_id"]))
            if route is not None:
                self._enqueue(route, f"预约 {task['schedule_id'][:8]} 已错过执行时间，本次未补跑。",
                              limit, "schedule:missed-task:" + task["id"])

    def dispatch_schedule(self, schedule: dict, route: Conversation, now: float, next_due: float | None):
        with self.db:
            current = self.get_schedule(schedule["id"])
            if (current["status"] != "scheduled" or current["due_at"] > now
                    or current["due_at"] != schedule["due_at"]):
                return None
            previous = self.get("tasks", current["last_task_id"]) if current["last_task_id"] else None
            if previous and previous["status"] in ("queued", "running"):
                self.db.execute("UPDATE schedules SET due_at=?,last_error=?,updated=? WHERE id=?", (
                    next_due if next_due is not None else current["due_at"],
                    "上一次任务仍未结束，已合并本次触发", now, current["id"]))
                return None
            uid = uuid.uuid4().hex
            event_key = f"schedule:{current['id']}:{current['due_at']:.6f}"
            context = f"{route.id}:{event_key}"
            self.db.execute("""INSERT INTO tasks
                (id,event_key,conversation,destination,context,sender,prompt,status,schedule_id,scheduled_for,intake_checked,created,updated)
                VALUES(?,?,?,?,?,?,?,'queued',?,?,1,?,?)""", (uid, event_key, route.id, destination(route),
                context, current["sender"], current["prompt"], current["id"], current["due_at"], now, now))
            self.db.execute("""UPDATE schedules SET due_at=?,status=?,last_task_id=?,last_error=NULL,updated=? WHERE id=?""", (
                next_due if next_due is not None else current["due_at"],
                "scheduled" if next_due is not None else "triggered", uid, now, current["id"]))
            return self.get("tasks", uid)

    def skip_schedule(self, schedule: dict, route: Conversation | None, now: float,
                      next_due: float | None, *, disabled=False, limit=1500):
        with self.db:
            current = self.get_schedule(schedule["id"])
            if current["status"] != "scheduled" or current["due_at"] != schedule["due_at"]:
                return
            error = "会话已停用或身份/权限发生变化，预约已停用" if disabled else "已错过执行时间，未补跑"
            self.db.execute("UPDATE schedules SET status=?,due_at=?,last_error=?,updated=? WHERE id=?", (
                "disabled" if disabled else "scheduled" if next_due is not None else "missed",
                next_due if next_due is not None else current["due_at"], error, now, current["id"]))
            if route is not None and not disabled:
                notice = (f"预约 {current['id'][:8]} 已错过执行时间："
                          f"{describe_time(current['due_at'], current['timezone'])}，本次未补跑。")
                self._enqueue(route, notice, limit, f"schedule:missed:{current['id']}:{current['due_at']:.6f}")

    def cancel_schedule(self, uid: str, conversation: str | None = None):
        with self.db:
            schedule = self.get_schedule(uid, conversation)
            if schedule["status"] not in ("scheduled", "triggered"):
                raise Conflict("预约已经结束或取消")
            self.db.execute("UPDATE schedules SET status='cancelled',updated=? WHERE id=?", (time.time(), schedule["id"]))
            self.db.execute("""UPDATE tasks SET status='cancelled',error='预约已取消',updated=?
                WHERE schedule_id=? AND status='queued'""", (time.time(), schedule["id"]))
            return self.get_schedule(schedule["id"])

    def schedule_counts(self):
        return {row[0]: row[1] for row in self.db.execute("SELECT status,count(*) FROM schedules GROUP BY status")}

    def get(self, table: str, uid: str) -> dict | None:
        if table not in ("tasks", "deliveries"):
            raise ValueError("unknown table")
        row = self.db.execute(f"SELECT * FROM {table} WHERE id=?", (uid,)).fetchone()
        return dict(row) if row else None

    def claim(self, table: str) -> dict | None:
        if table not in ("tasks", "deliveries"):
            raise ValueError("unknown table")
        status = "running" if table == "tasks" else "sending"
        # Each conversation has one execution lane, including groups with
        # different senders or isolated per-message contexts. Skip its queued
        # work so a busy conversation cannot block the other conversations.
        available = """
            AND NOT EXISTS (
                SELECT 1 FROM tasks AS active
                WHERE active.conversation = candidate.conversation
                  AND active.status = 'running')
        """ if table == "tasks" else ""
        with self.db:
            row = self.db.execute(f"SELECT * FROM {table} AS candidate WHERE status='queued' {available} ORDER BY created,rowid LIMIT 1").fetchone()
            if not row:
                return None
            self.db.execute(f"UPDATE {table} SET status=?,updated=? WHERE id=?", (status, time.time(), row["id"]))
        return self.get(table, row["id"])

    def part_sent(self, uid: str):
        with self.db:
            row = self.get("deliveries", uid)
            next_part = row["next_part"] + 1
            status = "sent" if next_part == len(json.loads(row["parts"])) else "queued"
            self.db.execute("UPDATE deliveries SET next_part=?,status=?,error=NULL,updated=? WHERE id=?", (next_part, status, time.time(), uid))

    def delivery_error(self, uid: str, status: str, error: str):
        with self.db:
            self.db.execute("UPDATE deliveries SET status=?,error=?,updated=? WHERE id=?", (status, error[:500], time.time(), uid))

    def retry_delivery(self, uid: str, *, after_restart=False):
        with self.db:
            row = self.get("deliveries", uid)
            if row is None:
                raise KeyError(uid)
            if row["status"] != "failed":
                raise Conflict("仅能重试确认尚未发送的 failed 记录；unknown 需先核对微信")
            if after_restart:
                self.db.execute("INSERT OR IGNORE INTO delivery_retries VALUES(?)", (uid,))
            else:
                self.db.execute("UPDATE deliveries SET status='queued',error=NULL,updated=? WHERE id=? AND status='failed'", (time.time(), uid))
                self.db.execute("DELETE FROM delivery_retries WHERE delivery_id=?", (uid,))
        return self.get("deliveries", uid)

    def thread_id(self, context: str) -> str | None:
        row = self.db.execute("SELECT thread_id FROM sessions WHERE context=?", (context,)).fetchone()
        return row[0] if row else None

    def save_thread(self, context: str, thread_id: str):
        with self.db:
            self.db.execute("INSERT INTO sessions VALUES(?,?) ON CONFLICT(context) DO UPDATE SET thread_id=excluded.thread_id", (context, thread_id))

    def reset(self, conversation: str):
        with self.db:
            # substr uses literal matching: route ids may contain SQL wildcards.
            self.db.execute("DELETE FROM sessions WHERE context=? OR substr(context,1,?)=?", (conversation, len(conversation) + 1, conversation + ":"))

    def recent(self, table: str, limit: int = 30) -> list[dict]:
        if table not in ("tasks", "deliveries"):
            raise ValueError("unknown table")
        return [dict(row) for row in self.db.execute(f"SELECT * FROM {table} ORDER BY created DESC LIMIT ?", (limit,))]

    def counts(self) -> dict:
        return {table: {r[0]: r[1] for r in self.db.execute(f"SELECT status,count(*) FROM {table} GROUP BY status")} for table in ("tasks", "deliveries")}
