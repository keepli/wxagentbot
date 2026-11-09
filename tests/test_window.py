import subprocess
import unittest

from wxagentbot.window import WindowActivationError, X11Window


class WindowTests(unittest.TestCase):
    def backend(self, *, title="微信", duplicate=False, refuse=False, minimized=False, refuse_restore=False):
        state = {"active": "10", "calls": [], "mapped": not minimized}
        def run(argv, **kwargs):
            args = argv[1:]
            state["calls"].append(args)
            op = args[0]
            output, code = "", 0
            if op == "getactivewindow":
                output = state["active"]
            elif op == "getwindowpid":
                output = "123" if args[1] in ("20", "21") else "999"
            elif op == "getwindowname":
                output = title if args[1] in ("20", "21") else "Terminal"
            elif op == "search":
                if "--onlyvisible" in args and not state["mapped"]:
                    code = 1
                else:
                    output = "20\n21" if duplicate else "20"
            elif op == "windowmap":
                if refuse_restore:
                    code = 1
                else:
                    state["mapped"] = True
            elif op == "windowactivate":
                if not refuse and state["mapped"]:
                    state["active"] = args[-1]
            else:
                self.fail("unexpected command: " + str(args))
            return subprocess.CompletedProcess(argv, code, output, "")
        return X11Window(runner=run, executable="/usr/bin/xdotool"), state

    def test_background_terminal_is_replaced_by_verified_wechat(self):
        window, state = self.backend()
        guards = []
        result = window.ensure_active(123, "微信", lambda: guards.append(True))
        self.assertEqual(result, "20")
        self.assertIn(["windowactivate", "--sync", "20"], state["calls"])
        self.assertGreaterEqual(len(guards), 3)

    def test_already_active_does_not_activate_again(self):
        window, state = self.backend()
        state["active"] = "20"
        self.assertEqual(window.ensure_active(123, "微信", lambda: None), "20")
        self.assertFalse(any(c[0] == "windowactivate" for c in state["calls"]))

    def test_silent_find_never_maps_or_activates_a_window(self):
        window, state = self.backend(minimized=True)
        self.assertEqual(window.find(123, "微信"), "20")
        self.assertFalse(any(c[0] in ("windowmap", "windowactivate") for c in state["calls"]))
        self.assertEqual(state["active"], "10")

    def test_minimized_window_is_restored_before_activation(self):
        window, state = self.backend(minimized=True)
        self.assertEqual(window.ensure_active(123, "微信", lambda: None), "20")
        self.assertTrue(state["mapped"])
        self.assertLess(state["calls"].index(["windowmap", "--sync", "20"]),
                        state["calls"].index(["windowactivate", "--sync", "20"]))

    def test_failed_restore_does_not_attempt_activation(self):
        window, state = self.backend(minimized=True, refuse_restore=True)
        with self.assertRaises(WindowActivationError):
            window.ensure_active(123, "微信", lambda: None)
        self.assertFalse(any(c[0] == "windowactivate" for c in state["calls"]))

    def test_ambiguous_minimized_windows_are_not_restored(self):
        window, state = self.backend(minimized=True, duplicate=True)
        with self.assertRaises(WindowActivationError):
            window.ensure_active(123, "微信", lambda: None)
        self.assertFalse(any(c[0] in ("windowmap", "windowactivate") for c in state["calls"]))

    def test_ambiguous_and_wrong_title_windows_are_rejected(self):
        for kwargs in ({"duplicate": True}, {"title": "其他窗口"}):
            window, state = self.backend(**kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(WindowActivationError):
                window.ensure_active(123, "微信", lambda: None)
            self.assertFalse(any(c[0] == "windowactivate" for c in state["calls"]))

    def test_refused_activation_never_claims_success(self):
        window, _ = self.backend(refuse=True)
        with self.assertRaises(WindowActivationError):
            window.ensure_active(123, "微信", lambda: None)

    def test_human_control_guard_prevents_any_command(self):
        window, state = self.backend()
        def guard():
            raise RuntimeError("human controls desktop")
        with self.assertRaises(RuntimeError):
            window.ensure_active(123, "微信", guard)
        self.assertEqual(state["calls"], [])

    def test_activation_timeout_is_bounded(self):
        def timed_out(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 3)
        window = X11Window(runner=timed_out, executable="/usr/bin/xdotool")
        with self.assertRaises(WindowActivationError):
            window.ensure_active(123, "微信", lambda: None)
