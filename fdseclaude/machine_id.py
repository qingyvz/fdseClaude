"""用户标识符：与电脑绑定且始终不变。

方案：优先读取操作系统级机器 GUID（重装软件不变，仅重装系统才会变）：
  - Windows: 注册表 HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid
  - Linux:   /etc/machine-id
  - macOS:   IOPlatformUUID
取 sha256 前 12 位作为短标识，并缓存到 ~/.claude/fdseClaude/machine_id，
保证即使系统级来源读取失败，同一台机器后续也始终使用同一标识。
"""
import hashlib
import re
import socket
import uuid
from pathlib import Path

from .config import MACHINE_ID_FILE
from .utils import IS_MAC, IS_WIN, run


def _raw_machine_id() -> str:
    try:
        if IS_WIN:
            rc, out = run(
                ["reg", "query",
                 r"HKLM\SOFTWARE\Microsoft\Cryptography", "/v", "MachineGuid"],
                timeout=10,
            )
            m = re.search(r"MachineGuid\s+\S+\s+(\S+)", out)
            if rc == 0 and m:
                return m.group(1)
        elif IS_MAC:
            rc, out = run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], timeout=10
            )
            m = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out)
            if rc == 0 and m:
                return m.group(1)
        else:
            p = Path("/etc/machine-id")
            if p.exists():
                return p.read_text().strip()
    except Exception:
        pass
    # 兜底：主机名 + MAC 地址
    return f"{socket.gethostname()}-{uuid.getnode()}"


def get_machine_id() -> str:
    try:
        if MACHINE_ID_FILE.exists():
            cached = MACHINE_ID_FILE.read_text(encoding="utf-8").strip()
            if cached:
                return cached
    except OSError:
        pass
    mid = hashlib.sha256(_raw_machine_id().encode("utf-8")).hexdigest()[:12]
    try:
        MACHINE_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
        MACHINE_ID_FILE.write_text(mid, encoding="utf-8")
    except OSError:
        pass
    return mid
