"""fdseClaude 全局配置。"""
from pathlib import Path

# ---------- 远程服务器 ----------
REMOTE_HOST = "fdse@10.176.37.2"
# 远端令牌路径（相对远端 $HOME）
REMOTE_CRED_PATH = ".claude/.credentials.json"

# ---------- 代理链路 ----------
HTTP_PROXY_PORT = 8080          # hpts (http-proxy-to-socks) 监听端口
HTTP_PORT_FALLBACK_COUNT = 5    # 8080 被占用时最多尝试 5 个备选端口
HPTS_START_RETRIES = 3          # 8080 空闲时启动 hpts 的最大重试次数
SOCKS_PORT = 1080               # ssh -D 动态转发端口
SSH_RETRIES = 3                 # ssh 连接建立的最大重试次数
SCP_RETRIES = 3                 # 令牌推送/拉取的最大重试次数

# ssh 持久连接参数：心跳保活，避免长耗时任务期间连接被断开
SSH_BASE_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=6",
    "-o", "StrictHostKeyChecking=accept-new",
]

# ---------- 本地路径 ----------
CLAUDE_DIR = Path.home() / ".claude"
CRED_FILE = CLAUDE_DIR / ".credentials.json"
SETTINGS_FILE = CLAUDE_DIR / "settings.json"
NOTIFY_FLAG_FILE = CLAUDE_DIR / ".fdseClaudeNotify"

APP_DIR = CLAUDE_DIR / "fdseClaude"
LOG_DIR = APP_DIR / "logs"
MACHINE_ID_FILE = APP_DIR / "machine_id"

# ---------- 守护任务 ----------
TOKEN_POLL_INTERVAL = 5         # 秒，轮询 .credentials.json 变化
PROXY_GUARD_INTERVAL = 30       # 秒，代理路径健康检查
PROBE_TIMEOUT = 120             # 秒，claude -p "hi" 试探超时

# Windows toast 点击聚焦使用的自定义协议名
PROTOCOL_NAME = "fdseclaude"
