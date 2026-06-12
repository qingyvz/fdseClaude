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
from fdseclaude.logger import mute_console, setup_logger, unmute_console
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
    local_token_expired,
    probe_local,
    push_token,
    startup_pull_from_remote,
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

    # ---------- 启动时：仅代理链路阻塞（claude 运行的必需前置） ----------
    # 代理建立可能与用户交互（端口占用询问），且 claude 需要 HTTP_PROXY 才能联网，
    # 必须在 claude 接管终端前同步完成。令牌校验则移至后台（见下）。
    proxy = ProxyPath(log)
    try:
        proxy.establish()
    except ProxyPathError as e:
        log.error("代理路径建立失败: %s", e)
        print(f"[fdseClaude] 启动失败: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        log.exception("代理路径建立异常")
        print(f"[fdseClaude] 启动失败: {e!r}", file=sys.stderr)
        sys.exit(1)

    port = proxy.http_port

    # ---------- 守护任务 ----------
    stop_event = threading.Event()
    guards = {}  # 持有 TokenGuard 引用，供退出清理读取 baseline

    # 代理守护立即启动（代理已就绪）
    ProxyGuard(proxy, stop_event, log).start()

    # ---------- 启动时令牌处理（折中方案） ----------
    # 1) 纯本地检查 expiresAt（不连服务器，极快）。
    # 2) 本地过期/缺失 → 阻塞连接真实远端校验并拉取；远端也失效则终止启动
    #    （此刻在 claude 启动前，可干净退出，避免起一个必然 401 的会话）。
    # 3) 本地未过期 → 不阻塞，将完整同步（probe_local + 推送/拉取）转入后台，
    #    用户输入首条 prompt 期间无感完成。
    defer_full_sync = True
    try:
        ensure_git_repo(log)  # 本地操作，快
        if local_token_expired():
            startup_pull_from_remote(log)  # 阻塞；失败抛 TokenError
            defer_full_sync = False  # 已持有远端有效令牌，无需后台再推送
        else:
            log.info("启动时本地令牌未过期，跳过阻塞校验，完整同步转入后台")
    except TokenError as e:
        log.error("启动令牌处理失败: %s", e)
        print(f"[fdseClaude] 启动失败: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception:
        log.exception("启动令牌处理异常，转入后台尽力同步")

    # 后台：完整同步（仅当本地未过期或上面异常兜底时）+ 启动令牌守护。
    # 守护在初始同步后才构造，baseline 反映同步后状态，避免误判为变化。
    def background_token_setup():
        try:
            if defer_full_sync:
                startup_token_sync(port, log)
        except TokenError as e:
            log.error("[令牌后台同步] 同步失败: %s", e)
            if NOTIFY_FLAG_FILE.exists():
                send_notification("Claude 令牌同步失败", str(e), log)
        except Exception:
            log.exception("[令牌后台同步] 异常")
        tg = TokenGuard(port, stop_event, log)
        tg.start()
        guards["token"] = tg

    threading.Thread(
        target=background_token_setup, name="TokenStartupSync", daemon=True
    ).start()

    # ---------- 退出清理 ----------
    cleaned = threading.Event()

    def cleanup():
        if cleaned.is_set():
            return
        cleaned.set()
        stop_event.set()
        log.info("执行退出清理 ...")
        tg = guards.get("token")
        try:
            # 0. 令牌变化检测 → 有效则推送（守护尚未就绪则跳过，由后台同步负责）
            if tg is not None and cred_hash() != tg.baseline:
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
    print(f"[fdseClaude] 代理就绪 (127.0.0.1:{port})，令牌将在后台校验，正在启动 Claude CLI ...")

    # Ctrl+C 应交给 claude 处理，父进程忽略，避免包装脚本先于 claude 退出
    old_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)
    # Claude CLI 接管终端期间，静默守护线程的控制台输出，避免破坏其 TUI 显示
    mute_console(log)
    try:
        rc = subprocess.call([claude, *claude_args], env=env)
    finally:
        unmute_console(log)
        signal.signal(signal.SIGINT, old_sigint)

    log.info("Claude CLI 退出，returncode=%s", rc)
    if rc != 0 and NOTIFY_FLAG_FILE.exists() and not is_our_terminal_focused():
        send_notification("Claude Code 异常退出",
                          f"Claude 因故障退出 (code {rc})，请检查终端", log)

    cleanup()
    sys.exit(rc)


if __name__ == "__main__":
    main()
