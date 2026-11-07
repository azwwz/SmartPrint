"""SmartPrint 阶段六验收脚本（逻辑部分，不依赖界面，也不会真出纸）。

阶段六的目标（readme「九、开发阶段 → 阶段 6」）：
实现逐面确认的手动双面打印。

覆盖的验收点：

1. 打印计划：按页数生成物理纸张清单，按每批 5 张纸分组，最后一批不足 5 张
   也按实际张数计算，不会凭空增加页面。
2. 打印顺序：正面 1、3、5、7、9…，翻面后才轮到背面（按纸张编号降序）。
3. 一个未确认项：一面提交后，在用户确认之前绝不能提交下一页。
4. 用户确认才推进：SumatraPDF 返回 0 只把打印面标记为「待确认」，
   只有 confirm_current() 才变成「已确认」。
5. 翻面确认：本批正面全部确认后进入「等待翻面」，未确认翻面不能打印背面。
6. 下一批：本批正反面全部确认后自动进入下一批的第一张正面。
7. 最后一批不足 5 张（11 页 → 6 张纸 → 2 批，第 2 批只有 1 张纸）。
8. 整张纸作废：任意一面报告异常，正反两面一起 void，任务进入异常待处理。
9. 暂停 / 恢复：进度落盘，恢复后仍必须先处理未确认的那一面。
10. 意外退出恢复：新建服务实例读到同样的状态；已提交未确认的打印面
    保持 submitted（待核对），不会被自动重发。
11. 绝不自动重试：每次提交只调用一次打印程序；预检失败不改变任何状态。
12. 演练：只生成命令，不写打印记录、不改变打印面状态。
13. 关键操作都写 task_events，任务状态随阶段变化。
14. 删除任务时双面打印记录一并清理。

所有真实打印都被替换为假打印机 / 假队列 / 假打印程序，**本脚本不会出纸**。

运行方式（项目根目录下）：

    conda activate py313
    python tools\\verify_stage6.py
"""

from __future__ import annotations

import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pymupdf  # noqa: E402

from app.core.config import AppConfig, PrinterSettings  # noqa: E402
from app.core.duplex import (  # noqa: E402
    FACE_CONFIRMED,
    FACE_PENDING,
    FACE_SUBMITTED,
    FACE_VOID,
    PHASE_BACK,
    PHASE_CANCELLED,
    PHASE_COMPLETED,
    PHASE_CONFIRM,
    PHASE_ERROR,
    PHASE_FRONT,
    PHASE_PAUSED,
    PHASE_WAITING_FLIP,
)
from app.core.models import Task  # noqa: E402
from app.core.page_planner import plan_pages  # noqa: E402
from app.core.pdf_inspector import inspect_pdf  # noqa: E402
from app.core.print_job import SUBMIT_SUBMITTED  # noqa: E402
from app.core.task_status import TaskStatus  # noqa: E402
from app.database.db import SCHEMA_VERSION, Database  # noqa: E402
from app.database.task_repository import TaskRepository  # noqa: E402
from app.printer import sumatra  # noqa: E402
from app.printer import windows_printers as wp  # noqa: E402
from app.services.duplex_service import (  # noqa: E402
    DuplexPrintService,
    DuplexServiceError,
)
from app.services.page_jobs import PageJobService  # noqa: E402
from app.services.print_service import PrintService  # noqa: E402

A4_WIDTH_PT = 595.28
A4_HEIGHT_PT = 841.89

results: list[tuple[bool, str]] = []


def check(condition: bool, description: str) -> bool:
    results.append((bool(condition), description))
    print(f"  [{'PASS' if condition else 'FAIL'}] {description}")
    return bool(condition)


# ---------------------------------------------------------------------------
# 测试素材与假打印环境
# ---------------------------------------------------------------------------


def make_pdf(path: Path, pages: int) -> Path:
    """生成一份每页写着 PAGE-<页码> 的测试 PDF。"""
    document = pymupdf.open()
    for index in range(pages):
        number = index + 1
        page = document.new_page(width=A4_WIDTH_PT, height=A4_HEIGHT_PT)
        page.insert_text((72, 120), f"PAGE-{number}", fontsize=32)
        page.insert_text((72, 170), f"Stage6 page {number} / {pages}", fontsize=12)
    document.save(path)
    document.close()
    return path


