"""任务数据模型。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from app.core.page_planner import page_file_name
from app.core.task_status import TaskStatus


@dataclass
class Task:
    """一个打印任务（对应 PDF24 送入 incoming 目录的一份 PDF）。"""

    task_uid: str                      # SmartPrint 内部任务号，例如 SP-20261105-195741-A1B2
    file_name: str                     # PDF24 生成的完整文件名
    document_name: str                 # 从文件名中解析出的原始文档名
    source_path: Path                  # incoming 目录中的 PDF 路径
    file_size: int
    page_count: int
    paper_size: str
    orientation: str
    width_pt: float
    height_pt: float
    created_at: datetime
    updated_at: datetime
    status: TaskStatus = TaskStatus.PENDING
    source_job_id: str | None = None   # PDF24 文件名中的 $id，用于区分重复提交
    file_hash: str | None = None       # 文件 SHA-256，便于后续校验与去重
    note: str | None = None            # 接收时的提示信息（例如混合页面尺寸）
    id: int | None = None              # 数据库自增主键

    @property
    def status_label(self) -> str:
        return self.status.label

    @property
    def size_text(self) -> str:
        return f"{self.paper_size} {self.orientation}"

    @property
    def created_at_text(self) -> str:
        return self.created_at.strftime("%Y-%m-%d %H:%M:%S")

    @property
    def updated_at_text(self) -> str:
        return self.updated_at.strftime("%Y-%m-%d %H:%M:%S")

    @property
    def estimated_sheets(self) -> int:
        """手动双面打印预计使用的纸张数量（一张纸最多两个页面）。

        阶段三只用于界面展示，真正的批次拆分在阶段四实现。
        """
        if self.page_count <= 0:
            return 0
        return math.ceil(self.page_count / 2)

    def estimated_batches(self, sheets_per_batch: int = 5) -> int:
        """按每批纸张数量估算批次数（最后一批不足也单独算一批）。"""
        sheets = self.estimated_sheets
        if sheets <= 0:
            return 0
        return math.ceil(sheets / max(1, sheets_per_batch))

    @property
    def source_exists(self) -> bool:
        """源 PDF 是否还在原处（可能在程序外被移动或删除）。"""
        try:
            return self.source_path.is_file()
        except OSError:
            return False


@dataclass
class TaskEvent:
    """任务事件记录（任务创建、状态变化、用户操作等）。"""

    task_uid: str
    event_type: str
    message: str
    created_at: datetime
    id: int | None = None


@dataclass
class TaskBatch:
    """一份 PDF 的打印批次（阶段四）。

    注意：一个批次只是「纸张管理与翻面管理的单位」，不是提交单位。
    批次里的每个页面各自对应一个单页 PDF（page_001.pdf、page_003.pdf …），
    所以这里保存的是文件列表，而不是「本批正面 PDF / 背面 PDF」。

    页码与 app/core/page_planner.py 保持一致，均为从 1 开始的 PDF 页码。
    """

    task_uid: str
    batch_index: int
    total_batches: int
    sheet_count: int
    front_pages: tuple[int, ...]
    back_pages: tuple[int, ...] = ()
    page_files: tuple[Path, ...] = ()      # 本批涉及的单页文件，正面在前、背面在后
    created_at: datetime = field(default_factory=datetime.now)
    id: int | None = None

    @property
    def is_last(self) -> bool:
        return self.batch_index == self.total_batches

    @property
    def has_back(self) -> bool:
        return bool(self.back_pages)

    @property
    def page_count(self) -> int:
        return len(self.front_pages) + len(self.back_pages)

    @property
    def front_pages_text(self) -> str:
        return "、".join(str(page) for page in self.front_pages) or "无"

    @property
    def back_pages_text(self) -> str:
        return "、".join(str(page) for page in self.back_pages) or "无"

    @property
    def status_text(self) -> str:
        tags = []
        if self.is_last:
            tags.append("最后一批")
        if not self.has_back:
            tags.append("只有正面")
        return "、".join(tags) if tags else "—"

    @property
    def front_file_names(self) -> tuple[str, ...]:
        """本批正面各页的单页文件名。"""
        return tuple(page_file_name(page) for page in self.front_pages)

    @property
    def back_file_names(self) -> tuple[str, ...]:
        """本批背面各页的单页文件名。"""
        return tuple(page_file_name(page) for page in self.back_pages)

    @property
    def file_names(self) -> tuple[str, ...]:
        return tuple(path.name for path in self.page_files)

    def missing_files(self) -> list[Path]:
        """返回已经不存在的单页文件（用户可能在程序外删除）。"""
        missing: list[Path] = []
        for path in self.page_files:
            try:
                if not path.is_file():
                    missing.append(path)
            except OSError:
                missing.append(path)
        return missing

    def summary_text(self) -> str:
        return (
            f"第 {self.batch_index}/{self.total_batches} 批：{self.sheet_count} 张纸，"
            f"正面 {self.front_pages_text}，背面 {self.back_pages_text}"
        )


# ---------------------------------------------------------------------------
# 任务接收过程中的通知，供界面轮询消费（避免在后台线程直接操作 Qt 控件）
# ---------------------------------------------------------------------------


@dataclass
class IntakeNotification:
    """任务接收服务产生的一条通知。"""

    kind: str                          # registered / duplicate / failed / info / watcher
    message: str
    task: Task | None = None
    created_at: datetime = field(default_factory=datetime.now)
