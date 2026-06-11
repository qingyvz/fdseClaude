"""代理路径探测与建立。

链路：claude CLI → hpts(HTTP代理, 默认8080) → ssh -D(SOCKS5, 1080) → 远程服务器 → Anthropic API
"""
import logging
from typing import List, Optional

import psutil

from .config import (
    HPTS_START_RETRIES,
    HTTP_PORT_FALLBACK_COUNT,
    HTTP_PROXY_PORT,
    LOG_DIR,
    REMOTE_HOST,
    SOCKS_PORT,
    SSH_BASE_OPTS,
    SSH_RETRIES,
)
from .utils import (
    ask_yes_no,
    find_listener,
    port_open,
    proc_desc,
    run,
    spawn_detached,
    wait_port,
    which_or_none,
)


class ProxyPathError(Exception):
    pass


def _is_hpts_proc(p: Optional[psutil.Process]) -> bool:
    if p is None:
        return False
    try:
        cmd = " ".join(p.cmdline()).lower()
        return "hpts" in cmd or "http-proxy-to-socks" in cmd
    except psutil.Error:
        return False


def _is_ssh_proc(p: Optional[psutil.Process]) -> bool:
    if p is None:
        return False
    try:
        return p.name().lower().startswith("ssh")
    except psutil.Error:
        return False


