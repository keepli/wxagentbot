import importlib.util
import tempfile
import unittest
from pathlib import Path

from wxagentbot.config import Conversation, Settings
from wxagentbot.demo import DemoAgent, DemoWechat
from wxagentbot.service import Bridge
from wxagentbot.store import Store

available = all(importlib.util.find_spec(x) for x in ("fastapi", "httpx"))


@unittest.skipUnless(available, "需先安装 FastAPI 和 httpx，当前环境无法连接 PyPI")
class HttpApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import httpx
        from wxagentbot.api import create_app
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        token_path = root / "token"
        token_path.write_text("a" * 48)
        token_path.chmod(0o600)
        route = Conversation("a", "Alice", "private", root / "work", False, True)
        settings = Settings(root, root / "config.yml", (route,), token_file=token_path, database=root / "db")
        self.service = Bridge(settings, store=Store(":memory:"), wechat=DemoWechat(), agent=DemoAgent())
        app = create_app(settings, bridge=self.service)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.headers = {"Authorization": "Bearer " + "a" * 48}

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.service.stop()
        self.tmp.cleanup()

    async def test_auth_required(self):
        self.assertEqual((await self.client.get("/v1/status")).status_code, 401)
        self.assertEqual((await self.client.get("/v1/status", headers=self.headers)).status_code, 200)

    async def test_send_idempotency_and_status(self):
        data = {"conversation": "Alice", "message": "hello"}
        headers = self.headers | {"Idempotency-Key": "first"}
        r = await self.client.post("/v1/messages/send", json=data, headers=headers)
        self.assertEqual(r.status_code, 202)
        again = await self.client.post("/v1/messages/send", json=data, headers=headers)
        self.assertEqual(r.json()["id"], again.json()["id"])
        clash = await self.client.post("/v1/messages/send", json=data | {"message": "other"}, headers=headers)
        self.assertEqual(clash.status_code, 409)
        await self.service.process_one_delivery()
        status = await self.client.get(r.json()["status_url"], headers=self.headers)
        self.assertEqual(status.json()["status"], "sent")

    async def test_invalid_body_unknown_chat_and_body_limit(self):
        for value in ([], {"conversation": "unknown", "message": "hi"}, {"conversation": "Alice", "message": " "}):
            r = await self.client.post("/v1/messages/send", json=value, headers=self.headers)
            self.assertEqual(r.status_code, 422)
        huge = await self.client.post("/v1/messages/send", content=b" " * 70000, headers=self.headers)
        self.assertEqual(huge.status_code, 413)
