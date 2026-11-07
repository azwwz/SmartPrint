"""数据备份与还原（阶段八）。

readme 阶段八要求「增加数据备份」。打印任务记录一旦丢失，用户就无法知道哪些纸已经
打完，所以备份的重点是**数据库**和**配置**：

- 数据库用 SQLite 自带的 `backup()` 接口导出，即使程序正在运行（WAL 模式）也能得到一致的快照；
- 配置（config/config.json）一并保存，还原时设置也回到备份时的状态；
- 单页 PDF（Data/jobs）体积大，默认不打包，需要时可以在设置里打开；
- 每份备份是一个 zip，里面带 manifest.json，记录时间、版本、任务数量，便于事后核对。

备份只做「复制」，不会移动或删除任何原始文件；还原之前会先把当前数据库另存一份
`*.before-restore-时间戳`，避免还原错了没有退路。
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.core.version import APP_VERSION

logger = logging.getLogger("smartprint.services.backup")

#: 备份文件名前缀
BACKUP_PREFIX = "smartprint-backup-"
#: 备份包内的清单文件名
MANIFEST_NAME = "manifest.json"
#: 备份包内的数据库文件名
DATABASE_MEMBER = "smartprint.db"
#: 备份包内的配置文件名
CONFIG_MEMBER = "config.json"


class BackupError(RuntimeError):
    """备份或还原失败。"""


@dataclass(frozen=True)
class BackupEntry:
    """一份备份文件。"""

    path: Path
    created_at: datetime
    label: str = ""
    size_bytes: int = 0
    include_jobs: bool = False
    task_count: int | None = None
    schema_version: int | None = None
    app_version: str = ""

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def created_text(self) -> str:
        return self.created_at.strftime("%Y-%m-%d %H:%M:%S")

    @property
    def size_text(self) -> str:
        size = float(self.size_bytes)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} GB"

    @property
    def tasks_text(self) -> str:
        if self.task_count is None:
            return "任务数未知"
        return f"{self.task_count} 个任务"

    @property
    def summary_text(self) -> str:
        parts = [self.created_text, self.size_text, self.tasks_text]
        if self.include_jobs:
            parts.append("含单页文件")
        if self.label:
            parts.append(self.label)
        return "，".join(parts)


def _parse_timestamp(name: str) -> datetime:
    """从备份文件名里解析时间戳，失败时退回文件修改时间。"""
    stem = name[len(BACKUP_PREFIX) :] if name.startswith(BACKUP_PREFIX) else name
    stem = stem.removesuffix(".zip")
    stamp = stem.split("-")
    if len(stamp) >= 3:
        try:
            return datetime.strptime("-".join(stamp[:3]), "%Y%m%d-%H%M%S")
        except ValueError:
            pass
    return datetime.fromtimestamp(0)


class BackupService:
    """把数据库、配置和（可选的）单页文件打包成 zip。"""

    def __init__(
        self,
        database_file: Path,
        backup_dir: Path,
        *,
        config_file: Path | None = None,
        jobs_dir: Path | None = None,
        keep: int = 10,
        include_jobs: bool = False,
        clock=None,
    ) -> None:
        self.database_file = Path(database_file)
        self.backup_dir = Path(backup_dir)
        self.config_file = Path(config_file) if config_file else None
        self.jobs_dir = Path(jobs_dir) if jobs_dir else None
        self.keep = max(1, int(keep))
        self.include_jobs = bool(include_jobs)
        self._clock = clock or datetime.now

    # -- 备份 ---------------------------------------------------------------

    def create(self, label: str = "", *, include_jobs: bool | None = None) -> BackupEntry:
        """创建一份备份，返回备份条目。"""
        if not self.database_file.exists():
            raise BackupError(f"数据库文件不存在，无法备份：{self.database_file}")
        with_jobs = self.include_jobs if include_jobs is None else bool(include_jobs)

        self.backup_dir.mkdir(parents=True, exist_ok=True)
        now = self._clock()
        suffix = f"-{label}" if label else ""
        target = self.backup_dir / f"{BACKUP_PREFIX}{now:%Y%m%d-%H%M%S}{suffix}.zip"
        index = 1
        while target.exists():
            index += 1
            target = self.backup_dir / f"{BACKUP_PREFIX}{now:%Y%m%d-%H%M%S}-{index}{suffix}.zip"

        snapshot = self.backup_dir / f".{DATABASE_MEMBER}.{now:%Y%m%d%H%M%S}.tmp"
        try:
            self._snapshot_database(snapshot)
            task_count, schema_version = self._inspect_database(snapshot)
            manifest = {
                "created_at": now.isoformat(timespec="seconds"),
                "app_version": APP_VERSION,
                "schema_version": schema_version,
                "task_count": task_count,
                "include_jobs": with_jobs,
                "label": label,
                "database_file": str(self.database_file),
            }
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(snapshot, DATABASE_MEMBER)
                if self.config_file and self.config_file.exists():
                    archive.write(self.config_file, CONFIG_MEMBER)
                self._write_jobs(archive, with_jobs)
                archive.writestr(
                    MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2)
                )
        except (OSError, sqlite3.Error, zipfile.BadZipFile) as exc:
            target.unlink(missing_ok=True)
            raise BackupError(f"创建备份失败：{exc}") from exc
        finally:
            self._discard_snapshot(snapshot)

        logger.info("已创建备份：%s（%s）", target, "含单页文件" if with_jobs else "仅数据库")
        entry = self._entry_for(target)
        self.prune()
        return entry

    @staticmethod
    def _discard_snapshot(snapshot: Path) -> None:
        """删掉临时快照，连同 SQLite 可能留下的 -wal / -shm / -journal。

        WAL 模式下打开快照会生成同名的 -wal 与 -shm 文件，只删主文件会在
        备份目录里留下垃圾文件（用户看不出这是什么），所以一起清掉。
        """
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(snapshot) + suffix).unlink(missing_ok=True)

    def _snapshot_database(self, target: Path) -> None:
        """用 SQLite 的 backup 接口导出一致快照（WAL 模式下也安全）。"""
        target.parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(self.database_file)
        try:
            destination = sqlite3.connect(target)
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()

    @staticmethod
    def _inspect_database(path: Path) -> tuple[int | None, int | None]:
        """读出备份里的任务数量和 schema 版本。"""
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error:  # pragma: no cover - 打不开就当作未知
            return None, None
        try:
            task_count = None
            schema_version = None
            try:
                task_count = int(connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
            except sqlite3.Error:
                pass
            try:
                row = connection.execute(
                    "SELECT value FROM schema_info WHERE key = 'schema_version'"
                ).fetchone()
                if row and row[0] is not None:
                    schema_version = int(row[0])
            except (sqlite3.Error, ValueError):
                pass
            return task_count, schema_version
        finally:
            connection.close()

    def _write_jobs(self, archive: zipfile.ZipFile, include_jobs: bool) -> None:
        """把 Data/jobs 下的单页文件放进备份包（可选）。"""
        if not include_jobs or not self.jobs_dir or not self.jobs_dir.is_dir():
            return
        for path in sorted(self.jobs_dir.rglob("*")):
            if path.is_file():
                archive.write(path, f"jobs/{path.relative_to(self.jobs_dir).as_posix()}")

    # -- 查询 ---------------------------------------------------------------

    def list_backups(self) -> list[BackupEntry]:
        """按时间倒序列出所有备份（同一秒内生成的按文件先后再排）。"""
        if not self.backup_dir.is_dir():
            return []
        entries = [
            self._entry_for(path)
            for path in self.backup_dir.glob(f"{BACKUP_PREFIX}*.zip")
            if path.is_file()
        ]
        entries.sort(key=self._order_key, reverse=True)
        return entries

    @staticmethod
    def _order_key(entry: BackupEntry) -> tuple:
        """排序键：先看备份时间，同一秒内再看文件修改时间和文件名。"""
        try:
            modified = entry.path.stat().st_mtime
        except OSError:
            modified = 0.0
        return (entry.created_at, modified, entry.path.name)

    def latest(self) -> BackupEntry | None:
        entries = self.list_backups()
        return entries[0] if entries else None

    def _entry_for(self, path: Path) -> BackupEntry:
        """读取备份包里的 manifest；没有清单时用文件名推断。"""
        created_at = _parse_timestamp(path.name)
        label = ""
        include_jobs = False
        task_count: int | None = None
        schema_version: int | None = None
        app_version = ""
        try:
            with zipfile.ZipFile(path) as archive:
                if MANIFEST_NAME in archive.namelist():
                    manifest = json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))
                    if isinstance(manifest, dict):
                        stamp = manifest.get("created_at")
                        if isinstance(stamp, str):
                            try:
                                created_at = datetime.fromisoformat(stamp)
                            except ValueError:
                                pass
                        label = str(manifest.get("label") or "")
                        include_jobs = bool(manifest.get("include_jobs"))
                        count = manifest.get("task_count")
                        task_count = int(count) if isinstance(count, int) else None
                        version = manifest.get("schema_version")
                        schema_version = int(version) if isinstance(version, int) else None
                        app_version = str(manifest.get("app_version") or "")
        except (OSError, zipfile.BadZipFile, json.JSONDecodeError, ValueError) as exc:
            logger.warning("读取备份清单失败（%s）：%s", path, exc)

        try:
            size_bytes = path.stat().st_size
        except OSError:
            size_bytes = 0
        return BackupEntry(
            path=path,
            created_at=created_at,
            label=label,
            size_bytes=size_bytes,
            include_jobs=include_jobs,
            task_count=task_count,
            schema_version=schema_version,
            app_version=app_version,
        )

    # -- 还原与清理 ---------------------------------------------------------

    def restore(self, path: Path, *, database_file: Path | None = None,
                config_file: Path | None = None) -> Path:
        """用备份覆盖数据库（和配置），返回数据库文件路径。

        覆盖前会先把当前文件另存为 `*.before-restore-<时间戳>`，还原出问题可以手工换回来。
        """
        path = Path(path)
        if not path.exists():
            raise BackupError(f"备份文件不存在：{path}")
        target_db = Path(database_file or self.database_file)
        target_config = Path(config_file) if config_file else self.config_file

        stamp = self._clock().strftime("%Y%m%d-%H%M%S")
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                if DATABASE_MEMBER not in names:
                    raise BackupError(f"备份包里没有数据库：{path.name}")
                target_db.parent.mkdir(parents=True, exist_ok=True)
                if target_db.exists():
                    shutil.copy2(target_db, target_db.with_suffix(f".before-restore-{stamp}.db"))
                temporary = target_db.with_suffix(".restoring")
                temporary.write_bytes(archive.read(DATABASE_MEMBER))
                # 还原前确认这份文件确实是 SmartPrint 的数据库
                self._inspect_database(temporary)
                for leftover in ("-wal", "-shm", "-journal"):
                    Path(str(temporary) + leftover).unlink(missing_ok=True)
                for suffix in ("-wal", "-shm", "-journal"):
                    Path(str(target_db) + suffix).unlink(missing_ok=True)
                shutil.move(str(temporary), str(target_db))
                if target_config and CONFIG_MEMBER in names:
                    target_config.parent.mkdir(parents=True, exist_ok=True)
                    if target_config.exists():
                        target_config.with_suffix(
                            f".before-restore-{stamp}.json"
                        ).write_bytes(target_config.read_bytes())
                    target_config.write_bytes(archive.read(CONFIG_MEMBER))
        except (OSError, zipfile.BadZipFile) as exc:
            raise BackupError(f"还原备份失败：{exc}") from exc

        logger.info("已用备份还原：%s → %s", path.name, target_db)
        return target_db

    def prune(self, keep: int | None = None) -> list[Path]:
        """只保留最近的若干份备份，返回被删除的文件。"""
        limit = max(1, int(keep if keep is not None else self.keep))
        entries = self.list_backups()
        removed: list[Path] = []
        for entry in entries[limit:]:
            try:
                entry.path.unlink()
                removed.append(entry.path)
                logger.info("已删除旧备份：%s", entry.path.name)
            except OSError as exc:  # pragma: no cover - 文件被占用时跳过
                logger.warning("删除旧备份失败（%s）：%s", entry.path, exc)
        return removed

    def summary_text(self) -> str:
        """给界面用的一句话摘要。"""
        entries = self.list_backups()
        if not entries:
            return f"还没有备份（目录 {self.backup_dir}）"
        latest = entries[0]
        return f"共 {len(entries)} 份备份，最近一份 {latest.created_text}（{latest.size_text}）"
