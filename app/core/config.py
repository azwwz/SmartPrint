"""程序配置读取与保存。

配置文件位于 config/config.json。
读取时采用「缺省值兜底」的策略：配置文件缺少某些字段也不影响程序启动。

阶段八增加了「设置界面」，因此这里也提供保存配置的函数：

- `update_config()`：把界面上改动的字段深合并进 config.json，再重新加载；
- `save_config()`：把一份完整字典原子写入（先写临时文件再替换，避免写坏）。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.page_planner import (
    DEFAULT_BACK_ORDER,
    DEFAULT_FRONT_ORDER,
    normalize_back_order,
    normalize_front_order,
)
from app.utils.paths import (
    COMPLETED_DIR,
    CONFIG_FILE,
    DATA_DIR,
    DATABASE_FILE,
    INCOMING_DIR,
    JOBS_DIR,
    PROJECT_ROOT,
)

logger = logging.getLogger("smartprint.core.config")

DEFAULT_CONFIG: dict[str, Any] = {
    "printer": {
        "name": "HP LaserJet 1020",
        "virtual_printer_name": "PDF24",
        #: SumatraPDF 可执行文件；留空则自动在 drivers/、常见安装目录、注册表里查找
        "sumatra_path": "",
        #: 追加到 -print-settings 的设置（例如 simplex、paper=A4），可为空
        "print_settings": "simplex",
        #: 等待 SumatraPDF 退出的超时秒数
        "timeout_seconds": 60.0,
        #: 提交前后各观察一次队列，失败时额外等待多久再看一次
        "queue_recheck_seconds": 0.0,
    },
    "paper": {"size": "A4", "sheets_per_batch": 5},
    "duplex": {
        "mode": "manual",
        "front_page_order": "odd_first",
        "back_page_order": DEFAULT_BACK_ORDER,
        "flip_direction": "long_edge",
    },
    "incoming": {
        "stability_seconds": 2.0,
        "poll_interval_seconds": 0.5,
        "max_wait_seconds": 60.0,
        "rescan_interval_seconds": 60.0,
        "min_size_bytes": 512,
    },
    "database": {"file": "Data/database/smartprint.db"},
    "paths": {
        "incoming": "Data/incoming",
        "jobs": "Data/jobs",
        "database": "Data/database",
        "completed": "Data/completed",
        "backups": "Data/backups",
    },
    "backup": {"keep": 10, "include_jobs": False},
}

#: 手动双面打印的翻面方向：设置界面与纸张示意图共用
FLIP_DIRECTIONS: tuple[tuple[str, str], ...] = (
    ("long_edge", "沿长边翻面（像翻书一样，左右翻）"),
    ("short_edge", "沿短边翻面（上下翻）"),
)

#: 翻面方向的默认值
DEFAULT_FLIP_DIRECTION = "long_edge"


def flip_direction_text(value: str) -> str:
    """把配置里的翻面方向转成中文说明。"""
    key = normalize_flip_direction(value)
    for candidate, text in FLIP_DIRECTIONS:
        if candidate == key:
            return text
    return FLIP_DIRECTIONS[0][1]


def normalize_flip_direction(value: Any) -> str:
    """把翻面方向收敛到已知取值，未知值退回默认值（大小写、空格、横线都容忍）。"""
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    for key, _label in FLIP_DIRECTIONS:
        if key == text:
            return key
    return DEFAULT_FLIP_DIRECTION


@dataclass(frozen=True)
class PrinterSettings:
    """阶段五的打印设置。"""

    #: SumatraPDF 路径（空字符串表示自动查找）
    sumatra_path: str = ""
    #: 追加到 -print-settings 的设置
    print_settings: str = "simplex"
    #: 等待 SumatraPDF 进程退出的超时
    timeout_seconds: float = 60.0
    #: 提交后再等多久复查队列（离线时作业会留在队列里，复查可发现这种情况）
    queue_recheck_seconds: float = 0.0



@dataclass(frozen=True)
class IncomingSettings:
    """`Data/incoming` 目录的监控与文件稳定性参数。"""

    stability_seconds: float = 2.0      # 文件大小与修改时间保持不变多久才算写入完成
    poll_interval_seconds: float = 0.5  # 稳定性检查的轮询间隔
    max_wait_seconds: float = 60.0      # 单个文件最长等待时间，超时视为接收失败
    rescan_interval_seconds: float = 60.0  # 定时全目录复查，弥补 watchdog 丢事件
    min_size_bytes: int = 512           # 小于该大小的 PDF 视为无效


@dataclass(frozen=True)
class BackupSettings:
    """阶段八的数据备份设置。"""

    #: 最多保留多少份备份（超出的自动删除最旧的）
    keep: int = 10
    #: 备份时是否一并打包 Data/jobs（单页文件，体积较大）
    include_jobs: bool = False


@dataclass(frozen=True)
class AppConfig:
    """程序运行配置。"""

    printer_name: str = "HP LaserJet 1020"
    virtual_printer_name: str = "PDF24"
    printer: PrinterSettings = field(default_factory=PrinterSettings)
    paper_size: str = "A4"
    sheets_per_batch: int = 5
    #: 手动双面打印时的正反面页码顺序（阶段四用于拆分页面）
    front_page_order: str = DEFAULT_FRONT_ORDER
    back_page_order: str = DEFAULT_BACK_ORDER
    flip_direction: str = DEFAULT_FLIP_DIRECTION
    incoming_dir: Path = INCOMING_DIR
    #: 每份任务的打印文件目录（jobs/<任务号>/page_001.pdf …）
    jobs_dir: Path = JOBS_DIR
    database_file: Path = DATABASE_FILE
    #: 删除任务时默认把源 PDF 归档到这里（避免被重新扫描登记）
    archive_dir: Path = COMPLETED_DIR / "removed"
    #: 已完成任务归档（阶段八）
    archive_root: Path = COMPLETED_DIR / "archive"
    #: 数据备份目录（阶段八）
    backup_dir: Path = DATA_DIR / "backups"
    backup: BackupSettings = field(default_factory=BackupSettings)
    incoming: IncomingSettings = field(default_factory=IncomingSettings)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def flip_direction_text(self) -> str:
        """翻面方向的中文说明。"""
        return flip_direction_text(self.flip_direction)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """把 override 合并到 base 之上，返回新字典。"""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _resolve_path(value: str | Path) -> Path:
    """把配置里的相对路径解析为项目根目录下的绝对路径。"""
    path = Path(value)
    return path if path.is_absolute() else (PROJECT_ROOT / path)


def _number(value: Any, default: float, cast=float) -> Any:
    """把配置值转成数字，类型不对时退回默认值（避免配置文件写错导致程序起不来）。"""
    try:
        return cast(value)
    except (TypeError, ValueError):
        logger.warning("配置值 %r 无法转换为数字，使用默认值 %r", value, default)
        return default


def _printer_settings(printer: dict[str, Any]) -> PrinterSettings:
    """把配置里的 printer 段转成 PrinterSettings（字段缺失用默认值）。"""
    return PrinterSettings(
        sumatra_path=str(printer.get("sumatra_path", "") or "").strip(),
        print_settings=str(printer.get("print_settings", "simplex") or "").strip(),
        timeout_seconds=max(
            1.0,
            _number(
                printer.get("timeout_seconds", PrinterSettings.timeout_seconds),
                PrinterSettings.timeout_seconds,
            ),
        ),
        queue_recheck_seconds=max(
            0.0,
            _number(
                printer.get("queue_recheck_seconds", PrinterSettings.queue_recheck_seconds),
                PrinterSettings.queue_recheck_seconds,
            ),
        ),
    )


def load_config(path: Path | None = None) -> AppConfig:
    """读取配置文件，缺失或损坏时使用默认配置。"""
    config_path = path or CONFIG_FILE
    data = DEFAULT_CONFIG

    if config_path.exists():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = _deep_merge(DEFAULT_CONFIG, loaded)
            else:
                logger.warning("配置文件 %s 内容不是对象，使用默认配置", config_path)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("读取配置文件 %s 失败（%s），使用默认配置", config_path, exc)
    else:
        logger.info("配置文件 %s 不存在，使用默认配置", config_path)

    printer = data.get("printer", {})
    paper = data.get("paper", {})
    duplex = data.get("duplex", {})
    incoming = data.get("incoming", {})
    database = data.get("database", {})
    paths = data.get("paths", {})
    backup = data.get("backup", {})

    return AppConfig(
        printer_name=str(printer.get("name", DEFAULT_CONFIG["printer"]["name"])),
        virtual_printer_name=str(
            printer.get("virtual_printer_name", DEFAULT_CONFIG["printer"]["virtual_printer_name"])
        ),
        printer=_printer_settings(printer),
        paper_size=str(paper.get("size", "A4")),
        sheets_per_batch=max(1, _number(paper.get("sheets_per_batch", 5), 5, int)),
        front_page_order=normalize_front_order(duplex.get("front_page_order")),
        back_page_order=normalize_back_order(duplex.get("back_page_order")),
        flip_direction=normalize_flip_direction(duplex.get("flip_direction")),
        incoming_dir=_resolve_path(paths.get("incoming", "Data/incoming")),
        jobs_dir=_resolve_path(paths.get("jobs", "Data/jobs")),
        database_file=_resolve_path(database.get("file", DEFAULT_CONFIG["database"]["file"])),
        archive_dir=_resolve_path(paths.get("completed", "Data/completed")) / "removed",
        archive_root=_resolve_path(paths.get("completed", "Data/completed")) / "archive",
        backup_dir=_resolve_path(paths.get("backups", "Data/backups")),
        backup=BackupSettings(
            keep=max(1, _number(backup.get("keep", BackupSettings.keep), BackupSettings.keep, int)),
            include_jobs=bool(backup.get("include_jobs", BackupSettings.include_jobs)),
        ),
        incoming=IncomingSettings(
            stability_seconds=_number(
                incoming.get("stability_seconds", IncomingSettings.stability_seconds),
                IncomingSettings.stability_seconds,
            ),
            poll_interval_seconds=max(
                0.05,
                _number(
                    incoming.get("poll_interval_seconds", IncomingSettings.poll_interval_seconds),
                    IncomingSettings.poll_interval_seconds,
                ),
            ),
            max_wait_seconds=_number(
                incoming.get("max_wait_seconds", IncomingSettings.max_wait_seconds),
                IncomingSettings.max_wait_seconds,
            ),
            rescan_interval_seconds=_number(
                incoming.get("rescan_interval_seconds", IncomingSettings.rescan_interval_seconds),
                IncomingSettings.rescan_interval_seconds,
            ),
            min_size_bytes=_number(
                incoming.get("min_size_bytes", IncomingSettings.min_size_bytes),
                IncomingSettings.min_size_bytes,
                int,
            ),
        ),
        raw=data,
    )


def _relative_if_inside(path: Path) -> str:
    """项目内的路径写成相对路径，整个目录搬到别的机器上也能用。"""
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except (OSError, ValueError):
        return str(path)


def config_to_dict(config: AppConfig) -> dict[str, Any]:
    """把 AppConfig 还原成 config.json 的字典（保留配置文件里未知的字段）。"""
    # 必须深拷贝：_deep_merge 只做浅拷贝，直接改会污染 DEFAULT_CONFIG
    data = copy.deepcopy(_deep_merge(DEFAULT_CONFIG, config.raw))
    for section in ("printer", "paper", "duplex", "paths", "database", "backup"):
        if not isinstance(data.get(section), dict):
            data[section] = {}

    data["printer"]["name"] = config.printer_name
    data["printer"]["virtual_printer_name"] = config.virtual_printer_name
    data["printer"]["sumatra_path"] = config.printer.sumatra_path
    data["printer"]["print_settings"] = config.printer.print_settings
    data["printer"]["timeout_seconds"] = config.printer.timeout_seconds
    data["printer"]["queue_recheck_seconds"] = config.printer.queue_recheck_seconds
    data["paper"]["size"] = config.paper_size
    data["paper"]["sheets_per_batch"] = config.sheets_per_batch
    data["duplex"]["mode"] = "manual"
    data["duplex"]["front_page_order"] = config.front_page_order
    data["duplex"]["back_page_order"] = config.back_page_order
    data["duplex"]["flip_direction"] = config.flip_direction
    data["paths"]["incoming"] = _relative_if_inside(config.incoming_dir)
    data["paths"]["jobs"] = _relative_if_inside(config.jobs_dir)
    data["paths"]["database"] = _relative_if_inside(config.database_file.parent)
    data["paths"]["completed"] = _relative_if_inside(config.archive_dir.parent)
    data["paths"]["backups"] = _relative_if_inside(config.backup_dir)
    data["database"]["file"] = _relative_if_inside(config.database_file)
    data["backup"]["keep"] = config.backup.keep
    data["backup"]["include_jobs"] = config.backup.include_jobs
    return data


def write_config(data: dict[str, Any], path: Path | None = None) -> Path:
    """把配置字典原子写入 config.json（先写临时文件，再整体替换）。"""
    config_path = path or CONFIG_FILE
    config_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    handle_fd, temp_name = tempfile.mkstemp(
        prefix="config-", suffix=".json", dir=str(config_path.parent)
    )
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temp_name, config_path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    logger.info("配置已保存：%s", config_path)
    return config_path


def save_config(config: AppConfig, path: Path | None = None) -> Path:
    """把整份配置写入磁盘。"""
    return write_config(config_to_dict(config), path)


def read_config_text(path: Path | None = None) -> str:
    """读回 config.json 的原始文本，给「查看配置文件」之类的界面用。"""
    config_path = path or CONFIG_FILE
    try:
        return config_path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("读取配置文件 %s 失败", config_path, exc_info=True)
        return ""


def update_config(changes: dict[str, Any], path: Path | None = None) -> AppConfig:
    """把界面上改动的字段深合并进 config.json，重新加载后返回新的 AppConfig。"""
    config_path = path or CONFIG_FILE
    current = load_config(config_path)
    data = _deep_merge(config_to_dict(current), changes)
    write_config(data, config_path)
    return load_config(config_path)


def ensure_config_file(path: Path | None = None) -> Path | None:
    """首次运行时把默认配置写成 config.json。

    打包后的程序里没有源仓库的 config/ 目录，这里主动落盘一份，用户之后
    可以直接改文件，也可以在「设置」里改；返回写出的路径，已存在或失败则为 None。
    """
    config_path = path or CONFIG_FILE
    if config_path.exists():
        return None
    try:
        return write_config(config_to_dict(load_config(config_path)), config_path)
    except OSError:
        logger.warning("无法写入默认配置文件 %s", config_path, exc_info=True)
        return None