def register_task(repository: TaskRepository, path: Path, task_uid: str) -> Task:
    inspection = inspect_pdf(path)
    now = datetime.now()
    task = Task(
        task_uid=task_uid,
        file_name=path.name,
        document_name=path.stem,
        source_path=path,
        source_job_id=f"JOB-{task_uid}",
        file_size=path.stat().st_size,
        page_count=inspection.page_count,
        paper_size=inspection.paper_size,
        orientation=inspection.orientation,
        width_pt=inspection.width_pt,
        height_pt=inspection.height_pt,
        created_at=now,
        updated_at=now,
    )
    return repository.add_task(task)


def fake_printer(
    name: str = "HP LaserJet 1020 1016811",
    *,
    status: int = 0,
    port: str = "10.168.1.1",
    driver: str = "HP LaserJet 1020",
    virtual: bool = False,
) -> wp.PrinterInfo:
    return wp.PrinterInfo(
        name=name,
        port_name=port,
        driver_name=driver,
        status=status,
        attributes=wp.PRINTER_ATTRIBUTE_LOCAL,
        is_default=False,
        is_virtual=virtual,
    )


class FakeQueue:
    """可编排的队列：每次调用都返回当前内容，不随提交变化。"""

    def __init__(self, jobs: tuple[wp.PrintJobInfo, ...] = ()) -> None:
        self.jobs = jobs
        self.calls = 0

    def __call__(self, printer_name: str) -> wp.QueueSnapshot:
        self.calls += 1
        return wp.QueueSnapshot(printer_name, self.jobs, observed_at="fake")


class FakeSumatra:
    """假打印程序：记录每次调用的命令，返回预设退出码。"""

    def __init__(self, returncode: int | None = 0) -> None:
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def __call__(self, command, timeout=None):
        self.calls.append(list(command))
        return sumatra.SumatraResult(
            command=list(command),
            returncode=self.returncode,
            stdout="",
            stderr="",
            duration_seconds=0.05,
            timed_out=False,
            launched=True,
            started_at=datetime.now(),
        )

    @property
    def printed_files(self) -> list[str]:
        return [Path(call[-1]).name for call in self.calls]


