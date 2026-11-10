from __future__ import annotations

import argparse
import asyncio
import dataclasses
import ctypes.util
import importlib.util
import json
import logging
import os
import secrets
import shutil
import signal
import sys
import tempfile
from pathlib import Path

from .config import ConfigError, Conversation, Settings, load_settings


def init_token(settings):
    path = settings.token_file
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print("接口 Token 已存在，已保留。")
        return
    with os.fdopen(fd, "w") as f:
        f.write(secrets.token_urlsafe(48) + "\n")
    print("已生成接口 Token（权限 600），未显示密钥。")


def doctor(settings):
    from .wechat import AtspiWechat, probe_desktop
    missing = [name for name in ("gi", "PIL", "openai_codex", "fastapi", "uvicorn") if importlib.util.find_spec(name) is None]
    result = {"python": sys.version.split()[0], "missing_modules": missing,
              "listen_mode": settings.listen_mode,
              "missing_libraries": [name for name in ("X11", "Xcomposite")
                                    if settings.listen_mode == "independent" and not ctypes.util.find_library(name)],
              "missing_commands": [] if shutil.which("xdotool") else ["xdotool"],
              "desktop_errors": probe_desktop(),
              "api_token_exists": settings.token_file.is_file(),
              "enabled_conversations": [c.id for c in settings.conversations if c.enabled],
              "notes": ["控件树可读不代表群聊提及元数据可读；用 inspect 实测", "doctor 不调用模型，也不发送微信消息"]}
    result["wechat_application"] = {}
    if not result["desktop_errors"] and "gi" not in missing:
        driver = AtspiWechat(dataclasses.replace(settings.wechat_options(), use_events=False))
        try:
            result["wechat_application"] = driver.check_application()
        except Exception as e:
            result["wechat_application"] = {**driver.app_diagnostics, "error": str(e)}
        finally:
            driver.close()
    result["ready"] = not missing and not result["missing_commands"] and not result["missing_libraries"] and not result["desktop_errors"] and not result["wechat_application"].get("error") and result["api_token_exists"]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ready"] else 1


async def demo():
    from .demo import DemoAgent, DemoWechat
    from .domain import Message
    from .service import Bridge
    from .store import Store
    with tempfile.TemporaryDirectory(prefix="wxagentbot-demo-") as tmp:
        root = Path(tmp)
        route = Conversation("demo", "演示联系人", "private", root / "work", True, True)
        settings = Settings(root, root / "config.yml", (route,))
        wechat = DemoWechat()
        bridge = Bridge(settings, store=Store(":memory:"), wechat=wechat, agent=DemoAgent())
        wechat.messages[route.id] = [Message("旧消息", route.name, "incoming")]
        await bridge.scan_once()
        wechat.messages[route.id].append(Message("请整理今天的待办", route.name, "incoming"))
        await bridge.scan_once()
        await bridge.process_one_task()
        await bridge.process_one_delivery()
        await bridge.scan_once()  # Own reply must not trigger another task.
        print(json.dumps({"mode": "simulation", "sent": wechat.sent, "queues": bridge.store.counts()}, ensure_ascii=False, indent=2))
        await bridge.stop()


async def codex_check(settings, run_prompt=False):
    from openai_codex import AsyncCodex, CodexConfig
    async with AsyncCodex(config=CodexConfig(codex_bin=settings.binary)) as client:
        await asyncio.wait_for(client.account(), settings.startup_timeout_seconds)
        # Report only a boolean; never serialize authentication or account data.
        print("Codex SDK 已连接；账号状态已读取。")
        if run_prompt:
            from .codex_agent import CodexAgent
            from .store import Store
            route = Conversation("sdk-test", "SDK 本地自检", "private", settings.root / "workspaces/sdk-test")
            store = Store(":memory:")
            try:
                result = await CodexAgent(settings, store).run(route, {"context": "sdk-test", "sender": "local", "prompt": "不要使用工具，只回复：Codex SDK 已连接。"})
                print(result)
            finally:
                store.close()


async def listen(settings):
    import fcntl
    from .service import Bridge
    settings.database.parent.mkdir(parents=True, exist_ok=True)
    with (settings.database.parent / "bridge.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        bridge = Bridge(settings)
        done = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, done.set)
        await bridge.start()
        try:
            await done.wait()
        finally:
            await bridge.stop()


def main(argv=None):
    parser = argparse.ArgumentParser(description="微信 Codex 任务助手（Linux AT-SPI2）")
    parser.add_argument("--config", default="config.yml")
    sub = parser.add_subparsers(dest="command", required=True)
    for cmd in ("init", "check", "doctor", "demo", "serve", "listen", "codex-check", "codex-test"):
        sub.add_parser(cmd)
    dump = sub.add_parser("dump-tree")
    dump.add_argument("--output", default="data/wechat-tree.json")
    inspect = sub.add_parser("inspect")
    inspect.add_argument("conversation")
    retry = sub.add_parser("retry-message")
    retry.add_argument("id")
    retry.add_argument("--after-restart", action="store_true", help="仅登记，在服务下次启动时重试")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        if args.command == "demo":
            asyncio.run(demo())
            return 0
        settings = load_settings(args.config)
        if args.command == "check":
            print(f"配置有效：{len(settings.conversations)} 个会话，{sum(c.enabled for c in settings.conversations)} 个启用监听。")
        elif args.command == "init":
            init_token(settings)
        elif args.command == "retry-message":
            from .store import Store
            store = Store(settings.database)
            try:
                row = store.retry_delivery(args.id, after_restart=args.after_restart)
                if args.after_restart:
                    print(f"发送记录 {row['id']} 已登记，将在服务下次启动时补发已有结果。")
                else:
                    print(f"发送记录 {row['id']} 已重新入队，将复用已有结果，不会重新执行 Codex。")
            finally:
                store.close()
        elif args.command == "doctor":
            return doctor(settings)
        elif args.command in ("codex-check", "codex-test"):
            asyncio.run(codex_check(settings, args.command == "codex-test"))
        elif args.command == "listen":
            asyncio.run(listen(settings))
        elif args.command == "serve":
            import uvicorn
            from .api import create_app
            uvicorn.run(create_app(settings), host=settings.host, port=settings.port, access_log=False)
        elif args.command in ("dump-tree", "inspect"):
            from .wechat import AtspiWechat
            driver = AtspiWechat(settings.wechat_options())
            try:
                if args.command == "dump-tree":
                    rows = driver.dump_tree()
                    path = (settings.root / args.output).resolve()
                    path.parent.mkdir(parents=True, exist_ok=True)
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    os.fchmod(fd, 0o600)
                    with os.fdopen(fd, "w") as f:
                        json.dump(rows, f, ensure_ascii=False, indent=2)
                    print(f"已保存 {len(rows)} 个节点到 {path}（包含聊天内容，请妥善保管）。")
                else:
                    route = settings.resolve(args.conversation)
                    messages = driver.read(route.wechat_target())
                    print(json.dumps({"messages": [dataclasses.asdict(m) for m in messages], "diagnostics": driver.last_diagnostics}, ensure_ascii=False, indent=2))
            finally:
                driver.close()
        return 0
    except (ConfigError, OSError, RuntimeError, ImportError, TypeError, ValueError) as e:
        print(f"无法执行：{e}", file=sys.stderr)
        if isinstance(e, ImportError):
            print("请在可联网的 Terminal 中运行 ./install.sh。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
