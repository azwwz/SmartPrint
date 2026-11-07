"""SmartPrint 阶段七验收脚本（逻辑部分，不依赖界面，也不会真出纸）。

阶段七的目标（readme「七、异常处理与补打」「九、开发阶段 → 阶段 7」）：
让打印过程中出现问题时，不必简单地从头开始——异常以整张物理纸为单位作废，
正常循环与补打循环分离，补打同样逐面确认，并且可以多轮补打。

覆盖的验收点：

1. 数据层：schema v6 增加 round_index / current_round / flip_confirmed_group
   与 duplex_anomalies 表；旧的 v5 库能原地升级，历史数据保留。
2. 正面异常：整张纸的正反两面一起作废，当前循环继续处理其它纸张。
3. 反面异常：即使正面已经成功，整张纸（含正面）也要一起作废。
4. 正常循环结束后才统一补打：第 1 轮补打清单就是正常轮里作废的那些纸张。
5. 补打逐面确认：正面确认 → 单独确认这一张纸翻面 → 背面确认，
   整张纸两面都确认才算补打完成。
6. 补打中再次异常：这张纸再整张作废，进入下一轮补打清单；
   本轮结束、下一轮开始时记录「补打结束 / 补打开始」事件。
7. 异常记录字段（readme 7.6）：任务号、纸张号、正反面页码、异常面、
   时间、轮次、处理方式、后续补打结果。
8. 打印机不可用（找不到真实打印机 / 脱机）时不提交、不改变任何打印面。
9. 命令发送后程序退出：重启后该面停在「待确认」，不会被自动重发。
10. 确认后程序退出：已确认的面不会重打，进度接着上次继续。
11. 暂停 / 恢复：未确认的面保持待核对。
12. 用户取消：任务停下，已提交未确认的面不会被当成成功。
13. 演练模式在补打轮里同样不写打印记录、不改变状态。
14. 任务状态随轮次变化（待补打），删除任务时异常记录一并清理。

所有真实打印都被替换为假打印机 / 假队列 / 假打印程序，**本脚本不会出纸**。

运行方式（项目根目录下）：

    conda activate py313
    python tools\\verify_stage7.py
"""

from __future__ import annotations

import shutil
import sqlite3
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
    ANOMALY_ACTION_VOID_SHEET,
    FACE_CONFIRMED,
    FACE_PENDING,
    FACE_SUBMITTED,
    FACE_VOID,
    PHASE_BACK,
    PHASE_CANCELLED,
    PHASE_COMPLETED,
    PHASE_CONFIRM,
    PHASE_FRONT,
    PHASE_PAUSED,
    PHASE_WAITING_FLIP,
    ROUND_NORMAL,
    build_faces,
    build_round_faces,
    flip_group_key,
    round_label,
    round_short_label,
)
from app.core.models import Task  # noqa: E402
from app.core.page_planner import plan_pages  # noqa: E402
from app.core.pdf_inspector import inspect_pdf  # noqa: E402
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

#: v5 时代的双面打印表（缺 round_index / current_round / flip_confirmed_group）
_OLD_SCHEMA = """
CREATE TABLE duplex_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_uid TEXT NOT NULL UNIQUE,
    phase TEXT NOT NULL,
    sheets_per_batch INTEGER NOT NULL,
    total_sheets INTEGER NOT NULL,
    total_batches INTEGER NOT NULL,
    flip_confirmed_batch INTEGER NOT NULL DEFAULT 0,
    current_batch INTEGER NOT NULL DEFAULT 1,
    current_sheet INTEGER,
    current_face TEXT,
    current_page INTEGER,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE duplex_faces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_uid TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    batch_index INTEGER NOT NULL,
    sheet_number INTEGER NOT NULL,
    face TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    status TEXT NOT NULL,
    job_id INTEGER,
    submitted_at TEXT,
    confirmed_at TEXT,
    note TEXT NOT NULL DEFAULT '',
    UNIQUE(task_uid, step_index)
);
INSERT INTO duplex_sessions (
    task_uid, phase, sheets_per_batch, total_sheets, total_batches,
    created_at, updated_at
) VALUES ('SP-OLD-V5', 'front', 5, 1, 1, '2026-01-01T09:00:00', '2026-01-01T09:00:00');
INSERT INTO duplex_faces (
    task_uid, step_index, batch_index, sheet_number, face, page_number, status
) VALUES ('SP-OLD-V5', 0, 1, 1, 'front', 1, 'confirmed');
"""

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
        page.insert_text((72, 170), f"Stage7 page {number} / {pages}", fontsize=12)
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
    )
    printers = printers if printers is not None else [fake_printer()]
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


