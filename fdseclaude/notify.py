"""系统通知模式：窗口焦点检测 + 跨平台系统通知 + 点击聚焦终端。

焦点检测原理：获取前台窗口所属进程的 PID，若该 PID 出现在本进程的祖先链中
（终端 / IDE / shell 都是祖先），则认为"当前终端或其父窗口"持有焦点。
IDE 内置终端场景下，IDE 主窗口进程同样是祖先，因此天然支持。
"""
import base64
import logging
import re
import sys
from typing import Dict, List, Optional
from xml.sax.saxutils import escape

from .config import PROTOCOL_NAME
from .utils import IS_MAC, IS_WIN, ancestor_pids, run, which_or_none

if IS_WIN:
    import ctypes
    import ctypes.wintypes as wintypes


# ---------------- 焦点检测 ----------------

def _foreground_pid() -> Optional[int]:
    if IS_WIN:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value or None
    if IS_MAC:
        rc, out = run(
            ["osascript", "-e",
             'tell application "System Events" to get unix id of first process whose frontmost is true'],
            timeout=10,
        )
        if rc == 0 and out.strip().isdigit():
            return int(out.strip())
        return None
    # Linux：依赖 xdotool（无则视为失焦，宁可多通知）
    if which_or_none("xdotool"):
        rc, out = run(["xdotool", "getactivewindow", "getwindowpid"], timeout=10)
        m = re.search(r"\d+", out)
        if rc == 0 and m:
            return int(m.group())
    return None


def is_our_terminal_focused() -> bool:
    fg = _foreground_pid()
    if fg is None:
        return False
    return fg in set(ancestor_pids())


# ---------------- Windows: 查找终端窗口句柄 / 聚焦 ----------------

def _win_windows_by_pid() -> Dict[int, List[int]]:
    user32 = ctypes.windll.user32
    result: Dict[int, List[int]] = {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            result.setdefault(pid.value, []).append(int(hwnd))
        return True

    user32.EnumWindows(cb, 0)
    return result


def find_terminal_hwnd() -> Optional[int]:
    """祖先链中最近的、拥有可见顶层窗口的进程的窗口句柄（终端或 IDE 主窗口）。"""
    if not IS_WIN:
        return None
    win_map = _win_windows_by_pid()
    for pid in ancestor_pids():
        if pid in win_map:
            return win_map[pid][0]
    return None


def focus_window(hwnd: int):
    """聚焦窗口（由 toast 点击触发的协议处理器调用）。"""
    if not IS_WIN:
        return
    user32 = ctypes.windll.user32
    SW_RESTORE = 9
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    # 模拟一次 Alt 键，绕过 SetForegroundWindow 的前台权限限制
    VK_MENU, KEYEVENTF_KEYUP = 0x12, 0x0002
    user32.keybd_event(VK_MENU, 0, 0, 0)
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    user32.SetForegroundWindow(hwnd)


# ---------------- Windows: 协议注册（toast 点击 → 聚焦终端） ----------------

def register_protocol_handler(log: logging.Logger):
    """注册 fdseclaude:// 协议到 HKCU（无需管理员），点击 toast 时回调聚焦。"""
    if not IS_WIN:
        return
    try:
        import winreg

        from . import hook_handler  # noqa: F401  仅为取路径
        handler_path = hook_handler.__file__
        pythonw = sys.executable.replace("python.exe", "pythonw.exe")
        import os
        if not os.path.exists(pythonw):
            pythonw = sys.executable
        cmd = f'"{pythonw}" "{handler_path}" focus "%1"'

        root = winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                                rf"Software\Classes\{PROTOCOL_NAME}")
        winreg.SetValueEx(root, None, 0, winreg.REG_SZ, "URL:fdseClaude focus")
        winreg.SetValueEx(root, "URL Protocol", 0, winreg.REG_SZ, "")
        cmd_key = winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            rf"Software\Classes\{PROTOCOL_NAME}\shell\open\command")
        winreg.SetValueEx(cmd_key, None, 0, winreg.REG_SZ, cmd)
        winreg.CloseKey(cmd_key)
        winreg.CloseKey(root)
        log.debug("已注册 %s:// 协议处理器: %s", PROTOCOL_NAME, cmd)
    except Exception:
        log.exception("注册协议处理器失败（toast 点击聚焦将不可用）")


# ---------------- 发送系统通知 ----------------

_PS_APPID = (
    "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}"
    "\\WindowsPowerShell\\v1.0\\powershell.exe"
)


def _notify_windows(title: str, body: str, log: logging.Logger):
    hwnd = find_terminal_hwnd() or 0
    xml = f"""<toast activationType="protocol" launch="{PROTOCOL_NAME}:focus/{hwnd}">
  <visual><binding template="ToastGeneric">
    <text>{escape(title)}</text>
    <text>{escape(body)}</text>
  </binding></visual>
  <audio src="ms-winsoundevent:Notification.Default"/>
</toast>"""
    script = f"""
$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime]
$null = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime]
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml(@'
{xml}
'@)
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{_PS_APPID}').Show($toast)
"""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    rc, out = run(
        ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        timeout=20,
    )
    if rc != 0:
        log.error("Windows toast 发送失败: %s", out[:300])


def _notify_mac(title: str, body: str, log: logging.Logger):
    tn = which_or_none("terminal-notifier")
    if tn:
        run([tn, "-title", title, "-message", body, "-activate",
             "com.apple.Terminal"], timeout=15)
        return
    body_esc = body.replace("\\", "\\\\").replace('"', '\\"')
    title_esc = title.replace("\\", "\\\\").replace('"', '\\"')
    rc, out = run(
        ["osascript", "-e",
         f'display notification "{body_esc}" with title "{title_esc}"'],
        timeout=15,
    )
    if rc != 0:
        log.error("macOS 通知发送失败: %s", out[:300])


def _notify_linux(title: str, body: str, log: logging.Logger):
    if which_or_none("notify-send"):
        rc, out = run(["notify-send", "-a", "fdseClaude", title, body], timeout=15)
        if rc != 0:
            log.error("notify-send 失败: %s", out[:300])
    else:
        log.warning("未找到 notify-send，无法发送系统通知")


def send_notification(title: str, body: str, log: logging.Logger):
    log.info("发送系统通知: %s | %s", title, body)
    try:
        if IS_WIN:
            _notify_windows(title, body, log)
        elif IS_MAC:
            _notify_mac(title, body, log)
        else:
            _notify_linux(title, body, log)
    except Exception:
        log.exception("发送系统通知失败")
