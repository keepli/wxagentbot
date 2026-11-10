import contextlib
import io
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from wxagentbot.cli import doctor
from wxagentbot.wechat import AppLookupError
from wxagentbot.wechat_types import WechatOptions


class DoctorTests(unittest.TestCase):
    def diagnose(self, desktop_errors=(), report=None, error=None):
        with tempfile.TemporaryDirectory() as tmp:
            token = Path(tmp) / "token"
            token.touch()
            settings = types.SimpleNamespace(listen_mode="independent", token_file=token,
                                             conversations=(), wechat_options=lambda: WechatOptions())
            driver = MagicMock()
            driver.app_diagnostics = report or {}
            driver.check_application.return_value = driver.app_diagnostics
            driver.check_application.side_effect = error
            output = io.StringIO()
            with patch("wxagentbot.wechat.probe_desktop", return_value=list(desktop_errors)), \
                    patch("wxagentbot.wechat.AtspiWechat", return_value=driver) as factory, \
                    patch("wxagentbot.cli.importlib.util.find_spec", return_value=object()), \
                    patch("wxagentbot.cli.ctypes.util.find_library", return_value="installed"), \
                    patch("wxagentbot.cli.shutil.which", return_value="xdotool"), \
                    contextlib.redirect_stdout(output):
                code = doctor(settings)
            return code, json.loads(output.getvalue()), driver, factory

    def test_inaccessible_bus_never_enters_native_atspi(self):
        code, report, _, factory = self.diagnose(["Operation not permitted"])
        self.assertEqual(code, 1)
        self.assertFalse(report["ready"])
        factory.assert_not_called()

    def test_lookup_failure_keeps_diagnostics_and_closes_driver(self):
        code, report, driver, _ = self.diagnose(report={"matching_entries": 2},
                                               error=AppLookupError("多个可用微信进程"))
        self.assertEqual(code, 1)
        self.assertEqual(report["wechat_application"]["matching_entries"], 2)
        self.assertIn("多个", report["wechat_application"]["error"])
        driver.close.assert_called_once()

    def test_success_reports_selected_pid_without_event_subscriptions(self):
        code, report, driver, factory = self.diagnose(report={"selected_pid": 42})
        self.assertEqual(code, 0)
        self.assertTrue(report["ready"])
        self.assertEqual(report["wechat_application"]["selected_pid"], 42)
        self.assertFalse(factory.call_args.args[0].use_events)
        driver.close.assert_called_once()