def submit_and_confirm(duplex: DuplexPrintService, task: Task) -> None:
    """提交当前面并确认出纸（一步走完，仅用于铺场景）。"""
    duplex.submit_current(task)
    duplex.confirm_current(task)


def table_info(connection: sqlite3.Connection, table: str) -> dict[str, str]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1]: row[2] for row in rows}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main() -> int:
    stamp = int(time.time())
    workdir = PROJECT_ROOT / "Data" / "jobs" / f"_verify_stage7_{stamp}"
    sources = workdir / "sources"
    jobs_dir = workdir / "jobs"
    db_path = PROJECT_ROOT / "Data" / "database" / f"_verify_stage7_{stamp}.db"
    legacy_db = PROJECT_ROOT / "Data" / "database" / f"_verify_stage7_legacy_{stamp}.db"
    sources.mkdir(parents=True, exist_ok=True)
    jobs_dir.mkdir(parents=True, exist_ok=True)

    print("SmartPrint 阶段七验收（异常恢复与补打）")
    print(f"工作目录：{workdir}")

    database = Database(db_path)
    database.initialize()
    repository = TaskRepository(database)

    try:
        # ---------------------------------------------------------------
        print("\n【检查 1】数据层：schema v6、轮次字段与异常记录表")
        # ---------------------------------------------------------------
        check(SCHEMA_VERSION >= 6, f"schema 版本为 v{SCHEMA_VERSION}")
        check(database.schema_version() == SCHEMA_VERSION, "schema_info 记录了当前版本")
        with database.connect() as connection:
            face_columns = table_info(connection, "duplex_faces")
            session_columns = table_info(connection, "duplex_sessions")
            anomaly_columns = table_info(connection, "duplex_anomalies")
        check("round_index" in face_columns, "duplex_faces 有 round_index（第几轮）")
        check("current_round" in session_columns, "duplex_sessions 有 current_round")
        check("flip_confirmed_group" in session_columns,
              "duplex_sessions 有 flip_confirmed_group（翻面按组确认）")
        for column in (
            "round_index",
            "sheet_number",
            "face",
            "front_page",
            "back_page",
            "reason",
            "action",
            "created_at",
            "resolved_round",
            "resolved_at",
            "result",
        ):
            check(column in anomaly_columns, f"duplex_anomalies 有 {column} 列")

        # 旧库升级：v5 表结构 + 历史数据 → initialize() 后补列且不丢数据
        legacy = sqlite3.connect(legacy_db)
        legacy.executescript(_OLD_SCHEMA)
        legacy.commit()
        legacy.close()
        legacy_database = Database(legacy_db)
        legacy_database.initialize()
        legacy_repository = TaskRepository(legacy_database)
        with legacy_database.connect() as connection:
            migrated_faces = table_info(connection, "duplex_faces")
            migrated_sessions = table_info(connection, "duplex_sessions")
            migrated_anomalies = table_info(connection, "duplex_anomalies")
        check("round_index" in migrated_faces, "旧库升级后 duplex_faces 补上了 round_index")
        check("current_round" in migrated_sessions,
              "旧库升级后 duplex_sessions 补上了 current_round")
        check(bool(migrated_anomalies), "旧库升级后建好了 duplex_anomalies 表")
        old_session = legacy_repository.get_duplex_session("SP-OLD-V5")
        check(old_session is not None and old_session.current_round == ROUND_NORMAL,
              "旧会话升级后轮次默认为 0（正常打印）")
        old_faces = legacy_repository.list_duplex_faces("SP-OLD-V5")
        check(len(old_faces) == 1 and old_faces[0].status == FACE_CONFIRMED,
              "旧打印面记录升级后仍然保留")

        # ---------------------------------------------------------------
        print("\n【检查 2】正面异常：整张作废，正反两面都进待补打清单")
        # ---------------------------------------------------------------
        task_a = register_task(
            repository, make_pdf(sources / "阶段七-十页.pdf", 10), "SP7-A"
        )
        duplex_a, runner_a = build_duplex(repository, jobs_dir)
        duplex_a.start(task_a)

        submit_and_confirm(duplex_a, task_a)                 # 第 1 张正面（第 1 页）
        duplex_a.submit_current(task_a)                      # 第 2 张正面（第 3 页）
        snapshot = duplex_a.report_anomaly(task_a, "卡纸，纸张撕裂")
        check(snapshot.phase == PHASE_FRONT,
              f"作废后仍在当前循环里打印正面（{snapshot.phase_text}）")
        check(snapshot.current is not None and snapshot.current.page_number == 5,
              "下一面是第 3 张正面（第 5 页），异常纸张已退出当前循环")
        sheet2 = next(item for item in snapshot.sheets if item.number == 2)
        check(sheet2.voided, "第 2 张纸整张作废")
        check(
            snapshot.face_for(2, "front") is not None
            and snapshot.face_for(2, "front").status == FACE_VOID,
            "正面被标记为已作废",
        )
        check(
            snapshot.face_for(2, "back") is not None
            and snapshot.face_for(2, "back").status == FACE_VOID,
            "背面还没打印，也一起作废（不能只补打一面）",
        )
        check(snapshot.void_count == 2, f"共 {snapshot.void_count} 个打印面作废")
        check(snapshot.reprint_pending_count == 1,
              f"待补打清单：{snapshot.reprint_pending_text}")
        check(
            repository.get_task(task_a.task_uid).status == TaskStatus.PRINTING_FRONT,
            "任务仍在正常打印流程（不停在异常待处理）",
        )
        check(len(runner_a.calls) == 2, "报告异常不会重新发送任何页面")

        # ---------------------------------------------------------------
        print("\n【检查 3】反面异常：正面已成功也要整张作废")
        # ---------------------------------------------------------------
        for _ in range(3):
            submit_and_confirm(duplex_a, task_a)             # 第 5、7、9 页（正面）
        check(duplex_a.require_snapshot(task_a).phase == PHASE_WAITING_FLIP,
              "正面全部确认后等待翻面")
        duplex_a.confirm_flip(task_a)
        submit_and_confirm(duplex_a, task_a)                 # 第 5 张背面（第 10 页，反面降序第一张）
        duplex_a.submit_current(task_a)                      # 第 4 张背面（第 8 页）
        snapshot = duplex_a.report_anomaly(task_a, "背面卡纸")
        sheet4 = next(item for item in snapshot.sheets if item.number == 4)
        check(sheet4.voided, "第 4 张纸整张作废")
        check(
            snapshot.face_for(4, "front") is not None
            and snapshot.face_for(4, "front").status == FACE_VOID,
            "正面（第 7 页）虽然已经确认过，也一起作废",
        )
        check(
            snapshot.face_for(4, "back") is not None
            and snapshot.face_for(4, "back").status == FACE_VOID,
            "异常所在背面被标记为已作废",
        )
        check(snapshot.void_count == 4, f"两张纸共 {snapshot.void_count} 个打印面作废")
        check(snapshot.reprint_pending_count == 2,
              f"待补打清单：{snapshot.reprint_pending_text}")
        check(snapshot.current is not None and snapshot.current.page_number == 6,
              "当前循环继续打印下一张背面（按纸张编号降序，下一个是第 3 张的背面 6）")

        # ---------------------------------------------------------------
        print("\n【检查 4】正常循环结束后自动开始第 1 轮补打")
        # ---------------------------------------------------------------
        submit_and_confirm(duplex_a, task_a)                 # 第 2 张背面（第 4 页）
        submit_and_confirm(duplex_a, task_a)                 # 第 1 张背面（第 2 页）
        snapshot = duplex_a.require_snapshot(task_a)
        check(snapshot.current_round == 1,
              f"正常循环结束，自动进入第 {snapshot.current_round} 轮补打")
        check(snapshot.round_text == "第 1 轮补打", f"轮次文案：{snapshot.round_text}")
        check(snapshot.phase == PHASE_FRONT, f"补打从正面开始（{snapshot.phase_text}）")
        check(snapshot.current is not None and snapshot.current.sheet_number == 2,
              "先补打第 2 张纸（纸张号小的先补）")
        check(snapshot.current is not None and snapshot.current.page_number == 3,
              "补打从第 3 页（正面）开始")
        check(
            repository.get_task(task_a.task_uid).status == TaskStatus.REPRINT,
            "任务状态变为「待补打」",
        )
        check(len(repository.list_duplex_faces(task_a.task_uid)) == 14,
              f"正常轮 10 面 + 补打轮 4 面 = {len(repository.list_duplex_faces(task_a.task_uid))} 条记录")
        events = repository.list_events(task_a.task_uid, limit=200)
        round_events = [e for e in events if e.event_type == "duplex_reprint_round"]
        check(bool(round_events), "留下了 duplex_reprint_round 事件")
        if round_events:
            message = round_events[-1].message
            check("第 1 轮补打" in message, f"事件写明轮次：{message[:60]}…")
            check("第 2 张" in message and "第 4 张" in message,
                  "事件列出了待补打的纸张")
        check(snapshot.total_sheets == 5, f"物理纸张总数仍是 {snapshot.total_sheets} 张")
        check(snapshot.completed_sheets == 3, f"已完成 {snapshot.completed_sheets} 张纸")

        # ---------------------------------------------------------------
        print("\n【检查 5】补打逐面确认：整张纸两面都确认才算完成")
        # ---------------------------------------------------------------
        duplex_a.submit_current(task_a)                      # 补打：第 3 页
        duplex_a.confirm_current(task_a)
        snapshot = duplex_a.require_snapshot(task_a)
        check(snapshot.phase == PHASE_WAITING_FLIP,
              f"补打也要单独确认翻面（{snapshot.phase_text}）")
        check(snapshot.current is not None and snapshot.current.page_number == 4,
              "下一面是这一张纸的背面（第 4 页）")
        check(snapshot.flip_confirmed_for_current is False, "这一张纸还没有确认翻面")
        check(not snapshot.can_submit, "未确认翻面之前不能提交背面")
        duplex_a.confirm_flip(task_a)
        snapshot = duplex_a.require_snapshot(task_a)
        check(snapshot.phase == PHASE_BACK, f"翻面后打印背面（{snapshot.phase_text}）")
        duplex_a.submit_current(task_a)
        check(
            repository.get_duplex_session(task_a.task_uid).flip_confirmed_group
            == flip_group_key(1, 1, 2),
            "翻面确认按「轮次 + 纸张」记在 flip_confirmed_group",
        )
        duplex_a.confirm_current(task_a)
        snapshot = duplex_a.require_snapshot(task_a)
        anomalies = {item.sheet_number: item for item in snapshot.anomalies}
        resolved = anomalies.get(2)
        check(resolved is not None and resolved.resolved_round == 1,
              "第 2 张纸的异常记录标记为在第 1 轮补打解决")
        check(resolved is not None and resolved.resolved_at is not None,
              "异常记录写下了解决时间")
        check(resolved is not None and "完成" in resolved.result_text,
              f"异常记录的补打结果：{resolved.result_text if resolved else '—'}")
        check(
            any(e.event_type == "duplex_anomaly_resolved" for e in
                repository.list_events(task_a.task_uid, limit=200)),
            "留下了 duplex_anomaly_resolved 事件",
        )
        check(snapshot.current is not None and snapshot.current.sheet_number == 4,
              "接着补打第 4 张纸")
        check(
            repository.get_task(task_a.task_uid).status == TaskStatus.REPRINT,
            "补打期间任务状态是「待补打」",
        )

        # ---------------------------------------------------------------
        print("\n【检查 6】补打中再次异常：进入下一轮补打")
        # ---------------------------------------------------------------
        duplex_a.submit_current(task_a)                      # 第 1 轮：第 7 页
        duplex_a.confirm_current(task_a)
        duplex_a.confirm_flip(task_a)
        duplex_a.submit_current(task_a)                      # 第 1 轮：第 8 页
        snapshot = duplex_a.report_anomaly(task_a, "补打时又卡纸")
        check(snapshot.current_round == 2,
              f"第 1 轮补打里只剩这一张，作废后立即开始第 {snapshot.current_round} 轮")
        check(snapshot.phase == PHASE_FRONT and snapshot.current is not None
              and snapshot.current.page_number == 7,
              "第 2 轮补打从第 4 张纸的正面（第 7 页）重新开始")
        check(
            repository.get_task(task_a.task_uid).status == TaskStatus.REPRINT,
            "任务状态仍是「待补打」",
        )
        events = repository.list_events(task_a.task_uid, limit=200)
        done_events = [e for e in events if e.event_type == "duplex_reprint_done"]
        check(bool(done_events), "留下了 duplex_reprint_done 事件（第 1 轮结束）")
        if done_events:
            message = done_events[-1].message
            check("第 1 轮补打" in message, f"事件写明结束的轮次：{message[:60]}…")
            check("1 张纸" in message, "事件写明本轮有 1 张纸再次异常")
        round_events = [e for e in events if e.event_type == "duplex_reprint_round"]
        check(len(round_events) == 2, f"共两次「开始补打」事件（实际 {len(round_events)}）")

        # ---------------------------------------------------------------
        print("\n【检查 7】多轮补打直到全部纸张确认完成")
        # ---------------------------------------------------------------
        duplex_a.submit_current(task_a)                      # 第 2 轮：第 7 页
        duplex_a.confirm_current(task_a)
        duplex_a.confirm_flip(task_a)
        duplex_a.submit_current(task_a)                      # 第 2 轮：第 8 页
        snapshot = duplex_a.confirm_current(task_a)
        check(snapshot.phase == PHASE_COMPLETED, f"最终阶段：{snapshot.phase_text}")
        check(snapshot.confirmed_count == 10, f"已确认 {snapshot.confirmed_count} 面")
        check(snapshot.progress_text.endswith("100%）"), f"进度：{snapshot.progress_text}")
        check(snapshot.completed_sheets == 5 and snapshot.total_sheets == 5,
              f"纸张进度：{snapshot.sheet_progress_text}")
        check(snapshot.has_reprint and snapshot.latest_round == 2,
              f"共 {snapshot.latest_round} 轮补打")
        check(
            repository.get_task(task_a.task_uid).status == TaskStatus.COMPLETED,
            "任务状态变为「已完成」",
        )
        check(
            runner_a.printed_files
            == [
                "page_001.pdf", "page_003.pdf", "page_005.pdf", "page_007.pdf",
                "page_009.pdf", "page_010.pdf", "page_008.pdf", "page_006.pdf",
                "page_002.pdf", "page_003.pdf", "page_004.pdf",
                "page_007.pdf", "page_008.pdf",
                "page_007.pdf", "page_008.pdf",
            ],
            f"提交顺序（正常轮 → 第 1 轮补打 → 第 2 轮补打）：{runner_a.printed_files}",
        )
        check(len(runner_a.calls) == 15, f"一共提交 {len(runner_a.calls)} 次（4、7、9 面的背面随整张作废被跳过）")
        check(len(repository.list_duplex_faces(task_a.task_uid)) == 16,
              "打印面记录保留了每一轮（历史行不删除）")
        text = snapshot.to_text()
        for keyword in ("当前轮次", "纸张进度", "待补打清单", "异常记录"):
            check(keyword in text, f"状态文本包含「{keyword}」")

        # ---------------------------------------------------------------
        print("\n【检查 8】异常记录字段（readme 7.6）")
        # ---------------------------------------------------------------
        records = repository.list_duplex_anomalies(task_a.task_uid)
        check(len(records) == 3, f"共 {len(records)} 条异常记录")
        check(all(item.task_uid == task_a.task_uid for item in records),
              "每条记录都带任务号")
        check(all(item.action == ANOMALY_ACTION_VOID_SHEET for item in records),
              "每条记录都写明处理方式：整张纸作废")
        check(all(item.reason for item in records), "每条记录都有异常原因")
        check(all(item.created_at is not None for item in records),
              "每条记录都有异常时间")
        first = records[0]
        check(first.sheet_number == 2 and first.face == "front",
              "第 1 条记录对应第 2 张纸正面")
        check(first.front_page == 3 and first.back_page == 4,
              f"记录里同时有正面 / 背面页码：{first.pages_text}")
        check(first.round_index == ROUND_NORMAL, "记录写明了发生轮次（正常打印）")
        second = records[1]
        check(second.sheet_number == 4 and second.face == "back",
              "第 2 条记录对应第 4 张纸背面")
        check(second.front_page == 7 and second.back_page == 8,
              f"反面容异常也记录正反面页码：{second.pages_text}")
        third = records[2]
        check(third.round_index == 1, "第 3 条记录发生在第 1 轮补打")
        check(records[0].resolved_round == 1,
              "第 2 张纸的异常在第 1 轮补打里解决")
        check(records[1].resolved_round == 2 and records[2].resolved_round == 2,
              "第 3 张纸的两次异常都在第 2 轮补打里一起解决")
        check(all(item.resolved_at is not None for item in records),
              "三条记录都有解决时间")
        check(all("完成" in item.result_text for item in records),
              "三条记录都写明了补打结果")
        record_text = records[0].to_text()
        for keyword in ("第 2 张", "正面 3", "背面 4", "异常面", "原因", "结果"):
            check(keyword in record_text, f"异常记录文本包含「{keyword}」")
        check(snapshot.anomaly_text.startswith("共 3 次"),
              f"异常统计：{snapshot.anomaly_text}")
        check(snapshot.open_anomaly_count == 0, "没有待补打的异常记录")
        check(repository.count_duplex_anomalies(task_a.task_uid) == 3,
              "count_duplex_anomalies 与记录数一致")

        # ---------------------------------------------------------------
        print("\n【检查 9】打印机不可用：不提交、不改变任何打印面")
        # ---------------------------------------------------------------
        task_b = register_task(
            repository, make_pdf(sources / "阶段七-四页.pdf", 4), "SP7-B"
        )
        duplex_b, runner_b = build_duplex(repository, jobs_dir, printers=[])
        duplex_b.start(task_b)
        result = duplex_b.submit_current(task_b)
        check(not result.submitted, "找不到真实打印机时没有提交")
        check(bool(result.error), f"返回了原因：{result.error[:40]}…")
        check(len(runner_b.calls) == 0, "没有调用打印程序")
        snapshot = duplex_b.require_snapshot(task_b)
        check(snapshot.phase == PHASE_FRONT, f"阶段没变：{snapshot.phase_text}")
        check(snapshot.current is not None and snapshot.current.status == FACE_PENDING,
              "打印面仍是「待打印」（不把失败当成已发送）")
        check(snapshot.can_submit, "修好打印机后还可以再提交")

        offline, _ = build_duplex(
            repository, jobs_dir, printers=[fake_printer(status=wp.PRINTER_STATUS_OFFLINE)]
        )
        duplex_b2, runner_b2 = offline, _
        duplex_b2.start(task_b, restart=True)
        result = duplex_b2.submit_current(task_b)
        check(not result.submitted, "打印机脱机时也没有提交")
        check(len(runner_b2.calls) == 0, "脱机时没有调用打印程序")

        # ---------------------------------------------------------------
        print("\n【检查 10】命令发送后程序退出：重启后停在待核对")
        # ---------------------------------------------------------------
        task_c = register_task(
            repository, make_pdf(sources / "阶段七-四页退出.pdf", 4), "SP7-C"
        )
        duplex_c, runner_c = build_duplex(repository, jobs_dir)
        duplex_c.start(task_c)
        result = duplex_c.submit_current(task_c)
        check(result.submitted, "第 1 页命令已发送")
        fresh_repository = TaskRepository(Database(db_path))
        fresh_duplex, fresh_runner = build_duplex(fresh_repository, jobs_dir)
        recovered = fresh_duplex.require_snapshot(task_c)
        check(recovered.phase == PHASE_CONFIRM,
              f"重启后停在「{recovered.phase_text}」")
        check(recovered.current is not None and recovered.current.status == FACE_SUBMITTED,
              "那一面保持 submitted（待用户核对）")
        check(recovered.awaiting_confirmation, "界面会提示用户检查是否已经出纸")
        check(not recovered.can_submit, "核对之前不能提交下一页")
        check(recovered.can_confirm, "可以确认出纸，也可以按异常处理")
        check(
        "等待用户确认" in recovered.status_text,
        f"状态文案提示用户核对：{recovered.status_text[:40]}…",
    )
        check(len(fresh_runner.calls) == 0, "重启不会自动重发")
        check(
            fresh_repository.get_task(task_c.task_uid).status == TaskStatus.PRINTING_FRONT,
            "任务状态没有因为重启而变化",
        )

        # ---------------------------------------------------------------
        print("\n【检查 11】确认出纸后退出：已确认的面不会重打")
        # ---------------------------------------------------------------
        recovered_confirm = fresh_duplex.confirm_current(task_c)
        duplex_c2, runner_c2 = build_duplex(fresh_repository, jobs_dir)
        resumed = duplex_c2.require_snapshot(task_c)
        check(resumed.current is not None and resumed.current.page_number == 3,
              "重启后接着第 3 页（第 2 张正面）")
        check(resumed.confirmed_count == 1, f"已确认 {resumed.confirmed_count} 面")
        check(len(runner_c2.calls) == 0, "重启后没有重打已经确认的页面")
        submit_and_confirm(duplex_c2, task_c)
        check(duplex_c2.require_snapshot(task_c).confirmed_count == 2, "继续确认第 3 页")

        # ---------------------------------------------------------------
        print("\n【检查 12】暂停 / 恢复：未确认的面保持待核对")
        # ---------------------------------------------------------------
        duplex_c2.confirm_flip(task_c)                        # 本批正面确认完，翻面
        duplex_c2.submit_current(task_c)                      # 第 1 张背面（第 2 页）
        paused = duplex_c2.pause(task_c)
        check(paused.phase == PHASE_PAUSED, f"任务已暂停：{paused.phase_text}")
        check(paused.can_resume and not paused.can_submit, "暂停时不能提交")
        check(
            repository.get_task(task_c.task_uid).status == TaskStatus.PAUSED,
            "任务状态变为「已暂停」",
        )
        check(
            any(e.event_type == "duplex_pause" for e in
                repository.list_events(task_c.task_uid, limit=100)),
            "留下了暂停事件",
        )
        resumed_snapshot = duplex_c2.resume(task_c)
        check(resumed_snapshot.phase == PHASE_CONFIRM,
              f"恢复后回到「{resumed_snapshot.phase_text}」")
        check(resumed_snapshot.awaiting_confirmation, "恢复后仍要处理那个未确认的面")
        check(not resumed_snapshot.can_submit, "未确认之前仍然不能提交下一页")
        check(len(runner_c2.calls) == 2, "暂停恢复没有重发任何页面")

        # ---------------------------------------------------------------
        print("\n【检查 13】用户取消：不会把未确认的面当成成功")
        # ---------------------------------------------------------------
        cancelled = duplex_c2.cancel(task_c)
        check(cancelled.phase == PHASE_CANCELLED, f"任务已取消：{cancelled.phase_text}")
        check(not cancelled.can_submit and not cancelled.can_confirm,
              "取消后不能再提交或确认")
        check(
            repository.get_task(task_c.task_uid).status == TaskStatus.CANCELLED,
            "任务状态变为「已取消」",
        )
        check(cancelled.confirmed_count == 2,
              f"已确认的仍然只有 {cancelled.confirmed_count} 面（未确认的不算成功）")
        check("重新开始" in cancelled.next_action_text,
              f"下一步提示：{cancelled.next_action_text}")
        check(
            any(e.event_type == "duplex_cancel" for e in
                repository.list_events(task_c.task_uid, limit=100)),
            "留下了取消事件",
        )

        # ---------------------------------------------------------------
        print("\n【检查 14】演练模式：补打轮里也只生成命令")
        # ---------------------------------------------------------------
        task_d = register_task(
            repository, make_pdf(sources / "阶段七-四页演练.pdf", 4), "SP7-D"
        )
        duplex_d, runner_d = build_duplex(repository, jobs_dir)
        duplex_d.start(task_d)
        duplex_d.submit_current(task_d)
        duplex_d.report_anomaly(task_d, "演练：模拟卡纸")
        submit_and_confirm(duplex_d, task_d)                 # 第 2 张正面（第 3 页）
        duplex_d.confirm_flip(task_d)
        submit_and_confirm(duplex_d, task_d)                 # 第 2 张背面（第 4 页）
        snapshot = duplex_d.require_snapshot(task_d)
        check(snapshot.current_round == 1, "4 页任务进入第 1 轮补打")
        calls_before = len(runner_d.calls)
        dry = duplex_d.submit_current(task_d, dry_run=True)
        check(dry.dry_run and not dry.submitted, "演练模式不算提交")
        check(len(runner_d.calls) == calls_before, "演练模式没有调用打印程序")
        after = duplex_d.require_snapshot(task_d)
        check(after.current is not None and after.current.status == FACE_PENDING,
              "演练后打印面仍是「待打印」")
        check(repository.count_print_jobs(task_d.task_uid) == 3,
              "演练没有新增打印记录（仍是前面的 3 条命令）")

        # ---------------------------------------------------------------
        print("\n【检查 15】轮次文案与纯函数")
        # ---------------------------------------------------------------
        check(round_label(0) == "正常打印", f"round_label(0) = {round_label(0)}")
        check(round_label(2) == "第 2 轮补打", f"round_label(2) = {round_label(2)}")
        check(round_short_label(0) == "正常", f"round_short_label(0) = {round_short_label(0)}")
        check(round_short_label(3) == "补打 3", f"round_short_label(3) = {round_short_label(3)}")
        check(flip_group_key(0, 2, 7) == "0:2",
              "正常轮按批次确认翻面（整批一起翻）")
        check(flip_group_key(2, 2, 7) == "2:7",
              "补打轮按「轮次 + 纸张」确认翻面（一张一张翻）")
        plan = plan_pages(10, 5)
        faces = build_faces("SP7-PLAN", plan)
        reprint = build_round_faces("SP7-PLAN", faces, 1, [3, 2])
        check(len(reprint) == 4, f"补打 2 张纸生成 {len(reprint)} 个打印面")
        check(
            [(face.sheet_number, face.face, face.page_number) for face in reprint]
            == [(2, "front", 3), (2, "back", 4), (3, "front", 5), (3, "back", 6)],
            "补打面按纸张号升序、每张先正面后背面",
        )
        check(all(face.round_index == 1 for face in reprint), "补打面的轮次为 1")
        check(
            [face.step_index for face in reprint]
            == [len(faces), len(faces) + 1, len(faces) + 2, len(faces) + 3],
            "补打面的 step_index 接在历史记录之后",
        )
        check(reprint[0].full_label.startswith("第 1 轮补打"),
              f"补打面标签带轮次：{reprint[0].full_label}")

        # ---------------------------------------------------------------
        print("\n【检查 16】删除任务：双面打印与异常记录一并清理")
        # ---------------------------------------------------------------
        faces_before = repository.count_duplex_faces(task_a.task_uid)
        anomalies_before = repository.count_duplex_anomalies(task_a.task_uid)
        check(faces_before == 16 and anomalies_before == 3,
              f"删除前有 {faces_before} 条打印面、{anomalies_before} 条异常记录")
        repository.delete_task(task_a.task_uid)
        check(repository.get_task(task_a.task_uid) is None, "任务已删除")
        check(repository.count_duplex_faces(task_a.task_uid) == 0, "打印面记录一并清理")
        check(repository.count_duplex_anomalies(task_a.task_uid) == 0, "异常记录一并清理")
        check(repository.get_duplex_session(task_a.task_uid) is None, "会话一并清理")

    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        for path in (db_path, legacy_db):
            for suffix in ("", "-wal", "-shm"):
                candidate = Path(str(path) + suffix)
                if candidate.exists():
                    try:
                        candidate.unlink()
                    except OSError:
                        pass

    passed = sum(1 for ok, _ in results if ok)
    total = len(results)
    print("\n" + "=" * 60)
    print(f"阶段七验收：{passed}/{total} 项通过")
    if passed != total:
        print("未通过的项目：")
        for ok, description in results:
            if not ok:
                print(f"  - {description}")
    print("=" * 60)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
