"""Claude Code hook 处理器（独立子进程入口）。

由 ~/.claude/settings.json 中注入的 hooks 调用：
  - Notification 事件：Claude 请求工具审批 / 提问 / 空闲等待（auto 模式下
    权限审批不会触发 Notification hook，天然区分了 auto 模式）
  - Stop 事件：Claude 完成任务停止
  - focus：Windows toast 点击后经 fdseclaude:// 协议回调，聚焦终端窗口

仅当 ~/.claude/.fdseClaudeNotify 存在（通知模式开启）且终端/父窗口失焦时才通知。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fdseclaude import notify  # noqa: E402
from fdseclaude.config import NOTIFY_FLAG_FILE  # noqa: E402
from fdseclaude.logger import setup_hook_logger  # noqa: E402


def main():
    log = setup_hook_logger()
    event = sys.argv[1] if len(sys.argv) > 1 else ""

    if event == "focus":
        # 参数形如 fdseclaude:focus/123456
        try:
            hwnd = int(sys.argv[2].rstrip("/").split("/")[-1])
            log.info("toast 点击，聚焦窗口 hwnd=%s", hwnd)
            if hwnd:
                notify.focus_window(hwnd)
        except (IndexError, ValueError):
            log.error("focus 参数无效: %s", sys.argv)
        return

    payload = {}
    try:
        payload = json.load(sys.stdin)
    except Exception:
        pass
    log.debug("hook 事件=%s payload=%s", event, json.dumps(payload, ensure_ascii=False)[:500])

    if not NOTIFY_FLAG_FILE.exists():
        log.debug("通知模式未启用，忽略")
        return
    if notify.is_our_terminal_focused():
        log.debug("终端/父窗口持有焦点，不发通知")
        return

    if event == "notification":
        msg = payload.get("message") or "Claude 需要你的处理（审批/提问）"
        notify.send_notification("Claude Code 等待干预", msg, log)
    elif event == "stop":
        notify.send_notification("Claude Code 已停止", "Claude 完成了任务或已停下，等待你的输入", log)


if __name__ == "__main__":
    main()
