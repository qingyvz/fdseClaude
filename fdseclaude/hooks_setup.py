"""向 ~/.claude/settings.json 注入 Notification / Stop hooks。

hooks 常驻注入（幂等）：hook 自身会检查 .fdseClaudeNotify 标记文件，
通知模式关闭时 hook 为空操作，因此无需在退出时移除。
"""
import json
import logging
import sys
from pathlib import Path

from .config import SETTINGS_FILE

_MARKER = "hook_handler.py"


def _hook_command() -> str:
    handler = Path(__file__).with_name("hook_handler.py")
    return f'"{sys.executable}" "{handler}"'


def install_hooks(log: logging.Logger):
    try:
        settings = {}
        if SETTINGS_FILE.exists():
            settings = json.loads(SETTINGS_FILE.read_text(encoding="utf-8") or "{}")
        hooks = settings.setdefault("hooks", {})
        changed = False
        for event, arg in (("Notification", "notification"), ("Stop", "stop")):
            entries = hooks.setdefault(event, [])
            if _MARKER in json.dumps(entries):
                continue
            entries.append({
                "hooks": [{
                    "type": "command",
                    "command": f"{_hook_command()} {arg}",
                    "timeout": 30,
                }]
            })
            changed = True
        if changed:
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS_FILE.write_text(
                json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            log.info("已向 %s 注入 Notification/Stop hooks", SETTINGS_FILE)
        else:
            log.debug("hooks 已存在，跳过注入")
    except Exception:
        log.exception("注入 hooks 失败（系统通知模式可能不可用）")
