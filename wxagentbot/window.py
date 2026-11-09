"""Activate and verify the real X11 window before coordinate operations."""
from __future__ import annotations

import re
import shutil
import subprocess


class WindowActivationError(RuntimeError):
    pass


class X11Window:
    def __init__(self, *, runner=subprocess.run, executable=None):
        self.runner = runner
        self.executable = executable or shutil.which("xdotool")

    def _run(self, *args, optional=False):
        if not self.executable:
            raise WindowActivationError("缺少 xdotool，无法自动激活并校验微信窗口")
        try:
            result = self.runner([self.executable, *args], capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise WindowActivationError(f"微信窗口激活失败（{type(e).__name__}）") from e
        if result.returncode:
            if optional:
                return ""
            raise WindowActivationError("窗口管理器未接受微信窗口激活或校验请求")
        return result.stdout.strip()

    def _belongs_to(self, window, pid, title):
        if not window.isdecimal():
            return False
        return (self._run("getwindowpid", window, optional=True) == str(pid)
                and self._run("getwindowname", window, optional=True) == title)

    def find(self, pid: int, title: str):
        """Find a window without mapping, activating, focusing or clicking it."""
        if not isinstance(pid, int) or pid <= 0:
            raise WindowActivationError("微信进程身份无效")
        pattern = "^" + re.escape(title) + "$"
        output = self._run("search", "--all", "--pid", str(pid), "--name", pattern, optional=True)
        matches = [w for w in dict.fromkeys(output.splitlines()) if self._belongs_to(w, pid, title)]
        if len(matches) != 1:
            raise WindowActivationError("无法按进程和标题唯一定位独立聊天窗口")
        return matches[0]

    def ensure_active(self, pid: int, title: str, guard):
        if not isinstance(pid, int) or pid <= 0:
            raise WindowActivationError("微信进程身份无效")
        guard()
        active = self._run("getactivewindow", optional=True)
        if self._belongs_to(active, pid, title):
            return active
        pattern = "^" + re.escape(title) + "$"
        output = self._run("search", "--all", "--onlyvisible", "--pid", str(pid), "--name", pattern, optional=True)
        matches = list(dict.fromkeys(output.splitlines()))
        verified = [w for w in matches if self._belongs_to(w, pid, title)]
        restore = not verified
        if restore:
            # Iconified windows are excluded by --onlyvisible. Locate the same
            # PID/title without that filter, then restore it before activation.
            output = self._run("search", "--all", "--pid", str(pid), "--name", pattern, optional=True)
            matches = list(dict.fromkeys(output.splitlines()))
            verified = [w for w in matches if self._belongs_to(w, pid, title)]
        if len(verified) != 1:
            raise WindowActivationError("无法按进程和标题唯一定位微信主窗口")
        target = verified[0]
        if restore:
            guard()
            self._run("windowmap", "--sync", target)
        guard()
        self._run("windowactivate", "--sync", target)
        guard()
        active = self._run("getactivewindow", optional=True)
        if active != target or not self._belongs_to(active, pid, title):
            raise WindowActivationError("激活后活动窗口并非目标微信，已停止坐标操作")
        return target
