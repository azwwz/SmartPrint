"""单页打印文件生成服务（阶段四）。

阶段四要产出的是**按页码命名的单页 PDF**，而不是「一批正反面」的合并文件。
readme 的「五、架构与目录」给出的目录结构就是：

    Data/jobs/<任务号>/original.pdf      原始 PDF 的副本（源文件不动）
    Data/jobs/<任务号>/page_001.pdf      第 1 页
    Data/jobs/<任务号>/page_003.pdf      第 3 页
    Data/jobs/<任务号>/page_005.pdf      第 5 页
    Data/jobs/<任务号>/plan.json         本次拆分的全部参数与结果

为什么是单页：readme 的「三、最重要的设计」要求「逐面提交、逐面确认」，
任何时候最多只有一个已提交但未确认的页面。批次（默认每批 5 张纸，可配置）只是纸张管理
与翻面管理的单位，绝不代表一次提交一整批的页面。所以这里给每个页面单独生成一个
只有 1 页的 PDF，打印程序每次只收到一个页面。

安全约定：

* 源 PDF 只读——生成前后各算一次 SHA-256，确认源文件一个字节都没变；
* 每个输出文件都先写临时文件再改名，中途失败不会留下半个 PDF；
* 每生成一个文件立刻比对内容，错位当场报错，不把问题留到打印之后；
* 每次重新生成都先清掉旧文件，避免页数变少后残留多余页面。
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from app.core.config import AppConfig
from app.core.models import Task, TaskBatch
from app.core.page_planner import (
    DEFAULT_BACK_ORDER,
    DEFAULT_FRONT_ORDER,
    DEFAULT_SHEETS_PER_BATCH,
    FACE_BACK,
    FACE_FRONT,
    BatchPlan,
    PagePlan,
    PrintStep,
    face_label,
    normalize_back_order,
    normalize_front_order,
    page_file_name,
    plan_pages,
    sheet_number_of_page,
)
from app.core.pdf_splitter import (
    PageScan,
    PdfSplitError,
    extract_pages,
    file_sha256,
    scan_pages,
    verify_pages,
)
from app.database.task_repository import TaskRepository
from app.utils.paths import JOBS_DIR

logger = logging.getLogger("smartprint.services.page_jobs")

MANIFEST_NAME = "plan.json"
ORIGINAL_NAME = "original.pdf"

#: 单页文件名的匹配前缀，用于清理旧文件
PAGE_FILE_PREFIX = "page_"

#: 记录到 task_events 的事件类型
EVENT_GENERATED = "pages"
EVENT_CLEARED = "pages_cleared"


class PageJobError(Exception):
    """生成打印文件失败。"""


@dataclass
class GeneratedPage:
    """一个已经生成好的单页 PDF。"""

    page_number: int
    file: Path
    size_bytes: int = 0
    verified: bool = False
    blank: bool = False

    @property
    def sheet_number(self) -> int:
        return sheet_number_of_page(self.page_number)

    @property
    def face(self) -> str:
        return FACE_FRONT if self.page_number % 2 == 1 else FACE_BACK

    @property
    def face_text(self) -> str:
        return face_label(self.face)

    @property
    def label(self) -> str:
        return f"第 {self.page_number} 页（{self.face_text}）"

    def to_dict(self) -> dict[str, object]:
        return {
            "page": self.page_number,
            "sheet": self.sheet_number,
            "face": self.face,
            "file": self.file.name,
            "bytes": self.size_bytes,
            "blank": self.blank,
            "verified": self.verified,
        }


@dataclass
class GenerationResult:
    """一次生成操作的完整结果。"""

    task_uid: str
    document_name: str
    job_dir: Path
    plan: PagePlan
    scan: PageScan
    pages: list[GeneratedPage] = field(default_factory=list)
    original_copy: Path | None = None
    manifest: Path | None = None
    warnings: list[str] = field(default_factory=list)
    source_sha256: str = ""
    generated_at: datetime = field(default_factory=datetime.now)

    # -- 便捷访问 -----------------------------------------------------------

    @property
    def page_count(self) -> int:
        return self.plan.page_count

    @property
    def batches(self) -> tuple[BatchPlan, ...]:
        """本次生成涉及的批次（每批若干张纸，页码与单页文件名都在里面）。"""
        return self.plan.batches

    @property
    def batch_count(self) -> int:
        return self.plan.batch_count

    @property
    def sheet_count(self) -> int:
        return self.plan.sheet_count

    @property
    def page_files(self) -> list[Path]:
        """按页码升序排列的单页文件。"""
        return [page.file for page in sorted(self.pages, key=lambda p: p.page_number)]

    @property
    def page_file_names(self) -> list[str]:
        return [path.name for path in self.page_files]

    @property
    def file_paths(self) -> list[Path]:
        paths: list[Path] = []
        if self.original_copy:
            paths.append(self.original_copy)
        paths.extend(self.page_files)
        if self.manifest:
            paths.append(self.manifest)
        return paths

    @property
    def total_bytes(self) -> int:
        size = 0
        for path in self.file_paths:
            try:
                size += path.stat().st_size
            except OSError:
                continue
        return size

    @property
    def size_text(self) -> str:
        size = self.total_bytes
        if size < 1024:
            return f"{size} B"
        if size < 1024 * 1024:
            return f"{size / 1024:.0f} KB"
        return f"{size / 1024 / 1024:.1f} MB"

    @property
    def verified_count(self) -> int:
        return sum(1 for page in self.pages if page.verified)

    def page_file(self, page_number: int) -> Path | None:
        """取某一页的单页文件；没有生成时返回 None。"""
        for page in self.pages:
            if page.page_number == page_number:
                return page.file
        return None

    def file_for_step(self, step: PrintStep) -> Path | None:
        return self.page_file(step.page_number)

    def missing_files(self) -> list[Path]:
        return [path for path in self.file_paths if not path.is_file()]

    def summary_text(self) -> str:
        return (
            f"{self.plan.summary_text}，共生成 {len(self.pages)} 个单页文件"
            f"（{self.size_text}）"
        )


class PageJobService:
    """按页码生成单页打印文件，并把批次信息写入数据库。"""

    def __init__(
        self,
        repository: TaskRepository | None = None,
        jobs_dir: Path | str | None = None,
        sheets_per_batch: int = DEFAULT_SHEETS_PER_BATCH,
        front_order: str = DEFAULT_FRONT_ORDER,
        back_order: str = DEFAULT_BACK_ORDER,
    ) -> None:
        self.repository = repository
        self.jobs_dir = Path(jobs_dir) if jobs_dir else JOBS_DIR
        self.sheets_per_batch = max(1, int(sheets_per_batch))
        self.front_order = normalize_front_order(front_order)
        self.back_order = normalize_back_order(back_order)

    @classmethod
    def from_config(
        cls, config: AppConfig, repository: TaskRepository | None = None
    ) -> "PageJobService":
        return cls(
            repository=repository,
            jobs_dir=config.jobs_dir,
            sheets_per_batch=config.sheets_per_batch,
            front_order=config.front_page_order,
            back_order=config.back_page_order,
        )

    # -- 目录 ---------------------------------------------------------------

    def job_dir_for(self, task: Task) -> Path:
        """任务目录 Data/jobs/<任务号>/。"""
        return self.job_dir_for_uid(task.task_uid)

    def job_dir_for_uid(self, task_uid: str) -> Path:
        return self.jobs_dir / _safe_dir_name(task_uid)

    def page_file_for(self, task: Task, page_number: int) -> Path:
        """某一页单页文件的路径（不保证已存在）。"""
        return self.job_dir_for(task) / page_file_name(page_number)

    @staticmethod
    def _clean_previous(job_dir: Path) -> list[Path]:
        """删除上一次生成的单页文件、旧版批次文件与清单，返回被删除的文件。"""
        removed: list[Path] = []
        if not job_dir.is_dir():
            return removed
        for path in sorted(job_dir.iterdir()):
            if not path.is_file():
                continue
            name = path.name
            if (
                name.startswith(PAGE_FILE_PREFIX)
                or name.startswith("front_batch_")
                or name.startswith("back_batch_")
                or name == MANIFEST_NAME
                or name.endswith(".part.pdf")
            ):
                try:
                    path.unlink()
                    removed.append(path)
                except OSError as exc:
                    logger.warning("删除旧打印文件失败：%s（%s）", path, exc)
        return removed

    # -- 读取 ---------------------------------------------------------------

    def inspect(self, task: Task) -> PageScan:
        """读取源 PDF 的页面信息；源文件丢失时给出明确错误。"""
        source = _require_source(task)
        try:
            return scan_pages(source)
        except PdfSplitError as exc:
            raise PageJobError(str(exc)) from exc

    def build_plan(
        self,
        task: Task,
        sheets_per_batch: int | None = None,
        page_count: int | None = None,
    ) -> PagePlan:
        """计算打印计划；page_count 为空时读源 PDF。"""
        pages = page_count if page_count is not None else self.inspect(task).page_count
        try:
            return plan_pages(
                pages,
                sheets_per_batch or self.sheets_per_batch,
                self.front_order,
                self.back_order,
            )
        except ValueError as exc:
            raise PageJobError(str(exc)) from exc

    # -- 生成 ---------------------------------------------------------------

    def generate(
        self,
        task: Task,
        sheets_per_batch: int | None = None,
        *,
        copy_original: bool = True,
        record: bool = True,
    ) -> GenerationResult:
        """为每一页生成一个单页 PDF。

        Args:
            task: 要处理的任务。
            sheets_per_batch: 覆盖默认的每批纸张数量（只影响分组，不影响文件）。
            copy_original: 是否把源 PDF 复制一份到任务目录（便于日后复查）。
            record: 是否把批次信息写入数据库并记录事件。

        Raises:
            PageJobError: 源文件不可用、页码校验失败或写入失败。
        """
        source = _require_source(task)
        scan = self.inspect(task)
        plan = self.build_plan(task, sheets_per_batch, scan.page_count)
        job_dir = self.job_dir_for(task)

        try:
            source_sha256 = file_sha256(source)
            job_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PageJobError(f"无法准备任务目录（{job_dir}）：{exc}") from exc

        removed = self._clean_previous(job_dir)
        if removed:
            logger.info("清理旧打印文件 %s 个：%s", len(removed), job_dir)

        original_copy: Path | None = None
        if copy_original:
            original_copy = job_dir / ORIGINAL_NAME
            try:
                shutil.copy2(source, original_copy)
            except OSError as exc:
                raise PageJobError(f"复制原始 PDF 失败：{exc}") from exc

        blank_pages = set(scan.blank_pages)
        pages: list[GeneratedPage] = []
        for page_number in range(1, plan.page_count + 1):
            pages.append(self._generate_page(source, job_dir, page_number, task, blank_pages))

        if file_sha256(source) != source_sha256:
            raise PageJobError("源 PDF 在生成过程中被改动，已中止（请检查是否有其他程序在写该文件）")

        manifest = self._write_manifest(
            task=task,
            job_dir=job_dir,
            plan=plan,
            scan=scan,
            pages=pages,
            source_sha256=source_sha256,
        )

        result = GenerationResult(
            task_uid=task.task_uid,
            document_name=task.document_name,
            job_dir=job_dir,
            plan=plan,
            scan=scan,
            pages=pages,
            original_copy=original_copy,
            manifest=manifest,
            warnings=list(scan.warnings),
            source_sha256=source_sha256,
        )

        if record:
            self._record(task, result)
        logger.info(
            "任务 %s 已生成单页打印文件：%s（目录 %s）",
            task.task_uid,
            result.summary_text(),
            job_dir,
        )
        return result

    def ensure_page_file(self, task: Task, page_number: int) -> Path:
        """确保某一页的单页 PDF 存在，返回其路径。

        阶段五/六每次只打印一个页面，用这个方法按需生成/复用单页文件：
        文件已存在且内容正确时直接返回，不重复写盘。
        """
        source = _require_source(task)
        try:
            scan = scan_pages(source)
        except PdfSplitError as exc:
            raise PageJobError(str(exc)) from exc
        if not 1 <= int(page_number) <= scan.page_count:
            raise PageJobError(
                f"页码 {page_number} 超出范围（源文件共 {scan.page_count} 页）"
            )

        job_dir = self.job_dir_for(task)
        target = job_dir / page_file_name(page_number)
        if target.is_file() and not verify_pages(target, (int(page_number),), source):
            return target

        try:
            job_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PageJobError(f"无法准备任务目录（{job_dir}）：{exc}") from exc
        return self._extract(source, target, (int(page_number),), f"{task.document_name} 第 {page_number} 页")

    def _generate_page(
        self,
        source: Path,
        job_dir: Path,
        page_number: int,
        task: Task,
        blank_pages: set[int],
    ) -> GeneratedPage:
        target = job_dir / page_file_name(page_number)
        path = self._extract(
            source, target, (page_number,), f"{task.document_name} 第 {page_number} 页"
        )
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        return GeneratedPage(
            page_number=page_number,
            file=path,
            size_bytes=size,
            verified=True,
            blank=page_number in blank_pages,
        )

    @staticmethod
    def _extract(
        source: Path, target: Path, page_numbers: tuple[int, ...], title: str
    ) -> Path:
        """提取一个页面并立即校验，返回目标文件路径。"""
        try:
            extract_pages(source, target, page_numbers, title=title)
        except PdfSplitError as exc:
            raise PageJobError(f"生成 {target.name} 失败：{exc}") from exc

        problems = verify_pages(target, page_numbers, source)
        if problems:
            raise PageJobError(f"{target.name} 页码校验失败：" + "；".join(problems))
        return target

    def _write_manifest(
        self,
        *,
        task: Task,
        job_dir: Path,
        plan: PagePlan,
        scan: PageScan,
        pages: list[GeneratedPage],
        source_sha256: str,
    ) -> Path | None:
        manifest = job_dir / MANIFEST_NAME
        data = {
            "task_uid": task.task_uid,
            "document_name": task.document_name,
            "source_path": str(task.source_path),
            "source_sha256": source_sha256,
            "page_count": plan.page_count,
            "paper_size": scan.paper_size,
            "sheets_per_batch": plan.sheets_per_batch,
            "front_order": plan.front_order,
            "back_order": plan.back_order,
            "sheet_count": plan.sheet_count,
            "batch_count": plan.batch_count,
            "blank_pages": list(scan.blank_pages),
            "warnings": list(scan.warnings),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "file_naming": "page_<页码>.pdf（每个文件只有一个页面）",
            "pages": [page.to_dict() for page in sorted(pages, key=lambda p: p.page_number)],
            "batches": [_batch_dict(batch) for batch in plan.batches],
        }
        try:
            manifest.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning("写入打印计划失败：%s（%s）", manifest, exc)
            return None
        return manifest

    def _record(self, task: Task, result: GenerationResult) -> None:
        """把批次写入数据库并记录一条事件。"""
        if self.repository is None:
            return
        created_at = result.generated_at
        job_dir = result.job_dir
        batches = [
            _to_task_batch(batch, task.task_uid, job_dir, created_at)
            for batch in result.plan.batches
        ]
        self.repository.replace_batches(task.task_uid, batches)
        message = (
            f"已生成单页打印文件：{result.plan.page_count} 页 → {result.sheet_count} 张纸 → "
            f"{result.batch_count} 批（每批 {result.plan.sheets_per_batch} 张），"
            f"{len(result.pages)} 个 page_XXX.pdf，目录 {job_dir.name}"
        )
        if result.warnings:
            message += "；提示：" + "；".join(result.warnings)
        self.repository.add_event(task.task_uid, EVENT_GENERATED, message)

    # -- 查询与清理 ---------------------------------------------------------

    def existing_batches(self, task_uid: str) -> list[TaskBatch]:
        if self.repository is None:
            return []
        return self.repository.list_batches(task_uid)

    def existing_page_files(self, task_uid: str) -> dict[int, Path]:
        """扫描任务目录里已存在的单页文件：{页码: 路径}。"""
        job_dir = self.job_dir_for_uid(task_uid)
        found: dict[int, Path] = {}
        if not job_dir.is_dir():
            return found
        for path in job_dir.glob(f"{PAGE_FILE_PREFIX}*.pdf"):
            number = _parse_page_file_number(path.name)
            if number is not None:
                found[number] = path
        return dict(sorted(found.items()))

    def remove_files(self, task_uid: str) -> int:
        """只删除任务目录下的文件，返回删除的文件数（不动数据库）。"""
        job_dir = self.job_dir_for_uid(task_uid)
        if not job_dir.is_dir():
            return 0
        removed = sum(1 for path in job_dir.rglob("*") if path.is_file())
        try:
            shutil.rmtree(job_dir, ignore_errors=False)
        except OSError as exc:
            raise PageJobError(f"无法删除任务目录（{job_dir}）：{exc}") from exc
        logger.info("任务 %s 的打印文件已删除：%s 个文件", task_uid, removed)
        return removed

    def clear(self, task_uid: str, *, remove_files: bool = True) -> int:
        """删除任务目录下的打印文件与数据库批次记录，返回删除的文件数。"""
        removed = self.remove_files(task_uid) if remove_files else 0
        if self.repository is not None:
            self.repository.clear_batches(task_uid)
            if removed:
                self.repository.add_event(
                    task_uid, EVENT_CLEARED, f"已删除 {removed} 个打印文件"
                )
        return removed


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _batch_dict(batch: BatchPlan) -> dict[str, object]:
    return {
        "index": batch.index,
        "total_batches": batch.total_batches,
        "sheet_count": batch.sheet_count,
        "front_pages": list(batch.front_pages),
        "back_pages": list(batch.back_pages),
        "front_files": list(batch.front_file_names),
        "back_files": list(batch.back_file_names),
    }


def _to_task_batch(
    batch: BatchPlan, task_uid: str, job_dir: Path, created_at: datetime
) -> TaskBatch:
    """把计算用的 BatchPlan 转成可入库的 TaskBatch。

    page_files 保存本批每个页面对应的单页文件（正面在前、背面在后），
    与 readme「每次只提交一个页面」的打印顺序一致。
    """
    files = tuple(job_dir / name for name in batch.file_names)
    return TaskBatch(
        task_uid=task_uid,
        batch_index=batch.index,
        total_batches=batch.total_batches,
        sheet_count=batch.sheet_count,
        front_pages=batch.front_pages,
        back_pages=batch.back_pages,
        page_files=files,
        created_at=created_at,
    )


def _parse_page_file_number(name: str) -> int | None:
    """从 ``page_007.pdf`` 里取出 7；不是单页文件时返回 None。"""
    if not name.startswith(PAGE_FILE_PREFIX) or not name.endswith(".pdf"):
        return None
    digits = name[len(PAGE_FILE_PREFIX) : -len(".pdf")]
    return int(digits) if digits.isdigit() else None


def _safe_dir_name(task_uid: str) -> str:
    """任务号只保留安全字符，避免出现路径分隔符。"""
    cleaned = "".join(ch for ch in str(task_uid) if ch.isalnum() or ch in "-_")
    return cleaned or "unknown-task"


def _require_source(task: Task) -> Path:
    source = Path(task.source_path)
    if not source.is_file():
        raise PageJobError(f"源 PDF 不存在：{source}")
    return source
