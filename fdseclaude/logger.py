"""日志系统：按天滚动，落盘 ~/.claude/fdseClaude/logs/。"""
import logging
from logging.handlers import TimedRotatingFileHandler

from .config import LOG_DIR

_FMT_FILE = "%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s"
_FMT_CONSOLE = "[fdseClaude] %(message)s"


def setup_logger(name: str = "fdseClaude", console: bool = True) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)

    fh = TimedRotatingFileHandler(
        LOG_DIR / f"{name}.log", when="midnight", backupCount=14, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(_FMT_FILE))
    logger.addHandler(fh)

    if console:
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        ch.setFormatter(logging.Formatter(_FMT_CONSOLE))
        logger.addHandler(ch)
    return logger


def mute_console(logger: logging.Logger) -> None:
    """关闭控制台输出（仍保留落盘）。

    Claude CLI 运行期间，守护线程若继续向 stderr 打印会破坏其 TUI 显示，
    因此把控制台 handler 的级别拉到 CRITICAL 以上，使其静默。
    """
    for h in logger.handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(
            h, logging.FileHandler
        ):
            h.setLevel(logging.CRITICAL + 1)


def unmute_console(logger: logging.Logger) -> None:
    """恢复控制台输出（与 mute_console 配对使用）。"""
    for h in logger.handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(
            h, logging.FileHandler
        ):
            h.setLevel(logging.INFO)


def setup_hook_logger() -> logging.Logger:
    """hook 子进程使用独立日志文件，避免与主进程争抢滚动句柄。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("fdseClaude.hooks")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    fh = logging.FileHandler(LOG_DIR / "hooks.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter(_FMT_FILE))
    logger.addHandler(fh)
    return logger
