"""跨平台基础工具：端口探测、进程查找、子进程封装。"""
import os
import shutil
import socket
import subprocess
import sys
import time
from typing import List, Optional, Set, Tuple

import psutil

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def find_listener(port: int) -> Optional[psutil.Process]:
    """返回监听指定端口的进程；找不到/无权限时返回 None。"""
    try:
        for c in psutil.net_connections(kind="tcp"):
            if (
                c.laddr
                and c.laddr.port == port
                and c.status == psutil.CONN_LISTEN
                and c.pid
            ):
                try:
                    return psutil.Process(c.pid)
                except psutil.Error:
                    return None
    except (psutil.AccessDenied, PermissionError):
        # macOS 上 net_connections 需要 root，降级用 lsof
        if not IS_WIN:
            rc, out = run(
                ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], timeout=10
            )
            if rc == 0 and out.strip():
                try:
                    return psutil.Process(int(out.strip().splitlines()[0]))
                except (ValueError, psutil.Error):
                    return None
    return None


def proc_desc(p: Optional[psutil.Process]) -> str:
    if p is None:
        return "(未知进程)"
    try:
        cmd = " ".join(p.cmdline())[:200]
        return f"PID={p.pid} 名称={p.name()} 命令行={cmd}"
    except psutil.Error:
        return f"PID={getattr(p, 'pid', '?')} (无法读取进程详情)"


def run(
    cmd: List[str], timeout: float = 30, env: Optional[dict] = None
) -> Tuple[int, str]:
    """运行命令并捕获 stdout+stderr 合并文本。返回 (returncode, output)。"""
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired as e:
        partial = ""
        for s in (e.stdout, e.stderr):
            if s:
                partial += s.decode("utf-8", "replace") if isinstance(s, bytes) else s
        return -1, "[TIMEOUT] " + partial
    except FileNotFoundError:
        return -2, f"[NOT FOUND] 命令不存在: {cmd[0]}"


def spawn_detached(cmd: List[str], logfile) -> subprocess.Popen:
    """启动一个与本脚本生命周期解耦的后台进程（脚本退出后仍存活，便于复用）。

    Windows 下必须完全静默：node/ssh 等控制台程序若用 DETACHED_PROCESS 启动，
    因脱离父控制台又无控制台可继承，反而会被系统分配一个新的控制台窗口（阻塞弹窗）。
    改用 CREATE_NO_WINDOW（不创建任何控制台窗口）+ CREATE_NEW_PROCESS_GROUP
    （独立进程组，父进程退出/Ctrl+C 不影响它，仍可后台存活复用），并显式隐藏窗口。
    """
    f = open(logfile, "ab")
    kwargs = {"stdout": f, "stderr": f, "stdin": subprocess.DEVNULL}
    if IS_WIN:
        kwargs["creationflags"] = (
            subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        )
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        kwargs["startupinfo"] = si
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def ancestor_pids() -> List[int]:
    """当前进程及其全部祖先 PID，自下而上有序（自己 → 终端/IDE → ...）。"""
    pids: List[int] = []
    seen: Set[int] = set()
    try:
        p = psutil.Process()
        while p is not None and p.pid not in seen:
            pids.append(p.pid)
            seen.add(p.pid)
            p = p.parent()
    except psutil.Error:
        pass
    return pids


def which_or_none(name: str) -> Optional[str]:
    return shutil.which(name)


def ask_yes_no(prompt: str) -> bool:
    while True:
        try:
            ans = input(f"{prompt} [y/n]: ").strip().lower()
        except EOFError:
            return False
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("请输入 y 或 n")


def wait_port(port: int, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_open(port):
            return True
        time.sleep(0.5)
    return port_open(port)


def proxy_env(port: int) -> dict:
    env = dict(os.environ)
    url = f"http://127.0.0.1:{port}"
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env[key] = url
    return env
