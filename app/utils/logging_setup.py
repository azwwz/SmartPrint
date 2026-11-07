"""日志配置。

日志写入 logs/smartprint.log，同时输出到控制台。
按 readme 的要求，后续的任务接收、打印命令、用户操作、异常与恢复过程都记录到这里。
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.utils.paths import LOGS_DIR

LOG_FILE = LOGS_DIR / "smartprint.log"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """初始化日志，返回 SmartPrint 主 logger。"""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("smartprint")
    logger.setLevel(level)
    logger.propagate = False

    # 避免重复初始化时产生重复日志行
    if logger.handlers:
        return logger

    formatter = logging.Formatter(LOG_FORMAT)

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger


def log_file_path() -> Path:
    """返回日志文件路径，便于界面显示。"""
    return LOG_FILE
