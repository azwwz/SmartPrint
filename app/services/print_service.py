"""单页打印服务（阶段五）。

职责：把**一个页面**的单页 PDF 交给 **一台指定打印机**，并把过程记录下来。

    page_005.pdf  →  SumatraPDF  →  Windows 打印系统  →  HP LaserJet 1020

对应 readme 阶段五的要求：获取打印机名称、选择目标打印机、排除 PDF24、
调用 SumatraPDF、指定单页 PDF、指定目标打印机、发起打印任务、获取进程
退出状态、观察打印队列、记录打印命令和相关状态。

四条不可越过的边界
------------------

1. **绝不把页面发给虚拟打印机。** PDF24 是本项目的输入设备，发给它只会
   得到另一份 PDF。Microsoft Print to PDF、OneNote、Fax 等同理。
2. **绝不自动重试。** 命令发出去之后，程序无法知道纸上到底印了什么；
   自动重发可能造成重复打印、浪费纸张（readme 8.2）。
3. **绝不把系统状态当作用户确认。** 退出码 0 只说明「作业交给了 Windows
   打印系统」；队列为空也不说明纸张已经完整出来了（readme 3.1、3.3、10.3）。
   本模块返回的所有结论都写成「命令是否已发送」，而不是「打印成功」。
4. **每次只提交一个页面。** 批次是纸张管理单位，不是提交单位（readme 10.4）。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.core.config import AppConfig, PrinterSettings, load_config
from app.core.models import Task
from app.core.print_job import (
    SUBMIT_BLOCKED,
    SUBMIT_FAILED,
    SUBMIT_NOT_LAUNCHED,
    SUBMIT_SUBMITTED,
    SUBMIT_TIMEOUT,
    PrintJob,
)
from app.database.task_repository import TaskRepository
from app.printer import sumatra, windows_printers as wp
from app.printer.windows_printers import (
    PrintJobInfo,
    PrinterInfo,
    QueueSnapshot,
)
from app.printer.sumatra import SumatraResult
from app.services.page_jobs import PageJobError, PageJobService

logger = logging.getLogger("smartprint.services.print_service")

#: 记录到 task_events 的事件类型
EVENT_PRINT = "print"
EVENT_PREFLIGHT = "print_blocked"

#: 就绪等级
LEVEL_READY = "ready"      # 可以发送
LEVEL_WARN = "warn"        # 可以发送，但有风险提示
LEVEL_BLOCKED = "blocked"  # 不能发送


class PrintServiceError(Exception):
    """预检或提交过程中的致命错误。"""


@dataclass
class PrintOutcome:
    """一次「打印某一页」的完整结果。"""

    task: Task
    page_number: int
    file_path: Path | None = None
    printer: PrinterInfo | None = None
    job: PrintJob | None = None
    sumatra: SumatraResult | None = None
    queue_before: QueueSnapshot | None = None
    queue_after: QueueSnapshot | None = None
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        """命令是否已成功交给 Windows 打印系统。

        注意：这**不是**「纸张已经印好了」。纸张是否完整必须由用户确认。
        """
        return bool(self.job is not None and self.job.delivered)

    @property
    def status(self) -> str:
        return self.job.status if self.job else SUBMIT_BLOCKED

    @property
    def status_text(self) -> str:
        return self.job.status_text if self.job else "未发送"

    @property
    def new_queue_job(self) -> PrintJobInfo | None:
        """提交后队列里新出现的那个作业（如果能识别出来）。"""
        if self.queue_after is None or self.job is None or self.job.job_id is None:
            return None
        return self.queue_after.find(self.job.job_id)

    def summary_text(self) -> str:
        title = f"{self.task.document_name} 第 {self.page_number} 页"
        if self.dry_run:
            return f"[演练] {title}：未发送（只生成命令并预检）"
        if self.error:
            return f"{title}：{self.error}"
        parts = [f"{title} → {self.printer.name if self.printer else '（未指定打印机）'}"]
        parts.append(self.status_text)
        if self.sumatra is not None and self.sumatra.returncode is not None:
            parts.append(f"退出码 {self.sumatra.returncode}")
        return "，".join(parts)


@dataclass
class PrintReadiness:
    """发送前的环境检查结果。"""

    level: str = LEVEL_BLOCKED
    target: PrinterInfo | None = None
    printers: list[PrinterInfo] = field(default_factory=list)
    sumatra_path: Path | None = None
    sumatra_version: str = ""
    queue: QueueSnapshot | None = None
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    requested_name: str = ""

    @property
    def ok(self) -> bool:
        return self.level != LEVEL_BLOCKED

    @property
    def real_printers(self) -> list[PrinterInfo]:
        return [item for item in self.printers if not item.is_virtual]

    @property
    def virtual_printers(self) -> list[PrinterInfo]:
        return [item for item in self.printers if item.is_virtual]

    @property
    def level_text(self) -> str:
        return {
            LEVEL_READY: "可以打印",
            LEVEL_WARN: "可以打印（有提示）",
            LEVEL_BLOCKED: "无法打印",
        }.get(self.level, self.level)

    def summary_text(self) -> str:
        if self.target is None:
            target = f"未找到「{self.requested_name}」"
        else:
            target = f"{self.target.name}（{self.target.status_text}）"
        parts = [f"{self.level_text}：{target}"]
        if self.sumatra_path is None:
            parts.append("未找到 SumatraPDF")
        elif self.sumatra_version:
            parts.append(f"SumatraPDF {self.sumatra_version}")
        if self.queue is not None:
            parts.append(f"队列：{self.queue.summary_text()}")
        return "；".join(parts)


class PrintService:
    """把单页 PDF 发送到指定打印机，并记录全过程。"""

    def __init__(
        self,
        config: AppConfig | None = None,
        repository: TaskRepository | None = None,
        page_jobs: PageJobService | None = None,
        *,
        printer_reader: Callable[[], list[PrinterInfo]] | None = None,
        queue_reader: Callable[[str], QueueSnapshot] | None = None,
        sumatra_finder: Callable[[str | Path | None], Path | None] | None = None,
        sumatra_runner: Callable[..., SumatraResult] | None = None,
    ) -> None:
        self.config = config or load_config()
        self.settings: PrinterSettings = self.config.printer
        self.repository = repository
        self.page_jobs = page_jobs or PageJobService.from_config(self.config, repository)

        # 以下四个依赖可注入，验收脚本据此替换成「假的」实现，
        # 既不真的打印，又能完整走通成功/失败/超时/离线等分支。
        self._printer_reader = printer_reader or wp.list_printers
        self._queue_reader = queue_reader or wp.enum_jobs
        self._sumatra_finder = sumatra_finder or sumatra.find_sumatra
        self._sumatra_runner = sumatra_runner or sumatra.run

    @classmethod
    def from_config(
        cls,
        config: AppConfig | None = None,
        repository: TaskRepository | None = None,
        page_jobs: PageJobService | None = None,
    ) -> "PrintService":
        """按配置创建服务（界面默认走这个入口）。"""
        config = config or load_config()
        return cls(
            config=config,
            repository=repository,
            page_jobs=page_jobs or PageJobService.from_config(config, repository),
        )

    # -- 环境 ---------------------------------------------------------------

    @property
    def printer_name(self) -> str:
        return self.config.printer_name

    def list_printers(self) -> list[PrinterInfo]:
        try:
            return list(self._printer_reader())
        except wp.PrinterError as exc:
            logger.warning("枚举打印机失败：%s", exc)
            return []

    def find_sumatra(self) -> Path | None:
        return self._sumatra_finder(self.settings.sumatra_path or None)

    def queue(self, printer_name: str | None = None) -> QueueSnapshot:
        """读取队列快照（默认读配置里的目标打印机）。"""
        name = printer_name or self.printer_name
        return self._queue_reader(name)

    def check_readiness(
        self, *, read_queue: bool = True, printer_name: str | None = None
    ) -> PrintReadiness:
        """发送前的完整检查：打印机、虚拟打印机排除、SumatraPDF、队列。

        Args:
            read_queue: 是否读取队列快照。
            printer_name: 覆盖配置里的目标打印机（界面里临时选择其他打印机时使用）。
        """
        requested = printer_name or self.printer_name
        readiness = PrintReadiness(requested_name=requested)

        if not wp.is_supported():
            readiness.problems.append("当前系统不支持打印机枚举（仅 Windows 可用）")
            return readiness

        printers = self.list_printers()
        readiness.printers = printers
        if not printers:
            readiness.problems.append("没有读取到任何打印机（打印服务可能未启动）")

        target = wp.resolve_printer_name(requested, printers)
        readiness.target = target
        if target is None:
            real = [item.name for item in readiness.real_printers]
            readiness.problems.append(
                f"系统中没有找到名为「{requested}」的真实打印机"
                + (f"；可用的真实打印机：{'、'.join(real)}" if real else "；也没有其他真实打印机")
            )
        elif target.is_virtual:
            readiness.problems.append(
                f"「{target.name}」是虚拟打印机，不能用来真实打印（会把页面变回 PDF）"
            )
            readiness.target = None
        else:
            if target.is_offline:
                readiness.problems.append(
                    f"打印机「{target.name}」当前处于脱机状态，作业会堆在队列里打不出来"
                )
            elif target.blocking_status_flags:
                readiness.problems.append(
                    f"打印机「{target.name}」当前状态为「{target.status_text}」，现在发送大概率打不出来"
                )
            elif target.status_text != "就绪":
                readiness.warnings.append(
                    f"打印机「{target.name}」状态：{target.status_text}（不影响发送，但请留意）"
                )

        sumatra_path = self.find_sumatra()
        readiness.sumatra_path = sumatra_path
        if sumatra_path is None:
            readiness.problems.append(
                "没有找到 SumatraPDF.exe；请在 config.json 的 printer.sumatra_path "
                "填写完整路径，或把它放到 drivers/SumatraPDF/ 下"
            )
        else:
            readiness.sumatra_version = sumatra.read_version(sumatra_path)

        if read_queue and target is not None:
            snapshot = self._queue_reader(target.name)
            readiness.queue = snapshot
            if not snapshot.ok:
                readiness.warnings.append(f"读取打印队列失败：{snapshot.error}")
            elif snapshot.failed_jobs:
                readiness.warnings.append(
                    f"队列中有 {len(snapshot.failed_jobs)} 个异常作业"
                    f"（如 #{snapshot.failed_jobs[0].job_id}），可能先要清理掉"
                )
            elif snapshot.jobs:
                readiness.warnings.append(
                    f"队列中已有 {snapshot.job_count} 个作业；新作业会排在它们后面"
                )

        if readiness.problems:
            readiness.level = LEVEL_BLOCKED
        elif readiness.warnings:
            readiness.level = LEVEL_WARN
        else:
            readiness.level = LEVEL_READY
        return readiness

    # -- 预检 ---------------------------------------------------------------

    def preflight(
        self,
        task: Task,
        page_number: int,
        *,
        allow_blocked_printer: bool = False,
        read_queue: bool = True,
        printer_name: str | None = None,
    ) -> PrintReadiness:
        """针对「某个任务的某一页」做预检：文件、页码、打印机、程序。"""
        readiness = self.check_readiness(read_queue=read_queue, printer_name=printer_name)

        problems = list(readiness.problems)
        warnings = list(readiness.warnings)

        # 页码范围
        try:
            if not 1 <= int(page_number) <= int(task.page_count):
                problems.append(
                    f"页码 {page_number} 超出范围（该任务共 {task.page_count} 页）"
                )
        except (TypeError, ValueError):
            problems.append(f"页码不合法：{page_number!r}")

        if not task.source_exists:
            problems.append(f"源 PDF 不存在：{task.source_path}")

        if allow_blocked_printer:
            # 用户明确选择「仍然发送」：把「打印机状态」类问题降级为警告
            remaining: list[str] = []
            for item in problems:
                if "脱机" in item or "当前状态" in item:
                    warnings.append(f"{item}（已选择仍然发送）")
                else:
                    remaining.append(item)
            problems = remaining

        readiness.problems = problems
        readiness.warnings = warnings
        readiness.level = (
            LEVEL_BLOCKED if problems else (LEVEL_WARN if warnings else LEVEL_READY)
        )
        return readiness

    def page_file(self, task: Task, page_number: int) -> Path:
        """取（必要时生成）某一页的单页 PDF。"""
        try:
            return self.page_jobs.ensure_page_file(task, page_number)
        except PageJobError as exc:
            raise PrintServiceError(str(exc)) from exc

    # -- 打印 ---------------------------------------------------------------

    def build_command(
        self,
        pdf_path: Path,
        printer_name: str,
        page_numbers: str | int | None = None,
    ) -> list[str]:
        exe = self.find_sumatra()
        if exe is None:
            raise PrintServiceError("没有找到 SumatraPDF.exe")
        return sumatra.build_command(
            exe,
            pdf_path,
            printer_name,
            page_numbers,
            extra_settings=self.settings.print_settings,
        )

    def print_page(
        self,
        task: Task,
        page_number: int,
        *,
        dry_run: bool = False,
        allow_blocked_printer: bool = False,
        printer_name: str | None = None,
        read_queue: bool = True,
    ) -> PrintOutcome:
        """把某一页发送到目标打印机。

        Args:
            task: 目标任务。
            page_number: 要打印的页码（从 1 开始）。
            dry_run: 只做预检与命令拼装，不真的启动 SumatraPDF。
            allow_blocked_printer: 打印机处于脱机/错误状态时是否仍然发送。
            printer_name: 覆盖配置里的打印机名称（仅用于验收脚本）。
            read_queue: 是否读取队列快照（验收脚本可关掉）。

        Returns:
            PrintOutcome：命令是否已发送、退出码、队列快照与提示。
        """
        outcome = PrintOutcome(task=task, page_number=int(page_number), dry_run=dry_run)
        readiness = self.preflight(
            task,
            page_number,
            allow_blocked_printer=allow_blocked_printer,
            read_queue=read_queue,
            printer_name=printer_name,
        )
        outcome.printer = readiness.target
        outcome.queue_before = readiness.queue
        outcome.warnings = list(readiness.warnings)

        target_name = printer_name or (readiness.target.name if readiness.target else "")

        if readiness.problems:
            outcome.error = "；".join(readiness.problems)
            outcome.job = PrintJob(
                task_uid=task.task_uid,
                page_number=int(page_number),
                file_path=Path(""),
                printer_name=target_name or self.printer_name,
                status=SUBMIT_BLOCKED,
                message=outcome.error,
            )
            self._record_event(task, "print_blocked", f"第 {page_number} 页未发送：{outcome.error}")
            logger.warning(
                "任务 %s 第 %s 页预检未通过：%s", task.task_uid, page_number, outcome.error
            )
            return outcome

        # 取单页文件（不存在则按需生成，复用阶段四的 ensure_page_file）
        try:
            pdf_path = self.page_file(task, int(page_number))
        except PrintServiceError as exc:
            outcome.error = str(exc)
            outcome.job = PrintJob(
                task_uid=task.task_uid,
                page_number=int(page_number),
                file_path=Path(""),
                printer_name=target_name,
                status=SUBMIT_BLOCKED,
                message=outcome.error,
            )
            self._record_event(task, "print_blocked", f"第 {page_number} 页未发送：{exc}")
            return outcome

        outcome.file_path = pdf_path
        command = self.build_command(pdf_path, target_name)
        command_text = sumatra.format_command(command)

        job = PrintJob(
            task_uid=task.task_uid,
            page_number=int(page_number),
            file_path=pdf_path,
            printer_name=target_name,
            status=SUBMIT_BLOCKED,   # 真的执行成功后才会改成 submitted
            command=command_text,
        )
        outcome.job = job

        if dry_run:
            job.message = "演练模式：未启动 SumatraPDF"
            self._record_event(
                task,
                "print_dry_run",
                f"第 {page_number} 页演练：{' '.join(command)}",
            )
            logger.info("演练模式，未发送：%s", command_text)
            return outcome

        # 记录「命令即将发出」——先落库再执行，程序意外退出也能查到最后一次尝试
        if self.repository is not None:
            job.queue_before = _snapshot_json(outcome.queue_before)
            self.repository.add_print_job(job)

        result = self._sumatra_runner(
            command, timeout=self.settings.timeout_seconds
        )
        outcome.sumatra = result

        job.exit_code = result.returncode
        job.duration_ms = int(result.duration_seconds * 1000)
        if result.timed_out:
            job.status = SUBMIT_TIMEOUT
            job.message = result.summary_text
        elif not result.launched:
            job.status = SUBMIT_NOT_LAUNCHED
            job.message = result.error or "SumatraPDF 未启动"
        elif result.returncode == 0:
            job.status = SUBMIT_SUBMITTED
            job.message = "命令已交给 Windows 打印系统；纸张是否完整仍需人工确认"
        else:
            job.status = SUBMIT_FAILED
            job.message = result.summary_text

        # 提交后观察队列（只作参考，不作为成功依据）
        if read_queue and target_name:
            if self.settings.queue_recheck_seconds > 0:
                time.sleep(self.settings.queue_recheck_seconds)
            outcome.queue_after = self._queue_reader(target_name)
            job.queue_after = _snapshot_json(outcome.queue_after)
            job.job_id = _new_job_id(outcome.queue_before, outcome.queue_after)

        self._finish_job(job, outcome, task)
        return outcome

    # -- 内部 ---------------------------------------------------------------

    def _finish_job(self, job: PrintJob, outcome: PrintOutcome, task: Task) -> None:
        """补齐提示信息、写数据库、写任务事件。"""
        warnings = outcome.warnings
        if job.status == SUBMIT_SUBMITTED:
            pending = outcome.new_queue_job
            if pending is not None and pending.is_failed:
                warnings.append(
                    f"作业 #{pending.job_id} 在队列中报错（{pending.status_text}），"
                    "请检查打印机"
                )
            elif pending is not None:
                warnings.append(
                    f"作业 #{pending.job_id} 仍在队列中（{pending.status_text}），"
                    "这通常说明打印机没有正常出纸"
                )
            elif outcome.queue_after is not None and outcome.queue_after.ok:
                warnings.append(
                    "队列已看不到这个作业，但队列空闲不等于纸张已完整出纸，请检查实际纸张"
                )
            warnings.append("本程序不会替你判定打印成功：请确认纸张无误后再继续")

        if self.repository is not None:
            if job.id is not None:
                self.repository.update_print_job(
                    job.id,
                    status=job.status,
                    exit_code=job.exit_code,
                    duration_ms=job.duration_ms,
                    queue_after=job.queue_after,
                    spool_job_id=job.job_id,
                    message=job.message,
                )
            else:
                self.repository.add_print_job(job)

        event_type = EVENT_PRINT if job.status != SUBMIT_BLOCKED else EVENT_PREFLIGHT
        detail = f"第 {job.page_number} 页 → {job.printer_name}：{job.status_text}"
        if job.exit_code is not None:
            detail += f"（退出码 {job.exit_code}）"
        if job.job_id:
            detail += f"，队列作业 #{job.job_id}"
        if job.message:
            detail += f"；{job.message}"
        self._record_event(task, event_type, detail)

        level = logger.info if job.status == SUBMIT_SUBMITTED else logger.warning
        level(
            "任务 %s 第 %s 页：%s（命令：%s）",
            task.task_uid,
            job.page_number,
            job.status_text,
            job.command,
        )

    def _record_event(self, task: Task, event_type: str, message: str) -> None:
        if self.repository is None:
            return
        try:
            self.repository.add_event(task.task_uid, event_type, message)
        except Exception:  # noqa: BLE001 - 事件写失败不应影响打印结果返回
            logger.exception("记录任务事件失败：%s", task.task_uid)


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _snapshot_json(snapshot: QueueSnapshot | None) -> str:
    """把队列快照序列化成 JSON（原样留存当时的情况，便于事后回放）。"""
    if snapshot is None:
        return ""
    data = {
        "printer": snapshot.printer_name,
        "observed_at": snapshot.observed_at,
        "error": snapshot.error,
        "jobs": [
            {
                "job_id": job.job_id,
                "document": job.document,
                "user": job.user,
                "status": job.status,
                "status_text": job.status_text,
                "total_pages": job.total_pages,
                "pages_printed": job.pages_printed,
                "position": job.position,
                "submitted": job.submitted,
            }
            for job in snapshot.jobs
        ],
    }
    return json.dumps(data, ensure_ascii=False)


def _new_job_id(before: QueueSnapshot | None, after: QueueSnapshot | None) -> int | None:
    """找出提交后新出现的作业号（无法判断时返回 None）。"""
    if after is None or not after.ok:
        return None
    before_ids = {job.job_id for job in (before.jobs if before and before.ok else ())}
    added = sorted({job.job_id for job in after.jobs} - before_ids)
    return added[0] if added else None


def format_readiness(readiness: PrintReadiness) -> str:
    """把预检结果整理成多行文本（界面与验收脚本共用）。"""
    lines = [f"结论：{readiness.level_text}"]
    if readiness.target is not None:
        lines.append(
            f"目标打印机：{readiness.target.name}"
            f"（{readiness.target.kind_text}，端口 {readiness.target.port_name or '未知'}，"
            f"状态 {readiness.target.status_text}）"
        )
    else:
        lines.append(f"目标打印机：未找到「{readiness.requested_name}」")
    lines.append(
        f"SumatraPDF：{readiness.sumatra_path or '未找到'}"
        + (f"（{readiness.sumatra_version}）" if readiness.sumatra_version else "")
    )
    if readiness.queue is not None:
        lines.append(f"当前队列：{readiness.queue.summary_text()}")
    for problem in readiness.problems:
        lines.append(f"× {problem}")
    for warning in readiness.warnings:
        lines.append(f"! {warning}")
    return "\n".join(lines)


def describe_outcome(outcome: PrintOutcome) -> str:
    """把一次打印结果整理成多行文本。"""
    lines = [outcome.summary_text()]
    if outcome.file_path:
        lines.append(f"单页文件：{outcome.file_path}")
    if outcome.sumatra is not None:
        lines.append(f"命令行：{outcome.sumatra.command_text}")
        if outcome.sumatra.stdout.strip():
            lines.append(f"标准输出：{outcome.sumatra.stdout.strip()[:300]}")
        if outcome.sumatra.stderr.strip():
            lines.append(f"错误输出：{outcome.sumatra.stderr.strip()[:300]}")
    elif outcome.job is not None and outcome.job.command:
        # 演练或预检未通过时没有进程结果，但仍然把命令行显示出来
        lines.append(f"命令行（未执行）：{outcome.job.command}")
    if outcome.queue_before is not None:
        lines.append(f"提交前队列：{outcome.queue_before.summary_text()}")
    if outcome.queue_after is not None:
        lines.append(f"提交后队列：{outcome.queue_after.summary_text()}")
    for warning in outcome.warnings:
        lines.append(f"! {warning}")
    return "\n".join(lines)