class ProxyPath:
    def __init__(self, log: logging.Logger):
        self.log = log
        self.http_port: Optional[int] = None

    # ---------------- 启动时建立 ----------------

    def establish(self) -> int:
        """完整的启动时代理路径建立流程（可能与用户交互）。"""
        self._ensure_hpts()
        self._ensure_socks_port()
        self._ensure_ssh()
        self.log.info("代理路径建立成功：claude → 127.0.0.1:%s (hpts) → 127.0.0.1:%s (ssh -D) → %s",
                      self.http_port, SOCKS_PORT, REMOTE_HOST)
        return self.http_port

    # ---- 步骤 1：hpts ----

    def _hpts_binary(self, interactive: bool) -> str:
        hpts = which_or_none("hpts")
        if hpts:
            return hpts
        if not interactive:
            raise ProxyPathError("未找到 hpts 命令")
        self.log.warning("未找到 hpts (http-proxy-to-socks)")
        npm = which_or_none("npm")
        if npm and ask_yes_no("未检测到 hpts，是否自动执行 npm install -g http-proxy-to-socks ?"):
            self.log.info("正在安装 http-proxy-to-socks ...")
            rc, out = run([npm, "install", "-g", "http-proxy-to-socks"], timeout=300)
            self.log.debug("npm install 输出: %s", out[-500:])
            hpts = which_or_none("hpts")
            if rc == 0 and hpts:
                return hpts
        raise ProxyPathError(
            "缺少 hpts。请先安装 Node.js 后执行: npm install -g http-proxy-to-socks"
        )

    def _start_hpts(self, port: int, interactive: bool = True) -> bool:
        hpts = self._hpts_binary(interactive)
        cmd = [hpts, "-s", f"127.0.0.1:{SOCKS_PORT}", "-p", str(port)]
        self.log.info("尝试在端口 %s 启动 hpts: %s", port, " ".join(cmd))
        try:
            spawn_detached(cmd, LOG_DIR / "hpts.log")
        except OSError as e:
            self.log.error("启动 hpts 失败: %s", e)
            return False
        ok = wait_port(port, timeout=6)
        self.log.info("hpts 端口 %s %s", port, "已就绪" if ok else "启动失败")
        return ok

    def _ensure_hpts(self):
        port = HTTP_PROXY_PORT
        if port_open(port):
            p = find_listener(port)
            if _is_hpts_proc(p):
                self.log.info("端口 %s 已有 hpts 进程在运行，复用", port)
                self.http_port = port
                return
            print(f"端口 {port} 已被以下进程占用：{proc_desc(p)}")
            if not ask_yes_no(f"是否尝试在端口 {port + 1} 启动 hpts 进程?"):
                raise ProxyPathError("用户拒绝在备用端口启动 hpts，脚本终止")
            for cand in range(port + 1, port + 1 + HTTP_PORT_FALLBACK_COUNT):
                if port_open(cand):
                    lp = find_listener(cand)
                    if _is_hpts_proc(lp):
                        self.log.info("端口 %s 已有 hpts，直接复用", cand)
                        self.http_port = cand
                        return
                    self.log.warning("端口 %s 也被占用 (%s)，尝试下一个", cand, proc_desc(lp))
                    continue
                if self._start_hpts(cand):
                    self.http_port = cand
                    return
            raise ProxyPathError(
                f"已尝试 {HTTP_PORT_FALLBACK_COUNT} 个备用端口，仍无法启动 hpts，脚本终止"
            )
        else:
            for i in range(1, HPTS_START_RETRIES + 1):
                self.log.info("hpts 启动尝试 %s/%s (端口 %s)", i, HPTS_START_RETRIES, port)
                if self._start_hpts(port):
                    self.http_port = port
                    return
            raise ProxyPathError(f"无法在端口 {port} 启动 hpts 进程，脚本终止")

    # ---- 步骤 2：1080 SOCKS 端口 ----

    def _ensure_socks_port(self):
        if port_open(SOCKS_PORT):
            p = find_listener(SOCKS_PORT)
            if p is not None and not _is_ssh_proc(p):
                raise ProxyPathError(
                    f"端口 {SOCKS_PORT} 被非 ssh 进程占用：{proc_desc(p)}，脚本终止"
                )
            self.log.info("端口 %s 已由 ssh 监听", SOCKS_PORT)
            return
        # 1080 不可连接：自动建立 ssh -D 动态转发
        self.log.info("端口 %s 不可连接，自动建立 ssh -D 动态转发", SOCKS_PORT)
        for i in range(1, SSH_RETRIES + 1):
            self.log.info("ssh -D 启动尝试 %s/%s", i, SSH_RETRIES)
            if self._start_ssh_daemon():
                return
        raise ProxyPathError(f"无法建立 ssh -D {SOCKS_PORT} 连接，脚本终止")

    # ---- 步骤 3：ssh 连接 ----

    def _ssh_daemon_cmd(self) -> List[str]:
        # -N: 仅做转发；ServerAlive*: 持久连接保活
        return ["ssh", "-D", str(SOCKS_PORT), "-N", *SSH_BASE_OPTS, REMOTE_HOST]

    def _start_ssh_daemon(self) -> bool:
        try:
            spawn_detached(self._ssh_daemon_cmd(), LOG_DIR / "ssh.log")
        except OSError as e:
            self.log.error("启动 ssh 失败: %s", e)
            return False
        return wait_port(SOCKS_PORT, timeout=15)

    def _find_remote_ssh_procs(self) -> List[psutil.Process]:
        procs = []
        for p in psutil.process_iter(["name", "cmdline"]):
            try:
                if not (p.info["name"] or "").lower().startswith("ssh"):
                    continue
                cmd = " ".join(p.info["cmdline"] or [])
                # 只匹配长驻转发进程，排除瞬时的健康检查 ssh
                if REMOTE_HOST in cmd and "-N" in (p.info["cmdline"] or []):
                    procs.append(p)
            except psutil.Error:
                continue
        return procs

    def ssh_healthy(self) -> bool:
        """连接正常 = 本地主机能与远程主机正常通信。"""
        rc, out = run(
            ["ssh", *SSH_BASE_OPTS, REMOTE_HOST, "echo __fdse_ok__"], timeout=25
        )
        ok = "__fdse_ok__" in out
        if not ok:
            self.log.warning("ssh 健康检查失败 rc=%s 输出=%s", rc, out[:300])
        return ok

    def _ensure_ssh(self):
        procs = self._find_remote_ssh_procs()
        if procs:
            self.log.info("发现 %s 个连接到 %s 的 ssh 进程", len(procs), REMOTE_HOST)
            if self.ssh_healthy():
                return
            self.log.warning("ssh 连接异常，杀死僵尸 ssh 进程并重建")
            for p in procs:
                try:
                    p.kill()
                except psutil.Error:
                    pass
        for i in range(1, SSH_RETRIES + 1):
            self.log.info("重建 ssh 连接尝试 %s/%s", i, SSH_RETRIES)
            if self._start_ssh_daemon() and self.ssh_healthy():
                return
        raise ProxyPathError(f"无法建立到 {REMOTE_HOST} 的 ssh 连接，脚本终止")

    # ---------------- 运行时守护 ----------------

    def check_alive(self) -> bool:
        return (
            self.http_port is not None
            and port_open(self.http_port)
            and port_open(SOCKS_PORT)
            and self.ssh_healthy()
        )

    def rebuild_quiet(self) -> bool:
        """运行时无交互重建（守护任务调用）。"""
        try:
            if not port_open(SOCKS_PORT) or not self.ssh_healthy():
                self.log.info("[守护] ssh 节点不可用，重建中 ...")
                for p in self._find_remote_ssh_procs():
                    try:
                        p.kill()
                    except psutil.Error:
                        pass
                self._start_ssh_daemon()
            if self.http_port and not port_open(self.http_port):
                self.log.info("[守护] hpts 节点不可用，重建中 ...")
                self._start_hpts(self.http_port, interactive=False)
        except ProxyPathError as e:
            self.log.error("[守护] 重建失败: %s", e)
            return False
        alive = self.check_alive()
        self.log.info("[守护] 代理路径重建%s", "成功" if alive else "失败，稍后重试")
        return alive
