#!/usr/bin/env python3
"""fdseClaude 主入口。

用法：fdseClaude [--notify] [其余参数原样传给 claude]
"""
import atexit
import signal
import subprocess
import sys
import threading
import time

from fdseclaude.config import (
    APP_DIR,
    NOTIFY_FLAG_FILE,
)
from fdseclaude.daemons import ProxyGuard, TokenGuard
from fdseclaude.hooks_setup import install_hooks
from fdseclaude.logger import setup_logger
from fdseclaude.machine_id import get_machine_id
from fdseclaude.notify import (
    is_our_terminal_focused,
    register_protocol_handler,
    send_notification,
)
from fdseclaude.proxy_path import ProxyPath, ProxyPathError
from fdseclaude.token_manager import (
    TokenError,
    cred_hash,
    ensure_git_repo,
    git_commit,
    probe_local,
    push_token,
    startup_token_sync,
)
from fdseclaude.utils import IS_WIN, proxy_env, run, which_or_none


def _clear_persistent_proxy_env(log):
    """清理持久化代理环境变量（Windows 用户级；本会话环境变量随进程消亡）。"""
    if IS_WIN:
        for var in ("HTTP_PROXY", "HTTPS_PROXY"):
            run(["reg", "delete", r"HKCU\Environment", "/v", var, "/f"], timeout=10)
        log.debug("已清理用户级 HTTP_PROXY / HTTPS_PROXY（如存在）")


def main():
    argv = sys.argv[1:]
    notify_enabled = "--notify" in argv
    claude_args = [a for a in argv if a != "--notify"]

    APP_DIR.mkdir(parents=True, exist_ok=True)
    log = setup_logger()
    log.info("=" * 60)
    log.info("fdseClaude 启动 | notify=%s | 透传参数=%s | machine_id=%s",
             notify_enabled, claude_args, get_machine_id())

    # 系统通知模式标记文件：默认关闭，每次启动按本次参数重置
    try:
        if notify_enabled:
            NOTIFY_FLAG_FILE.write_text("", encoding="utf-8")
            log.info("系统通知模式已启用（删除 %s 可随时关闭）", NOTIFY_FLAG_FILE)
        else:
            NOTIFY_FLAG_FILE.unlink(missing_ok=True)
    except OSError:
        log.exception("写入通知标记文件失败")

    install_hooks(log)
    if IS_WIN:
        register_protocol_handler(log)

    # ---------- 启动时：两个任务并发 ----------
    proxy = ProxyPath(log)
    proxy_ready = threading.Event()
    errors = {}

    def t_proxy():
        try:
            proxy.establish()
        except ProxyPathError as e:
            errors["proxy"] = str(e)
        except Exception as e:
            errors["proxy"] = repr(e)
            log.exception("代理路径建立异常")
        finally:
            proxy_ready.set()

    def t_token():
        try:
            ensure_git_repo(log)
            proxy_ready.wait()
            if "proxy" in errors:
                return
            # 本地令牌试探需要经过已建立的代理链路
            startup_token_sync(proxy.http_port, log)
        except TokenError as e:
            errors["token"] = str(e)
        except Exception as e:
            errors["token"] = repr(e)
            log.exception("令牌管理异常")

    th1 = threading.Thread(target=t_proxy, name="ProxySetup")
    th2 = threading.Thread(target=t_token, name="TokenSetup")
    th1.start()
    th2.start()
    th1.join()
    th2.join()

    if errors:
        for k, v in errors.items():
            log.error("[%s] %s", k, v)
            print(f"[fdseClaude] 启动失败: {v}", file=sys.stderr)
        sys.exit(1)

    port = proxy.http_port

    # ---------- 守护任务 ----------
    stop_event = threading.Event()
    token_guard = TokenGuard(port, stop_event, log)
    proxy_guard = ProxyGuard(proxy, stop_event, log)
    token_guard.start()
    proxy_guard.start()

    # ---------- 退出清理 ----------
    cleaned = threading.Event()

    def cleanup():
        if cleaned.is_set():
            return
        cleaned.set()
        stop_event.set()
        log.info("执行退出清理 ...")
        try:
            # 0. 令牌变化检测 → 有效则推送
            if cred_hash() != token_guard.baseline:
                log.info("退出时检测到 .credentials.json 变化，执行有效性检查")
                if probe_local(port, log):
                    if push_token(log):
                        ts = time.strftime("%Y-%m-%d %H:%M:%S")
                        git_commit(
                            log,
                            f"OAuth token updated by local CLI at {ts} by {get_machine_id()}",
                        )
                else:
                    log.warning("退出时本地令牌失效，不推送")
        except Exception:
            log.exception("退出时令牌检查失败")
        # 1. 清理代理环境变量
        _clear_persistent_proxy_env(log)
        # 2. 守护线程已通过 stop_event 停止（daemon 线程随进程退出）
        # 3. ssh / hpts 进程保留以便复用
        try:
            NOTIFY_FLAG_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        log.info("清理完成，ssh / hpts 进程保留以便下次复用")

    atexit.register(cleanup)

    def _on_signal(signum, frame):
        log.info("收到信号 %s，准备退出", signum)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_signal)
    if IS_WIN and hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _on_signal)

    # ---------- 设置终端代理并启动 Claude ----------
    claude = which_or_none("claude")
    if not claude:
        log.error("未找到 claude 命令，请先安装 Claude CLI")
        print("[fdseClaude] 未找到 claude 命令，请先安装 Claude CLI", file=sys.stderr)
        sys.exit(1)

    env = proxy_env(port)  # 仅对本次会话（子进程）生效
    log.info("启动 Claude CLI: %s (HTTP_PROXY=http://127.0.0.1:%s)", claude_args, port)
    print(f"[fdseClaude] 代理就绪 (127.0.0.1:{port})，正在启动 Claude CLI ...")

    # Ctrl+C 应交给 claude 处理，父进程忽略，避免包装脚本先于 claude 退出
    old_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        rc = subprocess.call([claude, *claude_args], env=env)
    finally:
        signal.signal(signal.SIGINT, old_sigint)

    log.info("Claude CLI 退出，returncode=%s", rc)
    if rc != 0 and NOTIFY_FLAG_FILE.exists() and not is_our_terminal_focused():
        send_notification("Claude Code 异常退出",
                          f"Claude 因故障退出 (code {rc})，请检查终端", log)

    cleanup()
    sys.exit(rc)


if __name__ == "__main__":
    main()
