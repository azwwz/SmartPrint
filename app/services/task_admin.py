"""任务管理操作：状态变更、删除任务、源文件归档。

阶段三的界面只负责「显示什么」，真正的任务管理动作放在这里，
这样界面、命令行和验收脚本可以共用同一套逻辑，也方便单元测试。

两个需要特别注意的地方（对应 readme「九、必须遵守的原则」）：

1. 状态变化必须记录到 task_events，否则以后说不清任务是怎么走到这一步的。
2. 删除任务时必须同时处理 incoming 目录里的源 PDF。否则下一次定时扫描会
   把这个文件重新登记成一个新任务，用户会觉得「删不掉」。默认做法是把源文件
   移动到 `Data/completed/removed/` 留档，只有用户明确勾选才会真正删除文件。
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Iterable

from app.core.models import Task
from app.core.task_status import TaskStatus, parse_status
from app.database.task_repository import TaskRepository
from app.utils.paths import COMPLETED_DIR, INCOMING_DIR

logger = logging.getLogger("smartprint.services.task_admin")

#: 允许的状态流转。阶段六起，打印相关状态由 DuplexPrintService 驱动：
#: 正面 → 等待翻面 → 背面 → （下一批正面 / 完成），暂停后可以回到原来的阶段。
#: 阶段七起增加「待补打」（REPRINT）：正常循环里作废的纸张进入补打循环，
#: 补打期间任务状态为待补打，补打完成最终回到已完成。
ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset(
        {TaskStatus.READY, TaskStatus.PRINTING_FRONT, TaskStatus.CANCELLED}
    ),
    TaskStatus.READY: frozenset(
        {TaskStatus.PENDING, TaskStatus.PRINTING_FRONT, TaskStatus.PAUSED, TaskStatus.CANCELLED}
    ),
    TaskStatus.PRINTING_FRONT: frozenset(
        {
            TaskStatus.WAITING_FLIP,
            TaskStatus.PRINTING_BACK,
            TaskStatus.REPRINT,
            TaskStatus.PAUSED,
            TaskStatus.ERROR,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.WAITING_FLIP: frozenset(
        {
            TaskStatus.PRINTING_FRONT,
            TaskStatus.PRINTING_BACK,
            TaskStatus.REPRINT,
            TaskStatus.PAUSED,
            TaskStatus.ERROR,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.PRINTING_BACK: frozenset(
        {
            TaskStatus.PRINTING_FRONT,
            TaskStatus.WAITING_FLIP,
            TaskStatus.REPRINT,
            TaskStatus.COMPLETED,
            TaskStatus.PAUSED,
            TaskStatus.ERROR,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.REPRINT: frozenset(
        {
            TaskStatus.PRINTING_FRONT,
            TaskStatus.WAITING_FLIP,
            TaskStatus.PRINTING_BACK,
            TaskStatus.REPRINT,
            TaskStatus.COMPLETED,
            TaskStatus.PAUSED,
            TaskStatus.ERROR,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.PAUSED: frozenset(
        {
            TaskStatus.READY,
            TaskStatus.PENDING,
            TaskStatus.PRINTING_FRONT,
            TaskStatus.WAITING_FLIP,
            TaskStatus.PRINTING_BACK,
            TaskStatus.REPRINT,
            TaskStatus.ERROR,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.ERROR: frozenset(
        {
            TaskStatus.READY,
            TaskStatus.PENDING,
            TaskStatus.PRINTING_FRONT,
            TaskStatus.WAITING_FLIP,
            TaskStatus.PRINTING_BACK,
            TaskStatus.REPRINT,
            TaskStatus.PAUSED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.CANCELLED: frozenset({TaskStatus.PENDING, TaskStatus.READY}),
}

#: 删除任务时对源文件采取的动作
FILE_DELETED = "deleted"      # 用户选择直接删除文件
FILE_ARCHIVED = "archived"    # 默认：移动到 completed/removed 留档
FILE_KEPT = "kept"            # 文件不在 incoming 目录内，保持原样
FILE_MISSING = "missing"      # 文件本来就不存在


class TaskAdminError(Exception):
    """任务管理操作失败。"""


@dataclass
class DeleteOutcome:
    """单个任务的删除结果。"""

    task_uid: str
    document_name: str
    record_deleted: bool = False
    file_action: str = FILE_KEPT
    file_path: Path | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.record_deleted and self.error is None

    @property
    def file_action_text(self) -> str:
        return {
            FILE_DELETED: "已删除源文件",
            FILE_ARCHIVED: "源文件已归档",
            FILE_KEPT: "源文件保留原处",
            FILE_MISSING: "源文件已不存在",
        }.get(self.file_action, self.file_action)


class TaskAdminService:
    """任务状态变更与删除。"""

    def __init__(
        self,
        repository: TaskRepository,
        incoming_dir: Path | None = None,
        archive_dir: Path | None = None,
    ) -> None:
        self.repository = repository
        self.incoming_dir = Path(incoming_dir or INCOMING_DIR)
        self.archive_dir = Path(archive_dir or (COMPLETED_DIR / "removed"))

    # -- 状态 ---------------------------------------------------------------

    def can_transition(self, current: TaskStatus | str, target: TaskStatus | str) -> bool:
        current_status = parse_status(current)
        target_status = parse_status(target)
        if current_status is target_status:
            return False
        return target_status in ALLOWED_TRANSITIONS.get(current_status, frozenset())

    def set_status(
        self,
        task_uid: str,
        target: TaskStatus | str,
        message: str | None = None,
    ) -> Task:
        """把任务切换到目标状态，并写入一条事件记录。

        状态不允许时抛出 TaskAdminError（而不是悄悄改掉）。
        """
        task = self.repository.get_task(task_uid)
        if task is None:
            raise TaskAdminError(f"任务不存在：{task_uid}")

        new_status = parse_status(target)
        if not self.can_transition(task.status, new_status):
            raise TaskAdminError(
                f"任务 {task_uid} 当前状态为「{task.status.label}」，"
                f"不能变更为「{new_status.label}」"
            )

        self.repository.update_status(task_uid, new_status)
        detail = f"状态由「{task.status.label}」变更为「{new_status.label}」"
        if message:
            detail = f"{detail}（{message}）"
        event = self.repository.add_event(task_uid, "status", detail)
        logger.info("%s：%s", task_uid, detail)
        return replace(task, status=new_status, updated_at=event.created_at)

    # -- 删除 ---------------------------------------------------------------

    def delete_tasks(
        self,
        task_uids: Iterable[str],
        delete_files: bool = False,
    ) -> list[DeleteOutcome]:
        """删除任务记录，并按需要处理源 PDF。

        `delete_files=False`（默认）把 incoming 中的源 PDF 移动到归档目录，
        既避免被重新扫描登记，又保留原始文件；`True` 则直接删除文件。
        """
        outcomes: list[DeleteOutcome] = []
        for task_uid in task_uids:
            outcomes.append(self._delete_one(task_uid, delete_files))
        return outcomes

    def _delete_one(self, task_uid: str, delete_file: bool) -> DeleteOutcome:
        task = self.repository.get_task(task_uid)
        if task is None:
            return DeleteOutcome(
                task_uid=task_uid,
                document_name="",
                error=f"任务不存在：{task_uid}",
            )

        outcome = DeleteOutcome(task_uid=task.task_uid, document_name=task.document_name)

        # 先处理文件：文件处理失败就保留任务记录，避免留下「扫描还会重新登记」的隐患
        try:
            outcome.file_action, outcome.file_path = self._handle_source_file(task, delete_file)
        except OSError as exc:
            outcome.error = f"处理源文件失败：{exc}"
            logger.error("删除任务 %s 失败：%s", task_uid, outcome.error)
            return outcome

        try:
            outcome.record_deleted = self.repository.delete_task(task.task_uid)
        except Exception as exc:  # 数据库异常
            outcome.error = f"删除任务记录失败：{exc}"
            logger.exception("删除任务记录失败：%s", task_uid)
            return outcome

        if not outcome.record_deleted:
            outcome.error = "任务记录不存在或已被删除"

        logger.info(
            "已删除任务 %s（%s）：%s",
            task.task_uid,
            task.document_name,
            outcome.file_action_text,
        )
        return outcome

    def _handle_source_file(self, task: Task, delete_file: bool) -> tuple[str, Path | None]:
        """处理任务的源 PDF，返回（动作, 文件路径）。"""
        path = Path(task.source_path)
        if not path.is_file():
            return FILE_MISSING, None

        if delete_file:
            path.unlink()
            logger.info("已删除源文件：%s", path)
            return FILE_DELETED, None

        if not self._is_inside_incoming(path):
            # 不在 incoming 目录里的文件不会造成重复登记，保持原样更安全
            return FILE_KEPT, path

        target = self._unique_archive_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(target))
        logger.info("源文件已归档：%s → %s", path, target)
        return FILE_ARCHIVED, target

    def _is_inside_incoming(self, path: Path) -> bool:
        try:
            return path.resolve().is_relative_to(self.incoming_dir.resolve())
        except OSError:
            return False

    def _unique_archive_path(self, source: Path) -> Path:
        """归档路径：completed/removed/YYYYMMDD/时间戳_原文件名，重名时自动加序号。"""
        day_dir = self.archive_dir / datetime.now().strftime("%Y%m%d")
        stamp = datetime.now().strftime("%H%M%S")
        candidate = day_dir / f"{stamp}_{source.name}"
        index = 1
        while candidate.exists():
            candidate = day_dir / f"{stamp}_{index}_{source.name}"
            index += 1
        return candidate
