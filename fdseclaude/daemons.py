"""运行时守护任务（随主进程退出的 daemon 线程）。"""
import threading
import time

from .config import PROXY_GUARD_INTERVAL, TOKEN_POLL_INTERVAL
from .machine_id import get_machine_id
from .token_manager import (
    cred_hash,
    git_commit,
    log_cred_state,
    probe_local,
    probe_remote,
    pull_token,
    push_token,
)


class TokenGuard(threading.Thread):
    """令牌守护：检测 .credentials.json 变化（每 5 秒轮询 sha256 哈希，
    跨平台无额外依赖；哈希不同即视为变化），变化且本地有效时推送远端并 git commit；
    变化但本地失效时检查远端，若远端有效则拉取恢复（拉取条件：本地失效且远端有效）。
    """

    def __init__(self, http_port, stop_event, log):
        super().__init__(name="TokenGuard", daemon=True)
        self.http_port = http_port
        self.stop_event = stop_event
        self.log = log
        self.baseline = cred_hash()  # 最近一次已同步状态的哈希

    def run(self):
        self.log.info("[令牌守护] 启动，基线 hash=%s", self.baseline)
        while not self.stop_event.wait(TOKEN_POLL_INTERVAL):
            try:
                current = cred_hash()
                if current == self.baseline:
                    continue
                # 等待写入完全结束，避免读到半个文件
                time.sleep(2)
                log_cred_state(self.log, "[令牌守护] 检测到 .credentials.json 变化")
                if probe_local(self.http_port, self.log):
                    if push_token(self.log):
                        ts = time.strftime("%Y-%m-%d %H:%M:%S")
                        git_commit(
                            self.log,
                            f"OAuth token updated by local CLI at {ts} by {get_machine_id()}",
                        )
                    else:
                        self.log.error("[令牌守护] 推送失败，下次变化时重试")
                else:
                    self.log.warning("[令牌守护] 本地令牌失效（疑似令牌污染），不推送")
                    # 满足拉取条件检查：本地失效 且 远端有效 → 从远端恢复
                    if probe_remote(self.log):
                        if pull_token(self.log):
                            ts = time.strftime("%Y-%m-%d %H:%M:%S")
                            git_commit(self.log, f"OAuth token updated by remote at {ts}")
                            log_cred_state(self.log, "[令牌守护] 已从远端恢复令牌")
                        else:
                            self.log.error("[令牌守护] 从远端拉取令牌失败，下次变化时重试")
                    else:
                        self.log.error("[令牌守护] 远端令牌同样失效，无法恢复，请在远程服务器上重新登录 claude")
                self.baseline = cred_hash()
            except Exception:
                self.log.exception("[令牌守护] 异常")


class ProxyGuard(threading.Thread):
    """代理路径守护：定期检测整条链路，失效时自动重建直到成功。"""

    def __init__(self, proxy, stop_event, log):
        super().__init__(name="ProxyGuard", daemon=True)
        self.proxy = proxy
        self.stop_event = stop_event
        self.log = log

    def run(self):
        self.log.info("[代理守护] 启动，每 %s 秒检测一次", PROXY_GUARD_INTERVAL)
        while not self.stop_event.wait(PROXY_GUARD_INTERVAL):
            try:
                if self.proxy.check_alive():
                    continue
                self.log.warning("[代理守护] 代理路径不可用，开始重建")
                backoff = 5
                while not self.stop_event.is_set():
                    if self.proxy.rebuild_quiet():
                        break
                    self.stop_event.wait(backoff)
                    backoff = min(backoff * 2, 60)
            except Exception:
                self.log.exception("[代理守护] 异常")
