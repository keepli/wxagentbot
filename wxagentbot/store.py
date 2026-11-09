from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

from .config import Conversation
from .domain import HistoryGap, Message, context_key, new_tail, split_reply, task_prompt


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
        """)

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
            delivery = self._enqueue(route, result if reply is None else reply, limit, "task:" + task["id"])
            self.db.execute("UPDATE tasks SET status='completed',result=?,delivery_id=?,updated=? WHERE id=?", (result, delivery["id"], time.time(), task["id"]))

    def finish_task(self, uid: str, status: str, error: str):
        with self.db:
            self.db.execute("UPDATE tasks SET status=?,error=?,updated=? WHERE id=?", (status, error[:500], time.time(), uid))

    def get(self, table: str, uid: str) -> dict | None:
        if table not in ("tasks", "deliveries"):
            raise ValueError("unknown table")
        row = self.db.execute(f"SELECT * FROM {table} WHERE id=?", (uid,)).fetchone()
        return dict(row) if row else None

    def claim(self, table: str) -> dict | None:
        if table not in ("tasks", "deliveries"):
            raise ValueError("unknown table")
        status = "running" if table == "tasks" else "sending"
        with self.db:
            row = self.db.execute(f"SELECT * FROM {table} WHERE status='queued' ORDER BY created,id LIMIT 1").fetchone()
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
