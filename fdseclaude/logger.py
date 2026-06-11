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
