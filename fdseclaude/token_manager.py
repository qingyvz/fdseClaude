"""OAuth 令牌管理：git 仓库、有效性试探、scp 推送/拉取。

拉取条件：本地令牌失效 且 远端令牌有效
推送条件：本地令牌变化 且 本地令牌有效
"""
import hashlib
import json
import logging
import time
from datetime import datetime
from typing import Optional

from .config import (
    CLAUDE_DIR,
    CRED_FILE,
    PROBE_TIMEOUT,
    REMOTE_CRED_PATH,
    REMOTE_HOST,
    SCP_RETRIES,
    SSH_BASE_OPTS,
)
from .utils import proxy_env, run, which_or_none


class TokenError(Exception):
    pass


# git 提交身份固定为脚本自身，避免依赖用户全局 git 配置
_GIT_IDENT = ["-c", "user.name=fdseClaude", "-c", "user.email=fdseClaude@local"]


def _git(args, timeout=30):
    return run(["git", *_GIT_IDENT, "-C", str(CLAUDE_DIR), *args], timeout=timeout)


# ---------------- git 仓库 ----------------

def ensure_git_repo(log: logging.Logger):
    """确保 ~/.claude 是 git 仓库，且仅追踪 .credentials.json。"""
    CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
    if (CLAUDE_DIR / ".git").exists():
        log.info("~/.claude git 仓库已存在")
        return
    log.info("初始化 ~/.claude git 仓库（仅追踪 .credentials.json）")
    rc, out = _git(["init"])
    if rc != 0:
        raise TokenError(f"git init 失败: {out[:300]}")
    gitignore = CLAUDE_DIR / ".gitignore"
    try:
        gitignore.write_text("*\n!.gitignore\n!.credentials.json\n", encoding="utf-8")
    except OSError as e:
        raise TokenError(f"写入 .gitignore 失败: {e}")
    rc, out = _git(["add", ".gitignore"])
    if rc != 0:
        raise TokenError(f"git add 失败: {out[:300]}")
    if CRED_FILE.exists():
        _git(["add", "-f", ".credentials.json"])
    rc, out = _git(["commit", "-m", "init fdseClaude credential tracking"])
    if rc != 0 and "nothing to commit" not in out:
        raise TokenError(f"git commit 失败: {out[:300]}")
    log.info("git 仓库初始化成功")


def git_commit(log: logging.Logger, message: str):
    _git(["add", "-f", ".credentials.json"])
    rc, out = _git(["commit", "-m", message])
    if rc == 0:
        log.info("git commit: %s", message)
    elif "nothing to commit" in out:
        log.debug("git commit 跳过（无变化）: %s", message)
    else:
        log.error("git commit 失败: %s", out[:300])


# ---------------- 令牌状态 ----------------

def cred_hash() -> Optional[str]:
    try:
        return hashlib.sha256(CRED_FILE.read_bytes()).hexdigest()
    except OSError:
        return None


def cred_expires_at() -> Optional[str]:
    """从 .credentials.json 解析 expiresAt，便于日志定位令牌刷新时机。"""
    try:
        data = json.loads(CRED_FILE.read_text(encoding="utf-8"))
        ts = (data.get("claudeAiOauth") or {}).get("expiresAt")
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    return None


def log_cred_state(log: logging.Logger, prefix: str):
    log.info("%s | hash=%s expiresAt=%s", prefix, cred_hash(), cred_expires_at())


# ---------------- 有效性试探 ----------------

def probe_local(http_port: int, log: logging.Logger) -> bool:
    """通过已建立的代理链路执行 claude -p "hi"，验证本地令牌。"""
    claude = which_or_none("claude")
    if not claude:
        raise TokenError("本地未找到 claude 命令，请先安装 Claude CLI")
    log.info("试探本地令牌：claude -p \"hi\" (经由代理端口 %s)", http_port)
    rc, out = run([claude, "-p", "hi"], timeout=PROBE_TIMEOUT, env=proxy_env(http_port))
    valid = "401" not in out
    log.info("本地令牌试探结果: %s (rc=%s)", "有效" if valid else "失效(401)", rc)
    log.debug("本地试探输出: %s", out[:500])
    return valid


def probe_remote(log: logging.Logger) -> bool:
    """通过 ssh 在远程服务器上执行 claude -p "hi"，验证远端令牌。"""
    log.info("试探远端令牌：ssh %s claude -p \"hi\"", REMOTE_HOST)
    rc, out = run(
        ["ssh", *SSH_BASE_OPTS, REMOTE_HOST, "bash -lc 'claude -p \"hi\"'"],
        timeout=PROBE_TIMEOUT,
    )
    if "command not found" in out or "not found" in out.lower() and rc != 0:
        log.error("远端未找到 claude 命令: %s", out[:300])
        return False
    valid = "401" not in out
    log.info("远端令牌试探结果: %s (rc=%s)", "有效" if valid else "失效(401)", rc)
    log.debug("远端试探输出: %s", out[:500])
    return valid


# ---------------- 推送 / 拉取 ----------------

def push_token(log: logging.Logger) -> bool:
    if not CRED_FILE.exists():
        log.error("本地 .credentials.json 不存在，无法推送")
        return False
    run(["ssh", *SSH_BASE_OPTS, REMOTE_HOST, "mkdir -p ~/.claude"], timeout=20)
    for i in range(1, SCP_RETRIES + 1):
        rc, out = run(
            ["scp", *SSH_BASE_OPTS, str(CRED_FILE), f"{REMOTE_HOST}:{REMOTE_CRED_PATH}"],
            timeout=60,
        )
        if rc == 0:
            log.info("令牌推送成功 (尝试 %s/%s)", i, SCP_RETRIES)
            return True
        log.warning("令牌推送失败 %s/%s: %s", i, SCP_RETRIES, out[:300])
        time.sleep(1)
    return False


def pull_token(log: logging.Logger) -> bool:
    CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
    for i in range(1, SCP_RETRIES + 1):
        rc, out = run(
            ["scp", *SSH_BASE_OPTS, f"{REMOTE_HOST}:{REMOTE_CRED_PATH}", str(CRED_FILE)],
            timeout=60,
        )
        if rc == 0:
            log.info("令牌拉取成功 (尝试 %s/%s)", i, SCP_RETRIES)
            return True
        log.warning("令牌拉取失败 %s/%s: %s", i, SCP_RETRIES, out[:300])
        time.sleep(1)
    return False


# ---------------- 启动时同步流程（README 2.2 / 2.3） ----------------

def startup_token_sync(http_port: int, log: logging.Logger):
    log_cred_state(log, "启动时本地令牌状态")
    if CRED_FILE.exists() and probe_local(http_port, log):
        # 2.2 本地有效 → 推送远端
        if not push_token(log):
            raise TokenError("无法推送 OAuth 令牌到远程服务器，脚本终止")
        log.info("本地令牌有效，已同步至远端，跳过远端检查")
        return
    # 2.3 本地失效 → 检查远端
    log.info("本地令牌失效或不存在，检查远端令牌")
    if not probe_remote(log):
        raise TokenError("本地与远端 OAuth 令牌均已失效，请在远程服务器上重新登录 claude")
    if not pull_token(log):
        raise TokenError("无法从远程服务器下载 OAuth 令牌，脚本终止")
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    git_commit(log, f"OAuth token updated by remote at {ts}")
    log_cred_state(log, "拉取远端令牌后状态")
