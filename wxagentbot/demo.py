"""Explicit in-memory simulation. Never selected by the production commands."""
from .domain import Message


class DemoWechat:
    def __init__(self):
        self.messages = {}
        self.sent = []
        self.last_diagnostics = {}

    def pump(self):
        return True

    def read(self, route):
        return list(self.messages.get(route.id, []))

    def send(self, route, text):
        self.sent.append((route.id, text))
        self.messages.setdefault(route.id, []).append(Message(text, "我", "outgoing"))

    def close(self):
        pass


class DemoAgent:
    async def run(self, route, task):
        return "演示任务已完成：" + task["prompt"]
