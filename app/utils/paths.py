"""项目路径定义。

所有模块都从这里获取目录，避免各处在代码里硬编码路径。

阶段八增加了 PyInstaller 打包支持：

- 开发运行时，`PROJECT_ROOT` 是源码根目录（app/utils/paths.py 往上三层）；
- 打包成 exe 之后，`PROJECT_ROOT` 是 exe 所在目录，
  这样配置、日志和 Data 目录始终跟着 exe 走，用户把整个文件夹搬到别处也能用；
- 只读的程序资源（图标等）打包后在 `sys._MEIPASS` 下，因此单独用 `RESOURCES_DIR` 指过去。
"""

from __future__ import annotations

import sys
from pathlib import Path


def _app_root() -> Path:
    """返回程序根目录：源码运行时是项目根，打包后是 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _bundle_root() -> Path:
    """返回随程序一起打包的只读资源目录（PyInstaller onefile/onedir 下的 _MEIPASS）。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return _app_root()


# app/utils/paths.py -> app/utils -> app -> 项目根目录（打包后是 exe 所在目录）
PROJECT_ROOT = _app_root()

APP_DIR = PROJECT_ROOT / "app"
CONFIG_DIR = PROJECT_ROOT / "config"
DRIVERS_DIR = PROJECT_ROOT / "drivers"
LOGS_DIR = PROJECT_ROOT / "logs"

#: 打包进 exe 的资源目录（图标等）
_BUNDLE_DIR = _bundle_root()
RESOURCES_DIR = (
    _BUNDLE_DIR / "resources" if (_BUNDLE_DIR / "resources").is_dir() else PROJECT_ROOT / "resources"
)

DATA_DIR = PROJECT_ROOT / "Data"
INCOMING_DIR = DATA_DIR / "incoming"      # PDF24 自动保存的原始 PDF
QUEUE_DIR = DATA_DIR / "queue"            # 等待处理的文件或中间文件
JOBS_DIR = DATA_DIR / "jobs"              # 任务执行过程中的临时 PDF
DATABASE_DIR = DATA_DIR / "database"      # SQLite 数据库
COMPLETED_DIR = DATA_DIR / "completed"    # 已完成任务归档
BACKUPS_DIR = DATA_DIR / "backups"        # 阶段八：数据备份

CONFIG_FILE = CONFIG_DIR / "config.json"
DATABASE_FILE = DATABASE_DIR / "smartprint.db"   # SQLite 数据库文件

ALL_DIRS = (
    CONFIG_DIR,
    DRIVERS_DIR,
    RESOURCES_DIR,
    LOGS_DIR,
    DATA_DIR,
    INCOMING_DIR,
    QUEUE_DIR,
    JOBS_DIR,
    DATABASE_DIR,
    COMPLETED_DIR,
    BACKUPS_DIR,
)


def ensure_dirs() -> None:
    """确保项目所需目录存在，缺失的自动创建。"""
    for directory in ALL_DIRS:
        directory.mkdir(parents=True, exist_ok=True)


def is_frozen() -> bool:
    """当前是否运行在 PyInstaller 打包出来的 exe 里。"""
    return bool(getattr(sys, "frozen", False))
