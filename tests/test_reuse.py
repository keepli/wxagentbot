import subprocess
import sys
import unittest


class StandaloneTransportTests(unittest.TestCase):
    def test_transport_imports_and_initializes_without_business_dependencies(self):
        script = '''
import importlib.abc, sys
class BlockBusiness(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {"openai_codex", "fastapi", "uvicorn", "sqlite3", "yaml", "wxagentbot.config", "wxagentbot.store", "wxagentbot.codex_agent"}:
            raise AssertionError("business dependency imported: " + fullname)
sys.meta_path.insert(0, BlockBusiness())
from wxagentbot import WechatClient, WechatOptions, ChatTarget, Message
client = WechatClient(WechatOptions())
chat = ChatTarget("friend", "好友")
assert chat.name == "好友"
assert Message("正文").text == "正文"
assert not hasattr(client.settings, "database")
assert not hasattr(client.settings, "model")
client.close()
'''
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
