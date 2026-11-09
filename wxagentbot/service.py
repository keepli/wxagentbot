from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import dataclasses
import json
import logging
import time

from .codex_agent import CodexAgent
from .config import ConfigError, Settings, load_settings
from .store import Conflict, Store, destination
from .wechat import AtspiWechat, DesktopBusy, NotSent
from .formatting import wechat_plain_text

log = logging.getLogger(__name__)


class Bridge:
    def __init__(self, settings: Settings, *, store=None, wechat=None, agent=None):
        self.settings = settings
        self.store = store if store is not None else Store(settings.database)
        self.wechat = wechat if wechat is not None else AtspiWechat(settings.wechat_options())
        self.agent = agent if agent is not None else CodexAgent(settings, self.store)
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="wechat-ui")
        self.ui_lock = asyncio.Lock()
        self.runners = []
        self.current_job = None
        self.current_task_id = None
        self.stopping = False
        self.desktop_status = "not_checked"
        self.desktop_error = None
        self.scan_warnings = {}
        self.scan_errors = {}
        self.windows_prepared = settings.listen_mode != "independent"
        self.window_setup_error = None

    async def ui(self, method, *args):
        # A timed-out to_thread cannot be stopped safely. The driver bounds its
        # own AT-SPI calls; hold this lock until the real operation has ended.
        async with self.ui_lock:
            future = asyncio.get_running_loop().run_in_executor(self.executor, method, *args)
            try:
                while not future.done():
                    # Some restricted runtimes cannot signal the event loop's
                    # wake socket from a worker; a short timer also wakes it.
                    await asyncio.wait((future,), timeout=0.1)
                return future.result()
            except asyncio.CancelledError:
                while not future.done():
                    await asyncio.wait((future,), timeout=0.1)
                raise

    async def start(self):
        self.store.recover()
        self.runners = [asyncio.create_task(fn(), name=name) for name, fn in (
            ("watch", self.watch_loop), ("codex", self.task_loop), ("send", self.send_loop))]

    async def stop(self):
        self.stopping = True
        for task in self.runners:
            task.cancel()
        await asyncio.gather(*self.runners, return_exceptions=True)
        await self.ui(self.wechat.close)
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.store.close()

    async def scan_once(self):
        for route in self.settings.conversations:
            if not route.enabled:
                continue
            try:
                messages = await self.ui(self.wechat.read, route.wechat_target())
                created, warning = self.store.ingest(route, messages)
                self.desktop_status, self.desktop_error = "connected", None
                self.scan_errors.pop(route.id, None)
                if warning:
                    self.scan_warnings[route.id] = warning
                else:
                    self.scan_warnings.pop(route.id, None)
                if created:
                    log.info("queued %d task(s) for %s", created, route.id)
            except DesktopBusy as e:
                self.scan_errors[route.id] = str(e)
                if self.desktop_status != "paused":
                    log.info("desktop paused: %s", e)
                self.desktop_status, self.desktop_error = "paused", str(e)
            except Exception as e:
                self.desktop_status = "degraded"
                self.desktop_error = f"{type(e).__name__}: {e}"
                self.scan_errors[route.id] = self.desktop_error
                log.warning("scan failed for %s: %s", route.id, self.desktop_error)
        if self.scan_errors and self.desktop_status == "connected":
            self.desktop_status = "degraded"
            self.desktop_error = "部分会话读取失败，请检查 scan_errors"

    async def watch_loop(self):
        last_scan = 0.0
        pending_change = False
        while True:
            routes = [c for c in self.settings.conversations if c.enabled]
            if routes:
                try:
                    if not self.windows_prepared:
                        if self.window_setup_error:
                            self.desktop_status, self.desktop_error = "degraded", self.window_setup_error
                            await asyncio.sleep(self.settings.poll_seconds)
                            continue
                        self.desktop_status = "preparing_windows"
                        try:
                            await self.ui(self.wechat.prepare_windows, [r.wechat_target() for r in routes])
                        except DesktopBusy:
                            raise
                        except Exception as e:
                            self.window_setup_error = f"{type(e).__name__}: {e}"
                            raise
                        self.windows_prepared = True
                        log.info("independent windows prepared; background reads will not activate them")
                    dirty = await self.ui(self.wechat.pump)
                    pending_change = pending_change or dirty
                    now = time.monotonic()
                    interval = self.settings.rescan_seconds if self.settings.use_events else self.settings.poll_seconds
                    if (pending_change and now - last_scan >= self.settings.poll_seconds) or now - last_scan >= interval:
                        await self.scan_once()
                        last_scan = time.monotonic()
                        pending_change = False
                except DesktopBusy as e:
                    self.desktop_status, self.desktop_error = "paused", str(e)
                    await asyncio.sleep(self.settings.poll_seconds)
                except Exception as e:
                    self.desktop_status = "degraded"
                    self.desktop_error = f"{type(e).__name__}: {e}"
                    await asyncio.sleep(self.settings.poll_seconds)
            else:
                self.desktop_status = "idle"
            await asyncio.sleep(min(0.5, self.settings.poll_seconds))

    async def execute(self, task):
        route = None
        try:
            route = self.settings.resolve(task["conversation"])
            if not route.enabled or destination(route) != task["destination"]:
                raise ConfigError("会话配置已变更，任务未执行")
            if task["prompt"] == "/new":
                self.store.reset(task["context"])
                result = "已重置当前对话的任务上下文。"
            else:
                result = await self.agent.run(route, task)
            reply = wechat_plain_text(result) if self.settings.reply_format == "plain" else result
            self.store.complete_task(task, route, result, self.settings.max_reply_chars, reply=reply)
        except asyncio.CancelledError:
            self.store.finish_task(task["id"], "interrupted" if self.stopping else "cancelled", "任务已停止；已发生的文件修改不会自动回滚")
            raise
        except Exception as e:
            error = "任务超时" if isinstance(e, TimeoutError) else f"任务执行失败（{type(e).__name__}）"
            self.store.finish_task(task["id"], "failed", error)
            log.warning("task %s failed: %s", task["id"], type(e).__name__)
            if route is not None and route.enabled and destination(route) == task["destination"]:
                self.store.enqueue(route, f"{error}。任务编号：{task['id']}。请检查本地诊断或任务记录。", self.settings.max_reply_chars, "error:" + task["id"])

    async def process_one_task(self):
        task = self.store.claim("tasks")
        if not task:
            return False
        self.current_task_id = task["id"]
        self.current_job = asyncio.create_task(self.execute(task))
        try:
            await self.current_job
        except asyncio.CancelledError:
            if self.stopping:
                raise
        finally:
            self.current_job = self.current_task_id = None
        return True

    async def task_loop(self):
        while True:
            if not await self.process_one_task():
                await asyncio.sleep(0.3)

    async def process_one_delivery(self):
        delivery = self.store.claim("deliveries")
        if not delivery:
            return False
        try:
            route = self.settings.resolve(delivery["conversation"])
            if destination(route) != delivery["destination"] or not (route.enabled or route.allow_push):
                raise NotSent("会话配置已更改或已禁用")
            text = json.loads(delivery["parts"])[delivery["next_part"]]
            await self.ui(self.wechat.send, route.wechat_target(), text)
            self.store.part_sent(delivery["id"])
        except asyncio.CancelledError:
            self.store.delivery_error(delivery["id"], "unknown", "服务在发送期间停止，请核对聊天")
            raise
        except DesktopBusy as e:
            self.store.delivery_error(delivery["id"], "queued", str(e))
            await asyncio.sleep(self.settings.poll_seconds)
        except (NotSent, ConfigError) as e:
            self.store.delivery_error(delivery["id"], "failed", str(e))
        except Exception as e:
            # Unknown adapter errors are conservatively treated as possibly sent.
            self.store.delivery_error(delivery["id"], "unknown", f"发送结果不确定（{type(e).__name__}），请核对微信")
        return True

    async def send_loop(self):
        while True:
            if not await self.process_one_delivery():
                await asyncio.sleep(0.3)

    def enqueue_push(self, conversation: str, message: str, key: str | None = None):
        if not isinstance(conversation, str) or not isinstance(message, str) or not message.strip():
            raise ConfigError("conversation 和 message 必须是非空字符串")
        if len(message) > 16000 or len(conversation) > 200:
            raise ConfigError("消息最多 16000 字，会话名称最多 200 字")
        if key is not None and (not isinstance(key, str) or not 1 <= len(key) <= 128):
            raise ConfigError("Idempotency-Key 长度必须在 1..128")
        if key and key.startswith(("task:", "error:")):
            raise ConfigError("Idempotency-Key 使用了保留前缀")
        route = self.settings.resolve(conversation)
        if not route.allow_push:
            raise ConfigError("该会话尚未开启 allow_push")
        return self.store.enqueue(route, message, self.settings.max_reply_chars, key)

    def cancel(self, uid):
        task = self.store.get("tasks", uid)
        if not task:
            raise KeyError(uid)
        if uid == self.current_task_id and self.current_job:
            self.current_job.cancel()
        elif task["status"] == "queued":
            self.store.finish_task(uid, "cancelled", "任务已取消")
        else:
            raise Conflict("任务已经结束或不在可取消状态")

    def reload(self):
        new = load_settings(self.settings.path)
        before, after = dataclasses.asdict(self.settings), dataclasses.asdict(new)
        before.pop("conversations")
        after.pop("conversations")
        if before != after:
            raise ConfigError("运行中只支持重载 conversations，其他设置请重启服务")
        if self.current_task_id:
            raise Conflict("当前有任务执行中，请完成或取消后重载")
        old_routes = {c.id: c for c in self.settings.conversations}
        # Do not permit queued work to move into a new workspace or recipient.
        pending = self.store.recent("tasks", 10000)
        for task in pending:
            if task["status"] != "queued":
                continue
            try:
                if new.resolve(task["conversation"]) != old_routes.get(task["conversation"]):
                    raise Conflict("被修改的会话仍有排队任务，请先取消")
            except ConfigError as e:
                raise Conflict("被删除的会话仍有排队任务，请先取消") from e
        self.settings = new
        enabled = {c.id for c in new.conversations if c.enabled}
        self.scan_errors = {k: v for k, v in self.scan_errors.items() if k in enabled}
        self.scan_warnings = {k: v for k, v in self.scan_warnings.items() if k in enabled}
        if new.listen_mode == "independent":
            self.windows_prepared = False
            self.window_setup_error = None
        return len(new.conversations)

    def status(self):
        return {"desktop": self.desktop_status, "desktop_error": self.desktop_error,
                "active_task": self.current_task_id, "queues": self.store.counts(),
                "watching": [c.id for c in self.settings.conversations if c.enabled],
                "scan_warnings": self.scan_warnings,
                "scan_errors": self.scan_errors, "listen_mode": self.settings.listen_mode,
                "windows_prepared": self.windows_prepared,
                "wechat_diagnostics": self.wechat.last_diagnostics}