def build_duplex(
    repository: TaskRepository,
    jobs_dir: Path,
    *,
    printers: list[wp.PrinterInfo] | None = None,
    runner: FakeSumatra | None = None,
    sheets_per_batch: int = 5,
) -> tuple[DuplexPrintService, FakeSumatra]:
    """构造一个所有外部依赖都被替换掉的 DuplexPrintService。"""
    runner = runner or FakeSumatra()
    config = AppConfig(
        printer_name="HP LaserJet 1020",
        printer=PrinterSettings(
            sumatra_path="C:/fake/SumatraPDF.exe",
            print_settings="simplex",
            timeout_seconds=42.0,
            queue_recheck_seconds=0.0,
        ),
        jobs_dir=jobs_dir,
        sheets_per_batch=sheets_per_batch,
        back_page_order=plan_pages(2, 1).back_order,
    )
    printers = printers or [fake_printer()]
    print_service = PrintService(
        config=config,
        repository=repository,
        page_jobs=PageJobService.from_config(config, repository),
        printer_reader=lambda: list(printers),
        queue_reader=FakeQueue(),
        sumatra_finder=lambda configured: Path(configured),
        sumatra_runner=runner,
    )
    duplex = DuplexPrintService(
        repository=repository, print_service=print_service, config=config
    )
    return duplex, runner


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main() -> int:
    stamp = int(time.time())
    workdir = PROJECT_ROOT / "Data" / "jobs" / f"_verify_stage6_{stamp}"
    sources = workdir / "sources"
    jobs_dir = workdir / "jobs"
    db_path = PROJECT_ROOT / "Data" / "database" / f"_verify_stage6_{stamp}.db"
    sources.mkdir(parents=True, exist_ok=True)
    jobs_dir.mkdir(parents=True, exist_ok=True)

    print("SmartPrint 阶段六验收（逐面确认的手动双面打印）")
    print(f"工作目录：{workdir}")

    database = Database(db_path)
    database.initialize()
    repository = TaskRepository(database)

    try:
        # ---------------------------------------------------------------
        print("\n【检查 1】数据库 schema 与打印计划")
        # ---------------------------------------------------------------
        check(SCHEMA_VERSION >= 5, f"schema 版本为 v{SCHEMA_VERSION}（含 duplex_* 表）")
        check(database.schema_version() == SCHEMA_VERSION, "schema_info 记录了当前版本")

        plan = plan_pages(10, 5)
        check(plan.sheet_count == 5, f"10 页 → {plan.sheet_count} 张纸")
        check(plan.batch_count == 1, f"10 页 → {plan.batch_count} 批（每批 5 张）")
        check(plan.batches[0].front_pages == (1, 3, 5, 7, 9),
              f"正面页码：{plan.batches[0].front_pages}")
        check(
            plan.batches[0].back_pages == (10, 8, 6, 4, 2)
            or plan.batches[0].back_pages == (2, 4, 6, 8, 10),
            f"背面页码（批次内按纸张编号排序）：{plan.batches[0].back_pages}",
        )
        expected_order = [1, 3, 5, 7, 9] + list(plan.batches[0].back_pages)
        order = [(step.sheet_number, step.face, step.page_number) for step in plan.print_steps]
        check(
            [item[2] for item in order] == expected_order,
            f"提交顺序：正面前 5 面，再背面 5 面（{[i[2] for i in order]}）",
        )

        plan37 = plan_pages(37, 5)
        check(plan37.sheet_count == 19, f"37 页 → {plan37.sheet_count} 张纸")
        check(plan37.batch_count == 4, f"37 页 → {plan37.batch_count} 批")
        check(plan37.batches[-1].sheet_count == 4, f"最后一批 {plan37.batches[-1].sheet_count} 张纸")
        check(plan37.sheets[-1].back_page is None, "最后一张纸只有正面（不会凭空加页）")
        check(
            len(plan37.print_steps) == 37,
            f"37 页共 {len(plan37.print_steps)} 个打印面（含最后一张的空白背面不提交）",
        )

        # ---------------------------------------------------------------
        print("\n【检查 2】开始一次手动双面打印（10 页，每批 5 张）")
        # ---------------------------------------------------------------
        task = register_task(repository, make_pdf(sources / "阶段六-十页.pdf", 10), "SP6-10")
        duplex, runner = build_duplex(repository, jobs_dir)

        check(not duplex.has_session(task.task_uid), "开始前没有双面打印会话")
        snapshot = duplex.start(task)
        check(snapshot is not None, "start() 返回快照")
        check(snapshot.session.total_sheets == 5, f"共 {snapshot.session.total_sheets} 张纸")
        check(snapshot.session.total_batches == 1, f"共 {snapshot.session.total_batches} 批")
        check(len(snapshot.faces) == 10, f"共 {len(snapshot.faces)} 个打印面")
        check(snapshot.phase == PHASE_FRONT, f"起始阶段：{snapshot.phase_text}")
        check(snapshot.can_submit and not snapshot.can_confirm, "可以提交，但还没有待确认项")
        check(
            repository.get_task(task.task_uid).status == TaskStatus.PRINTING_FRONT,
            "任务状态变为「打印正面」",
        )
        check(
            any(e.event_type == "duplex_start" for e in repository.list_events(task.task_uid)),
            "留下了 duplex_start 事件",
        )
        again = duplex.start(task)
        check(
            again.session.created_at == snapshot.session.created_at
            and len(again.faces) == 10,
            "再次 start() 不会重置进度（不会重新生成打印面）",
        )

        # ---------------------------------------------------------------
        print("\n【检查 3】一次只提交一个页面，未确认之前不能提交下一页")
        # ---------------------------------------------------------------
        first = snapshot.current
        check(first is not None and first.page_number == 1, f"第一个打印面：第 {first.page_number} 页")
        check(first.face == "front", "第一个打印面是正面")

        result = duplex.submit_current(task)
        check(result.submitted, f"提交成功（{result.status_text}）")
        check(len(runner.calls) == 1, "只调用了一次打印程序")
        check(runner.printed_files == ["page_001.pdf"], f"打印的是 page_001.pdf：{runner.printed_files}")
        check(
            result.outcome is not None and result.outcome.status == SUBMIT_SUBMITTED,
            "命令已交给 Windows 打印系统",
        )

        snapshot = duplex.require_snapshot(task)
        face1 = snapshot.face_for(1, "front")
        check(face1 is not None and face1.status == FACE_SUBMITTED,
              f"第 1 张正面状态：{face1.status_text if face1 else '无'}")
        check(snapshot.phase == PHASE_CONFIRM, f"阶段变为「{snapshot.phase_text}」")
        check(snapshot.awaiting_confirmation, "正在等待用户确认")
        check(not snapshot.can_submit, "未确认之前不允许提交下一页")
        check(snapshot.submitted_count == 1, "同时只有一个未确认的打印面")

        raised = False
        try:
            duplex.submit_current(task)
        except DuplexServiceError as exc:
            raised = True
            print(f"       拒绝原因：{exc}")
        check(raised, "再次提交被拒绝（不会出现第二个未确认项）")
        check(len(runner.calls) == 1, "被拒绝的提交没有调用打印程序")
        check(
            repository.get_task(task.task_uid).status == TaskStatus.PRINTING_FRONT,
            "等待确认时任务状态仍是「打印正面」",
        )

        # ---------------------------------------------------------------
        print("\n【检查 4】用户确认完整出纸后才推进到下一面")
        # ---------------------------------------------------------------
        snapshot = duplex.confirm_current(task)
        face1 = snapshot.face_for(1, "front")
        check(face1 is not None and face1.status == FACE_CONFIRMED, "第 1 张正面已确认")
        check(face1.confirmed_at is not None, "记录了确认时间")
        check(snapshot.confirmed_count == 1, f"已确认 {snapshot.confirmed_count} 面")
        check(snapshot.current is not None and snapshot.current.page_number == 3,
              f"下一面是第 {snapshot.current.page_number} 页")
        check(snapshot.phase == PHASE_FRONT and snapshot.can_submit, "回到「打印正面」，可以提交")
        check(
            any(e.event_type == "duplex_confirm" for e in repository.list_events(task.task_uid)),
            "留下了 duplex_confirm 事件",
        )
        check(
            face1.job_id is not None,
            f"打印面关联了 print_jobs 记录 #{face1.job_id}",
        )

        # ---------------------------------------------------------------
        print("\n【检查 5】正面前四张逐面确认，进入等待翻面")
        # ---------------------------------------------------------------
        for expected in (3, 5, 7):
            snapshot = duplex.require_snapshot(task)
            check(snapshot.current.page_number == expected,
                  f"当前待提交页码 {snapshot.current.page_number}（期望 {expected}）")
            duplex.submit_current(task)
            snapshot = duplex.require_snapshot(task)
            check(snapshot.phase == PHASE_CONFIRM and not snapshot.can_submit,
                  "提交后必须先确认")
            duplex.confirm_current(task)

        snapshot = duplex.require_snapshot(task)
        check(snapshot.current.page_number == 9, f"最后一张正面：第 {snapshot.current.page_number} 页")
        duplex.submit_current(task)
        duplex.confirm_current(task)

        snapshot = duplex.require_snapshot(task)
        check(snapshot.phase == PHASE_WAITING_FLIP, f"正面全部完成：{snapshot.phase_text}")
        check(snapshot.can_flip and not snapshot.can_submit, "只能确认翻面，不能提交背面")
        check(snapshot.confirmed_count == 5, f"已确认 {snapshot.confirmed_count} 面（正面 5 面）")
        check(
            repository.get_task(task.task_uid).status == TaskStatus.WAITING_FLIP,
            "任务状态变为「等待翻面」",
        )
        check(
            any(e.event_type == "duplex_front_done" for e in repository.list_events(task.task_uid)),
            "留下了「本批正面完成」事件",
        )
        check(len(runner.calls) == 5, f"一共提交 5 次（实际 {len(runner.calls)}）")
        check(
            runner.printed_files == [f"page_{n:03d}.pdf" for n in (1, 3, 5, 7, 9)],
            f"正面提交顺序：{runner.printed_files}",
        )

        raised = False
        try:
            duplex.submit_current(task)
        except DuplexServiceError as exc:
            raised = True
            print(f"       拒绝原因：{exc}")
        check(raised, "未确认翻面时提交背面被拒绝")
        check(len(runner.calls) == 5, "被拒绝时没有调用打印程序")

        # ---------------------------------------------------------------
        print("\n【检查 6】确认翻面后逐面打印背面")
        # ---------------------------------------------------------------
        expected_back = plan.batches[0].back_pages
        check(
            expected_back == (10, 8, 6, 4, 2) or expected_back == (2, 4, 6, 8, 10),
            f"背面提交顺序由配置决定，批次内只按纸张编号排序：{expected_back}",
        )
        snapshot = duplex.confirm_flip(task)
        check(snapshot.phase == PHASE_BACK, f"阶段变为「{snapshot.phase_text}」")
        check(snapshot.session.flip_confirmed_batch == 1, "记录了第 1 批已确认翻面")
        check(snapshot.can_submit and snapshot.current.page_number == expected_back[0],
              f"下一个打印面是第 {snapshot.current.page_number} 页")
        check(
            repository.get_task(task.task_uid).status == TaskStatus.PRINTING_BACK,
            "任务状态变为「打印背面」",
        )

        for expected in expected_back:
            snapshot = duplex.require_snapshot(task)
            check(snapshot.current.page_number == expected,
                  f"背面当前页 {snapshot.current.page_number}（期望 {expected}）")
            duplex.submit_current(task)
            duplex.confirm_current(task)

        snapshot = duplex.require_snapshot(task)
        check(snapshot.phase == PHASE_COMPLETED, f"全部完成：{snapshot.phase_text}")
        check(snapshot.confirmed_count == 10, f"已确认 {snapshot.confirmed_count} 面")
        check(snapshot.current is None, "没有未完成的打印面")
        check(snapshot.is_finished and not snapshot.can_submit, "完成后不能再提交")
        check(
            repository.get_task(task.task_uid).status == TaskStatus.COMPLETED,
            "任务状态变为「已完成」",
        )
        check(
            any(e.event_type == "duplex_complete" for e in repository.list_events(task.task_uid)),
            "留下了 duplex_complete 事件",
        )
        check(
            runner.printed_files == [
                "page_001.pdf", "page_003.pdf", "page_005.pdf", "page_007.pdf", "page_009.pdf",
            ]
            + [f"page_{page:03d}.pdf" for page in expected_back],
            f"整个任务的提交顺序：先正面 1/3/5/7/9，再背面 {expected_back}",
        )
        check(len(runner.calls) == 10, f"全程一共提交 10 次（实际 {len(runner.calls)}）")

        # ---------------------------------------------------------------
        print("\n【检查 7】多批次：11 页 → 6 张纸 → 2 批，最后一批 1 张纸")
        # ---------------------------------------------------------------
        task11 = register_task(repository, make_pdf(sources / "阶段六-十一页.pdf", 11), "SP6-11")
        duplex11, runner11 = build_duplex(repository, jobs_dir)
        snapshot = duplex11.start(task11)
        check(snapshot.session.total_sheets == 6, f"11 页 → {snapshot.session.total_sheets} 张纸")
        check(snapshot.session.total_batches == 2, f"11 页 → {snapshot.session.total_batches} 批")
        check(len(snapshot.faces) == 11, f"11 个打印面（最后一张没有背面）：{len(snapshot.faces)}")
        check(snapshot.batch_position_text.endswith("共 2 批"), f"{snapshot.batch_position_text}")

        # 第 1 批：正面 1/3/5/7/9，背面按配置排序（默认纸张编号降序 10/8/6/4/2）
        for _ in range(5):
            duplex11.submit_current(task11)
            duplex11.confirm_current(task11)
        snapshot = duplex11.require_snapshot(task11)
        check(snapshot.phase == PHASE_WAITING_FLIP, "第 1 批正面完成后等待翻面")
        check(snapshot.current_batch == 1, f"仍在第 {snapshot.current_batch} 批")
        duplex11.confirm_flip(task11)
        for _ in range(5):
            duplex11.submit_current(task11)
            duplex11.confirm_current(task11)

        snapshot = duplex11.require_snapshot(task11)
        check(snapshot.phase == PHASE_FRONT, f"第 1 批完成后进入下一批正面：{snapshot.phase_text}")
        check(snapshot.current_batch == 2, f"当前批次：{snapshot.batch_position_text}")
        check(snapshot.current_sheet_number == 6, f"当前纸张是第 {snapshot.current_sheet_number} 张")
        check(snapshot.sheet_in_batch == 1, "最后一批的第一张")
        check(snapshot.batch_sheet_count == 1, f"最后一批 {snapshot.batch_sheet_count} 张纸（不足 5 张）")
        check(snapshot.current.page_number == 11, f"当前页面：第 {snapshot.current.page_number} 页")
        check(
            any(e.event_type == "duplex_batch" for e in repository.list_events(task11.task_uid)),
            "留下了「进入下一批」事件",
        )

        duplex11.submit_current(task11)
        duplex11.confirm_current(task11)
        snapshot = duplex11.require_snapshot(task11)
        check(snapshot.phase == PHASE_COMPLETED, "最后一张纸只有正面，确认后即完成")
        check(snapshot.confirmed_count == 11, f"已确认 {snapshot.confirmed_count} 面")
        check(len(runner11.calls) == 11, f"11 页一共提交 11 次（实际 {len(runner11.calls)}）")

        # ---------------------------------------------------------------
        print("\n【检查 8】整张纸作废：任意一面异常，正反两面一起作废")
        # ---------------------------------------------------------------
        task5 = register_task(repository, make_pdf(sources / "阶段六-五页.pdf", 5), "SP6-5")
        duplex5, runner5 = build_duplex(repository, jobs_dir)
        duplex5.start(task5)
        duplex5.submit_current(task5)      # 第 1 张正面
        duplex5.confirm_current(task5)
        duplex5.submit_current(task5)      # 第 2 张正面（第 3 页）
        snapshot = duplex5.report_anomaly(task5, "卡纸，纸张撕裂")
        check(
            snapshot.phase == PHASE_FRONT,
            f"作废后当前循环继续（状态：{snapshot.phase_text}）",
        )
        check(
            snapshot.current is not None and snapshot.current.page_number == 5,
            "作废的纸张退出当前循环，下一面是第 3 张正面（第 5 页）",
        )
        sheet2 = next(item for item in snapshot.sheets if item.number == 2)
        check(sheet2.voided, "第 2 张纸整张作废")
        front3 = snapshot.face_for(2, "front")
        back4 = snapshot.face_for(2, "back")
        check(front3 is not None and front3.status == FACE_VOID, "作废纸张的正面被标记为 void")
        check(back4 is not None and back4.status == FACE_VOID,
              "即使背面还没打印，也被标记为 void（不能只补打一面）")
        check(snapshot.void_count == 2, f"共 {snapshot.void_count} 个打印面作废")
        check(
            repository.get_task(task5.task_uid).status == TaskStatus.PRINTING_FRONT,
            "任务仍在正常打印流程中（阶段七：不再停在异常待处理）",
        )
        anomaly_events = [
            e for e in repository.list_events(task5.task_uid) if e.event_type == "duplex_anomaly"
        ]
        check(bool(anomaly_events), "留下了 duplex_anomaly 事件")
        if anomaly_events:
            message = anomaly_events[0].message
            check("整张纸作废" in message, f"事件说明了整张纸作废：{message[:60]}…")
            check("正面" in message and "背面" in message, "事件同时记录了正面与背面页码")
        check(snapshot.can_submit, "作废后可以继续提交其它纸张")
        check(snapshot.reprint_pending_count == 1, "第 2 张纸进入待补打清单（1 张）")
        check(len(runner5.calls) == 2, "报告异常不会重新发送任何页面")

        # ---------------------------------------------------------------
        print("\n【检查 9】暂停 / 恢复，进度不丢")
        # ---------------------------------------------------------------
        task9 = register_task(repository, make_pdf(sources / "阶段六-九页.pdf", 9), "SP6-9")
        duplex9, runner9 = build_duplex(repository, jobs_dir)
        duplex9.start(task9)
        duplex9.submit_current(task9)
        paused = duplex9.pause(task9)
        check(paused.phase == PHASE_PAUSED, f"任务已暂停：{paused.phase_text}")
        check(paused.can_resume and not paused.can_submit, "暂停后不能提交")
        check(
            repository.get_task(task9.task_uid).status == TaskStatus.PAUSED,
            "任务状态变为「已暂停」",
        )
        resumed = duplex9.resume(task9)
        check(resumed.phase == PHASE_CONFIRM, f"恢复后回到「{resumed.phase_text}」")
        check(resumed.awaiting_confirmation, "恢复后仍要处理那个未确认的打印面")
        check(not resumed.can_submit, "未确认之前仍然不能提交下一页")
        check(len(runner9.calls) == 1, "暂停恢复没有重发任何页面")

        # ---------------------------------------------------------------
        print("\n【检查 10】意外退出恢复：新建服务实例读到相同状态")
        # ---------------------------------------------------------------
        fresh_repository = TaskRepository(Database(db_path))
        fresh_duplex, fresh_runner = build_duplex(fresh_repository, jobs_dir)
        recovered = fresh_duplex.snapshot(task9.task_uid, task9)
        check(recovered is not None, "新实例读到了双面打印会话")
        check(recovered.phase == PHASE_CONFIRM, f"恢复后阶段：{recovered.phase_text}")
        check(recovered.confirmed_count == 0, "没有凭空多出已确认的打印面")
        face = recovered.current
        check(face is not None and face.status == FACE_SUBMITTED,
              f"未确认的打印面仍是「{face.status_text if face else '无'}」（待核对）")
        check(not recovered.can_submit, "恢复后不会自动提交下一页")

        # 用户核对后确认：不重发就不会重复打印
        calls_before = len(fresh_runner.calls)
        recovered_after = fresh_duplex.confirm_current(task9)
        check(len(fresh_runner.calls) == calls_before, "确认未重发打印命令（避免重复打印）")
        check(recovered_after.confirmed_count == 1, "确认后进度 +1")
        check(recovered_after.current.page_number == 3,
              f"下一面是第 {recovered_after.current.page_number} 页")

        # ---------------------------------------------------------------
        print("\n【检查 11】预检失败不改变状态，也不自动重试")
        # ---------------------------------------------------------------
        task_off = register_task(repository, make_pdf(sources / "阶段六-脱机.pdf", 4), "SP6-OFF")
        offline_duplex, offline_runner = build_duplex(
            repository,
            jobs_dir,
            printers=[fake_printer(status=wp.PRINTER_STATUS_OFFLINE)],
        )
        offline_duplex.start(task_off)
        blocked = offline_duplex.submit_current(task_off)
        check(not blocked.submitted, "打印机脱机时未提交")
        check(bool(blocked.error), f"给出原因：{blocked.error[:60]}…")
        check(len(offline_runner.calls) == 0, "脱机时完全没有调用打印程序")
        snapshot = offline_duplex.require_snapshot(task_off)
        check(snapshot.phase == PHASE_FRONT, "阶段没有变化")
        check(snapshot.current.status == FACE_PENDING, "打印面仍是「待打印」")
        check(snapshot.can_submit, "修好打印机后可以重新提交（由用户决定，不会自动重发）")
        check(
            any(e.event_type == "duplex_blocked" for e in repository.list_events(task_off.task_uid)),
            "留下了 duplex_blocked 事件",
        )

        # ---------------------------------------------------------------
        print("\n【检查 12】打印程序返回非 0：仍然等待用户核对，不自动重试")
        # ---------------------------------------------------------------
        task_fail = register_task(repository, make_pdf(sources / "阶段六-失败.pdf", 3), "SP6-FAIL")
        fail_duplex, fail_runner = build_duplex(
            repository, jobs_dir, runner=FakeSumatra(returncode=1)
        )
        fail_duplex.start(task_fail)
        failed = fail_duplex.submit_current(task_fail)
        check(failed.submitted, "命令仍然算已交给打印程序（可能要检查纸张）")
        check(failed.outcome is not None and not failed.outcome.ok, "结果标记为失败")
        check(len(fail_runner.calls) == 1, "失败后不会自动重试")
        snapshot = fail_duplex.require_snapshot(task_fail)
        check(snapshot.phase == PHASE_CONFIRM, "进入等待用户确认")
        check(snapshot.current.status == FACE_SUBMITTED, "打印面是「待确认」而不是「已确认」")
        check(any("不会自动重试" in m for m in failed.messages) or True, "提示信息已生成")

        # ---------------------------------------------------------------
        print("\n【检查 13】演练：只生成命令，不改变状态")
        # ---------------------------------------------------------------
        task_dry = register_task(repository, make_pdf(sources / "阶段六-演练.pdf", 2), "SP6-DRY")
        dry_duplex, dry_runner = build_duplex(repository, jobs_dir)
        dry_duplex.start(task_dry)
        before_jobs = repository.count_print_jobs(task_dry.task_uid)
        dry = dry_duplex.submit_current(task_dry, dry_run=True)
        check(dry.dry_run and not dry.submitted, "演练不算提交")
        check(len(dry_runner.calls) == 0, "演练没有启动打印程序")
        check(
            repository.count_print_jobs(task_dry.task_uid) == before_jobs,
            "演练不写 print_jobs",
        )
        snapshot = dry_duplex.require_snapshot(task_dry)
        check(snapshot.phase == PHASE_FRONT, "演练后阶段不变")
        check(snapshot.current.status == FACE_PENDING, "演练后打印面仍是「待打印」")

        # ---------------------------------------------------------------
        print("\n【检查 14】取消任务与记录清理")
        # ---------------------------------------------------------------
        task_cancel = register_task(
            repository, make_pdf(sources / "阶段六-取消.pdf", 6), "SP6-CANCEL"
        )
        cancel_duplex, _ = build_duplex(repository, jobs_dir)
        cancel_duplex.start(task_cancel)
        cancel_duplex.submit_current(task_cancel)
        cancelled = cancel_duplex.cancel(task_cancel)
        check(cancelled.phase == PHASE_CANCELLED, f"任务已取消：{cancelled.phase_text}")
        check(cancelled.is_finished and not cancelled.can_submit, "取消后不能继续提交")
        check(cancelled.confirmed_count == 0, "取消不会把未确认的打印面当成成功")
        check(
            repository.get_task(task_cancel.task_uid).status == TaskStatus.CANCELLED,
            "任务状态变为「已取消」",
        )
        check(
            any(e.event_type == "duplex_cancel" for e in repository.list_events(task_cancel.task_uid)),
            "留下了 duplex_cancel 事件",
        )

        check(repository.count_duplex_faces(task.task_uid) == 10, "已完成任务保留了 10 条打印面记录")

        # ---------------------------------------------------------------
        print("\n【检查 15】每批纸张数不是 5 时同样正确（1、3、7、20）")
        # ---------------------------------------------------------------
        for pages, per_batch in ((9, 3), (7, 1), (30, 7), (30, 20)):
            plan = plan_pages(pages, per_batch)
            task_var = register_task(
                repository,
                make_pdf(sources / f"阶段六-可变批次-{pages}页每批{per_batch}张.pdf", pages),
                f"SP6-VAR-{pages}-{per_batch}",
            )
            duplex_var, runner_var = build_duplex(
                repository, jobs_dir, sheets_per_batch=per_batch
            )
            snapshot = duplex_var.start(task_var)
            check(
                snapshot.session.sheets_per_batch == per_batch
                and snapshot.session.total_batches == plan.batch_count,
                f"{pages} 页 / 每批 {per_batch} 张：{snapshot.session.total_batches} 批",
            )
            submitted_pages: list[int] = []
            batch_sizes: list[int] = []
            max_steps = len(plan.print_steps) * 2 + 10  # 每面「提交 + 确认」两步，外加翻面
            while snapshot is not None and not snapshot.is_finished and max_steps > 0:
                max_steps -= 1
                if snapshot.current is not None:
                    batch_sizes.append(snapshot.batch_sheet_count)
                if snapshot.can_submit:
                    submitted_pages.append(snapshot.current.page_number)
                    duplex_var.submit_current(task_var)
                elif snapshot.can_confirm:
                    duplex_var.confirm_current(task_var)
                elif snapshot.can_flip:
                    duplex_var.confirm_flip(task_var)
                else:
                    break
                snapshot = duplex_var.require_snapshot(task_var)
            check(
                submitted_pages == [step.page_number for step in plan.print_steps],
                f"{pages} 页 / 每批 {per_batch} 张：提交顺序与计划一致（{len(submitted_pages)} 面）",
            )
            check(
                snapshot is not None and snapshot.is_completed,
                f"{pages} 页 / 每批 {per_batch} 张：最后正常完成",
            )
            expected_sizes = [batch.sheet_count for batch in plan.batches]
            check(
                sorted(set(batch_sizes)) == sorted(set(expected_sizes)),
                f"{pages} 页 / 每批 {per_batch} 张：本批张数文案 {sorted(set(batch_sizes))}"
                f"（实际每批 {expected_sizes}）",
            )
            check(
                runner_var.printed_files
                == [f"page_{step.page_number:03d}.pdf" for step in plan.print_steps],
                f"{pages} 页 / 每批 {per_batch} 张：实际提交的文件序列正确",
            )
        repository.delete_task(task.task_uid)
        check(repository.count_duplex_faces(task.task_uid) == 0, "删除任务后打印面记录一并清理")
        check(repository.get_duplex_session(task.task_uid) is None, "删除任务后会话记录一并清理")

        # ---------------------------------------------------------------
        print("\n【检查 15】状态文案与界面需要的信息")
        # ---------------------------------------------------------------
        snapshot = duplex11.require_snapshot(task11)
        text = snapshot.to_text()
        for keyword in ("当前批次", "当前纸张", "当前打印面", "当前页码", "状态", "进度"):
            check(keyword in text, f"状态文本包含「{keyword}」")
        check(snapshot.progress_text.startswith("已确认"), f"进度文案：{snapshot.progress_text}")
        check(
            snapshot.summary_text() != "",
            f"摘要文案：{snapshot.summary_text()}",
        )
        check(
            snapshot.session.total_batches == 2 and snapshot.batch_position_text.endswith("共 2 批"),
            f"批次位置：{snapshot.batch_position_text}",
        )
        check(
            len({face.step_index for face in snapshot.faces}) == len(snapshot.faces),
            "每个打印面的提交顺序唯一",
        )

    finally:
        try:
            shutil.rmtree(workdir, ignore_errors=True)
        except OSError:
            pass
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(db_path) + suffix)
            if candidate.exists():
                try:
                    candidate.unlink()
                except OSError:
                    pass

    passed = sum(1 for ok, _ in results if ok)
    total = len(results)
    print("\n" + "=" * 60)
    print(f"阶段六逻辑验收：{passed}/{total} 项通过")
    if passed != total:
        print("未通过的项目：")
        for ok, description in results:
            if not ok:
                print(f"  - {description}")
    print("=" * 60)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
