"""SmartPrint 阶段七界面验收脚本（离屏运行，不需要人工点击，也不会真出纸）。

覆盖的验收点：

1. 主窗口接入：「待补打」状态筛选、状态颜色、暂停按钮在待补打任务上可用。
2. 双面打印窗口新增「当前轮次 / 纸张进度 / 待补打清单 / 异常记录」四项信息，
   纸张清单表格新增「轮次」列（8 列），正常轮显示「正常」。
3. 正面异常：整张纸正反两面都显示「已作废」，当前循环继续处理其它纸张，
   待补打清单与异常记录立刻出现内容（含原因）。
4. 正常循环结束后自动开始第 1 轮补打：轮次、任务状态（待补打）、页码、
   下一步提示、日志都跟着切换。
5. 补打轮逐面确认：正面确认后必须确认「只翻这一张纸」才能提交背面。
6. 补打中再次异常：该张纸进入第 2 轮补打，异常记录变成两条。
7. 多轮补打完成：任务完成、进度按整张纸统计、异常记录标记为补打完成。
8. 补打轮里暂停 / 恢复：按钮文案切换，未确认的面保持待核对。
9. 补打轮里取消：任务停下，已提交未确认的面不会被当成成功。
10. 删除任务时双面打印记录、补打轮次与异常记录一并清理。

脚本使用独立的测试目录与测试数据库（`Data/jobs/_verify_stage7_gui_时间戳/`），
跑完自动删除，不会污染正式任务库，也不会真的出纸。

运行方式（项目根目录下）：

    conda activate py313
    python tools\\verify_stage7_gui.py
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # 离屏运行，不弹窗

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pymupdf  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core.config import AppConfig, PrinterSettings  # noqa: E402
from app.core.duplex import (  # noqa: E402
    FACE_CONFIRMED,
    FACE_SUBMITTED,
    FACE_VOID,
    PHASE_BACK,
    PHASE_CANCELLED,
    PHASE_COMPLETED,
    PHASE_CONFIRM,
    PHASE_FRONT,
    PHASE_PAUSED,
    PHASE_WAITING_FLIP,
)
from app.core.models import Task  # noqa: E402
from app.core.pdf_inspector import inspect_pdf  # noqa: E402
from app.core.task_status import TaskStatus  # noqa: E402
from app.printer import sumatra  # noqa: E402
from app.printer import windows_printers as wp  # noqa: E402
from app.services.duplex_service import DuplexPrintService  # noqa: E402
from app.services.print_service import PrintService  # noqa: E402
from app.ui.duplex_dialog import DuplexDialog, _SHEET_COLUMNS  # noqa: E402
from app.ui.main_window import MainWindow, _STATUS_COLORS, _STATUS_FILTERS  # noqa: E402
from app.utils.logging_setup import setup_logging  # noqa: E402
from app.utils.paths import ensure_dirs  # noqa: E402

A4_WIDTH_PT = 595.28
A4_HEIGHT_PT = 841.89

#: 测试任务：文件名 → 页数
DOCUMENTS: dict[str, int] = {
    "阶段七界面-八页.pdf": 8,
    "阶段七界面-四页.pdf": 4,
}

results: list[tuple[bool, str]] = []


def check(condition: bool, description: str) -> bool:
    results.append((bool(condition), description))
    print(f"  [{'PASS' if condition else 'FAIL'}] {description}")
    return bool(condition)


def make_pdf(path: Path, pages: int) -> Path:
    document = pymupdf.open()
    for index in range(pages):
        number = index + 1
        page = document.new_page(width=A4_WIDTH_PT, height=A4_HEIGHT_PT)
        page.insert_text((72, 100), f"PAGE-{number}", fontsize=28)
        page.insert_text((72, 150), f"Stage 7 GUI test - page {number} / {pages}", fontsize=12)
    document.save(path)
    document.close()
    return path


def register_task(repository, path: Path, task_uid: str) -> Task:
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


def select_task(window: MainWindow, document_name: str) -> Task | None:
    wanted = Path(document_name).stem
    for row in range(window.table.rowCount()):
        if window.table.item(row, 1).text() == wanted:
            window.table.selectRow(row)
            QApplication.instance().processEvents()
            return window._tasks[row]
    return None


# ---------------------------------------------------------------------------
# 假的打印环境（保证验收不出纸）
# ---------------------------------------------------------------------------


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
        is_virtual=virtual,
    )


class FakeQueue:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, printer_name: str) -> wp.QueueSnapshot:
        self.calls += 1
        return wp.QueueSnapshot(printer_name, (), observed_at="fake-empty")


class FakeSumatra:
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


def install_fake_duplex_environment(
    window: MainWindow,
    *,
    printers: list[wp.PrinterInfo] | None = None,
    runner: FakeSumatra | None = None,
) -> DuplexPrintService:
    """把主窗口里的打印服务与双面服务换成「不会真的打印」的版本。"""
    config = window.config
    runner = runner or FakeSumatra()
    print_service = PrintService(
        config=AppConfig(
            printer_name=config.printer_name,
            printer=PrinterSettings(
                sumatra_path="C:/fake/SumatraPDF.exe",
                print_settings="simplex",
                timeout_seconds=30.0,
                queue_recheck_seconds=0.0,
            ),
            jobs_dir=config.jobs_dir,
            sheets_per_batch=config.sheets_per_batch,
        ),
        repository=window.repository,
        page_jobs=window.page_jobs,
        printer_reader=lambda: list(
            printers
            if printers is not None
            else [
                fake_printer(),
                fake_printer(name="PDF24", port=r"\\.\pipe\PDFPrint", driver="PDF24", virtual=True),
            ]
        ),
        queue_reader=FakeQueue(),
        sumatra_finder=lambda configured: Path("C:/fake/SumatraPDF.exe"),
        sumatra_runner=runner,
    )
    duplex = DuplexPrintService(
        repository=window.repository,
        print_service=print_service,
        config=config,
    )
    window.printer = print_service
    window.duplex = duplex
    return duplex


def open_dialog(window: MainWindow, task: Task) -> DuplexDialog:
    """打开双面打印窗口，并关掉所有确认 / 报错弹窗（离屏下不能真的弹窗）。"""
    dialog = window.create_duplex_dialog(task)
    dialog.confirm_dialogs = False
    dialog.show_errors = False
    dialog.refresh()
    return dialog


def table_text(dialog: DuplexDialog, row: int) -> list[str]:
    return [
        dialog.sheet_table.item(row, column).text()
        for column in range(dialog.sheet_table.columnCount())
    ]


def sheet_row(dialog: DuplexDialog, sheet_number: int, round_index: int) -> int:
    for row in range(dialog.sheet_table.rowCount()):
        if (
            f"第 {sheet_number} 张" == dialog.sheet_table.item(row, 2).text()
            and table_text(dialog, row)[0] == ("正常" if round_index == 0 else f"补打 {round_index}")
        ):
            return row
    return -1


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def run_checks(window: MainWindow, workdir: Path) -> None:
    app = QApplication.instance()
    assert app is not None

    # ---------------------------------------------------------------
    print("\n【检查 1】主窗口接入：待补打状态、暂停按钮、详情文案")
    # ---------------------------------------------------------------
    filters = dict(_STATUS_FILTERS)
    check("待补打" in filters, "状态筛选里新增「待补打」")
    check(filters.get("待补打") == (TaskStatus.REPRINT,), "「待补打」筛选对应 REPRINT 状态")
    check(TaskStatus.REPRINT in filters["未结束任务"], "「未结束任务」包含待补打")
    check(_STATUS_COLORS.get(TaskStatus.REPRINT) is not None,
          f"待补打状态有颜色 {_STATUS_COLORS.get(TaskStatus.REPRINT)}")

    task_main = window.repository.get_task("SP7GUI-8")
    assert task_main is not None
    window._refresh_tasks()
    app.processEvents()

    # ---------------------------------------------------------------
    print("\n【检查 2】窗口结构与「正常打印」一轮的样子")
    # ---------------------------------------------------------------
    dialog = open_dialog(window, task_main)
    check(len(_SHEET_COLUMNS) == 8, f"纸张清单共 {len(_SHEET_COLUMNS)} 列")
    check(_SHEET_COLUMNS[0] == "轮次", "第一列是「轮次」")
    check(dialog.sheet_table.columnCount() == 8, "表格实际 8 列")
    headers = [dialog.sheet_table.horizontalHeaderItem(index).text() for index in range(8)]
    check(headers == list(_SHEET_COLUMNS), f"表头：{' / '.join(headers)}")
    check(dialog.start_button.isEnabled(), "尚未开始时「开始」可用")
    check("尚未开始" in dialog.headline_label.text(),
          f"初始标题：{dialog.headline_label.text()}")

    dialog.start()
    app.processEvents()
    check(dialog.snapshot.phase == PHASE_FRONT, "开始后进入打印正面")
    check(dialog.round_label.text() == "正常打印",
          f"当前轮次：{dialog.round_label.text()}")
    check(dialog.task_status_label.text() == TaskStatus.PRINTING_FRONT.label,
          f"任务状态：{dialog.task_status_label.text()}")
    check(dialog.sheet_table.rowCount() == 4, f"8 页 → 4 张纸，表格 {dialog.sheet_table.rowCount()} 行")
    first = table_text(dialog, 0)
    check(first[0] == "正常", f"首行轮次：{first[0]}")
    check(first[1] == "第 1 批", f"首行批次：{first[1]}")
    check(first[2] == "第 1 张", f"首行纸张：{first[2]}")
    check(first[3] == "1" and first[5] == "2",
          f"首行正反面页码：{first[3]} / {first[5]}")
    check(dialog.progress_label.text() == "已确认 0 / 8 面（0%）",
          f"总进度：{dialog.progress_label.text()}")
    check(dialog.sheet_progress_label.text() == "已完成 0 / 4 张纸（0%）",
          f"纸张进度：{dialog.sheet_progress_label.text()}")
    check(dialog.reprint_label.text() == "无", f"还没有待补打：{dialog.reprint_label.text()}")
    check(dialog.anomaly_label.text() == "无",
          f"还没有异常记录：{dialog.anomaly_label.text()}")
    check("还没有异常记录" in dialog.anomaly_view.toPlainText(), "异常记录区给出空态提示")

    # ---------------------------------------------------------------
    print("\n【检查 3】正面异常：整张纸作废、当前循环继续")
    # ---------------------------------------------------------------
    dialog.submit_current()
    app.processEvents()
    check(dialog.snapshot.current.status == FACE_SUBMITTED, "第 1 页命令已发送")
    check(dialog.confirm_button.isEnabled() and not dialog.submit_button.isEnabled(),
          "提交后只能确认或报异常")
    dialog.confirm_normal()
    app.processEvents()
    check(dialog.snapshot.confirmed_count == 1, "第 1 张正面已确认")

    dialog.submit_current()  # 第 3 页（第 2 张正面）
    app.processEvents()
    check(dialog.snapshot.current.page_number == 3, "当前是第 3 页")
    before_round = dialog.snapshot.current_round
    dialog.report_anomaly("测试：模拟卡纸")
    app.processEvents()
    check(dialog.snapshot.phase == PHASE_FRONT, "报异常后仍停在打印正面（不被当成结束）")
    check(dialog.snapshot.current is not None and dialog.snapshot.current.page_number == 5,
          "当前循环继续处理第 3 张正面（第 5 页）")
    row = sheet_row(dialog, 2, 0)
    check(row >= 0, "表格里能找到第 2 张纸（正常轮）")
    values = table_text(dialog, row)
    check(values[4] == "已作废" and values[6] == "已作废",
          f"第 2 张正反两面都作废：{values[4]} / {values[6]}")
    check(values[7].startswith("已作废"), f"整张状态：{values[7]}")
    check("整张作废" in dialog.result_label.text(),
          f"结果提示：{dialog.result_label.text()[:40]}…")
    check(any("整张作废" in line for line in dialog.log_lines), "运行日志记录了整张作废")

    check(dialog.reprint_label.text() == "第 2 张（正面 3 / 背面 4）",
          f"待补打清单：{dialog.reprint_label.text()}")
    check(dialog.anomaly_label.text() == "共 1 次，待补打 1 次",
          f"异常记录：{dialog.anomaly_label.text()}")
    anomaly_text = dialog.anomaly_view.toPlainText()
    check("第 2 张" in anomaly_text and "卡纸" in anomaly_text,
          "异常记录区显示纸张编号与原因")
    check("正面" in anomaly_text, "异常记录区显示异常发生在哪一面")
    check(dialog.submit_button.isEnabled(), "作废后可以继续提交其它纸张")

    window._refresh_tasks()
    app.processEvents()
    duplex_text = window._duplex_text(window.repository.get_task("SP7GUI-8"))
    check("待补打 1 张" in duplex_text, f"主窗口详情带待补打张数：{duplex_text[:60]}…")

    # ---------------------------------------------------------------
    print("\n【检查 4】正常循环不收尾：继续把本批其它纸张打完")
    # ---------------------------------------------------------------
    for _ in range(2):  # 第 5 页、第 7 页
        dialog.submit_current()
        dialog.confirm_normal()
        app.processEvents()
    check(dialog.snapshot.phase == PHASE_WAITING_FLIP,
          f"正面打完进入等待翻面：{dialog.snapshot.phase}")
    check(dialog.snapshot.confirmed_count == 3, "正常轮已确认 3 面")
    check(dialog.flip_button.isEnabled(), "「确认已翻面」可用")
    check(not dialog.submit_button.isEnabled(), "翻面确认之前不能提交背面")
    check("本批" in dialog.next_label.text(), f"正常轮提示整批翻面：{dialog.next_label.text()[:40]}…")

    dialog.confirm_flip()
    app.processEvents()
    check(dialog.snapshot.phase == PHASE_BACK, "确认翻面后进入打印背面")
    check("背面" in dialog.next_label.text(),
          f"背面提示：{dialog.next_label.text()[:40]}…")

    dialog.submit_current()  # 第 2 页
    dialog.confirm_normal()
    app.processEvents()
    check(dialog.snapshot.current is not None and dialog.snapshot.current.page_number == 6,
          "第 4 页随整张作废被跳过，直接到第 6 页")
    for _ in range(2):  # 第 6 页、第 8 页
        dialog.submit_current()
        dialog.confirm_normal()
        app.processEvents()

    # ---------------------------------------------------------------
    print("\n【检查 5】正常循环结束 → 自动开始第 1 轮补打")
    # ---------------------------------------------------------------
    check(dialog.snapshot.current_round == 1,
          f"进入补打轮：current_round={dialog.snapshot.current_round}")
    check(dialog.snapshot.phase == PHASE_FRONT, "补打轮从正面开始")
    check(dialog.snapshot.current.page_number == 3, "补打第 2 张的正面（第 3 页）")
    check("第 1 轮补打" in dialog.round_label.text(),
          f"当前轮次：{dialog.round_label.text()}")
    check("共第 1 轮" in dialog.round_label.text(), "轮次后面标出累计轮数")
    check(dialog.task_status_label.text() == TaskStatus.REPRINT.label,
          f"任务状态：{dialog.task_status_label.text()}")
    check("第 1 轮补打" in dialog.reprint_label.text(),
          f"待补打清单：{dialog.reprint_label.text()[:40]}…")
    check("补打规则" in dialog.anomaly_view.toPlainText(), "异常记录区说明补打规则")
    events = window.repository.list_events("SP7GUI-8")
    check(any(event.event_type == "duplex_reprint_round" for event in events),
          "任务记录里留下「开始补打轮」事件")
    check(any("第 1 轮补打" in (event.message or "") for event in events),
          "事件文案带轮次")
    check(dialog.start_button.isEnabled() is False, "补打过程中不能再点「开始」")
    window._refresh_tasks()
    app.processEvents()
    duplex_text = window._duplex_text(window.repository.get_task("SP7GUI-8"))
    check("第 1 轮补打" in duplex_text, f"主窗口详情显示轮次：{duplex_text[:60]}…")

    # ---------------------------------------------------------------
    print("\n【检查 6】补打轮逐面确认：只翻这一张纸")
    # ---------------------------------------------------------------
    dialog.submit_current()
    app.processEvents()
    check(dialog.snapshot.current.face == "front" and dialog.snapshot.current.page_number == 3,
          "补打轮先打正面（第 3 页）")
    dialog.confirm_normal()
    app.processEvents()
    check(dialog.snapshot.phase == PHASE_WAITING_FLIP,
          "补打轮正面确认后等待「只翻这一张」")
    check(dialog.flip_button.isEnabled() and not dialog.submit_button.isEnabled(),
          "必须确认翻面才能打背面")
    check("本批" not in dialog.next_label.text(), "补打轮不再提示整批翻面")
    check("这张纸" in dialog.next_label.text() or "第 2 张" in dialog.next_label.text(),
          f"补打轮提示单张翻面：{dialog.next_label.text()[:50]}…")
    dialog.confirm_flip()
    app.processEvents()
    check(dialog.snapshot.phase == PHASE_BACK, "补打第 2 张进入背面")
    check(any("第 1 轮补打" in line and "翻面" in line for line in dialog.log_lines),
          "日志记录补打轮的翻面确认")

    # ---------------------------------------------------------------
    print("\n【检查 7】补打中再次异常 → 第 2 轮补打")
    # ---------------------------------------------------------------
    dialog.submit_current()  # 第 4 页
    app.processEvents()
    dialog.report_anomaly("测试：补打时再次卡纸")
    app.processEvents()
    check(dialog.snapshot.current_round == 2,
          f"再异常后进入第 2 轮补打：current_round={dialog.snapshot.current_round}")
    check("第 2 轮补打" in dialog.round_label.text(), f"当前轮次：{dialog.round_label.text()}")
    check(dialog.anomaly_label.text() == "共 2 次，待补打 2 次",
          f"异常记录：{dialog.anomaly_label.text()}（这张纸两次异常都还没结案）")
    check("已开始第 2 轮补打" in dialog.result_label.text(),
          f"结果提示：{dialog.result_label.text()[:40]}…")
    anomaly_text = dialog.anomaly_view.toPlainText()
    check(anomaly_text.count("异常面") == 2, f"异常记录区两条记录：{anomaly_text.count('异常面')} 条")
    check("补打时再次卡纸" in anomaly_text, "第二条异常原因已记录")

    # ---------------------------------------------------------------
    print("\n【检查 8】第 2 轮补打完成：任务完成、异常结案")
    # ---------------------------------------------------------------
    dialog.submit_current()
    dialog.confirm_normal()
    app.processEvents()
    dialog.confirm_flip()
    dialog.submit_current()
    dialog.confirm_normal()
    app.processEvents()
    check(dialog.snapshot.phase == PHASE_COMPLETED,
          f"补打完成后阶段：{dialog.snapshot.phase}")
    check(dialog.snapshot.confirmed_count == 8,
          f"整张纸口径全部确认：{dialog.snapshot.confirmed_count} 面")
    check(dialog.snapshot.completed_sheets == 4, "三张正常纸 + 一张补打纸")
    check(dialog.task_status_label.text() == TaskStatus.COMPLETED.label,
          f"任务状态：{dialog.task_status_label.text()}")
    check("完成" in dialog.headline_label.text(), f"标题：{dialog.headline_label.text()}")
    check("补打" in dialog.result_label.text() or "完成" in dialog.result_label.text(),
          f"结果提示：{dialog.result_label.text()[:50]}…")
    check(not dialog.submit_button.isEnabled() and not dialog.flip_button.isEnabled(),
          "完成后不能继续提交")
    anomaly_text = dialog.anomaly_view.toPlainText()
    check("结果：" in anomaly_text, "异常记录带有结案结果")
    check("补打完成" in anomaly_text or "第 2 轮补打完成" in anomaly_text,
          f"异常记录已标记补打完成：{anomaly_text[:60]}…")
    check(dialog.snapshot.open_anomaly_count == 0, "没有未结案的异常")
    check(any("duplex_complete" in line or "完成" in line for line in dialog.log_lines),
          "日志记录任务完成")

    # ---------------------------------------------------------------
    print("\n【检查 9】补打轮里暂停 / 恢复")
    # ---------------------------------------------------------------
    task_b = window.repository.get_task("SP7GUI-4")
    assert task_b is not None
    dialog_b = open_dialog(window, task_b)
    dialog_b.start()
    dialog_b.submit_current()                      # 第 1 页
    dialog_b.confirm_normal()                      # 第 1 张正面确认
    dialog_b.submit_current()                      # 第 3 页（第 2 张正面）
    dialog_b.report_anomaly("测试：正面异常")
    app.processEvents()
    check(dialog_b.snapshot.phase == PHASE_WAITING_FLIP,
          f"4 页任务：第 2 张作废后只剩第 1 张背面要打：{dialog_b.snapshot.phase}")
    check(dialog_b.snapshot.current.page_number == 2, "下一面是第 1 张背面（第 2 页）")
    dialog_b.confirm_flip()
    dialog_b.submit_current()                      # 第 2 页
    dialog_b.confirm_normal()
    app.processEvents()
    check(dialog_b.snapshot.current_round == 1, "4 页任务进入第 1 轮补打")
    check(dialog_b.snapshot.current.page_number == 3, "补打第 2 张的正面（第 3 页）")
    dialog_b.submit_current()                      # 补打第 2 张正面（第 3 页）
    app.processEvents()
    check(window.repository.get_task("SP7GUI-4").status == TaskStatus.REPRINT,
          "补打轮里任务状态是「待补打」")
    window._refresh_tasks()
    app.processEvents()
    select_task(window, "阶段七界面-四页.pdf")
    check(window.act_pause.isEnabled(), "暂停按钮在待补打任务上可用")
    dialog_b.pause_task()
    app.processEvents()
    check(dialog_b.snapshot.phase == PHASE_PAUSED, f"暂停后阶段：{dialog_b.snapshot.phase}")
    check("恢复任务" in dialog_b.pause_button.text(),
          f"按钮文案：{dialog_b.pause_button.text()}")
    check(dialog_b.task_status_label.text() == TaskStatus.PAUSED.label,
          f"任务状态：{dialog_b.task_status_label.text()}")
    check(dialog_b.snapshot.current.status == FACE_SUBMITTED,
          "暂停时那一面仍是「待确认」（不会被当成成功）")
    check(not dialog_b.submit_button.isEnabled(), "暂停时不能提交")

    dialog_b.resume_task()
    app.processEvents()
    check(dialog_b.snapshot.phase == PHASE_CONFIRM,
          f"恢复后回到待核对：{dialog_b.snapshot.phase}")
    check(dialog_b.snapshot.current.status == FACE_SUBMITTED, "恢复后不自动重发，等用户核对")
    check("暂停任务" in dialog_b.pause_button.text(), "按钮文案切回「暂停任务」")

    # ---------------------------------------------------------------
    print("\n【检查 10】补打轮里取消")
    # ---------------------------------------------------------------
    dialog_b.cancel_task()
    app.processEvents()
    check(dialog_b.snapshot.phase == PHASE_CANCELLED, f"取消后阶段：{dialog_b.snapshot.phase}")
    check(dialog_b.snapshot.current is not None
          and dialog_b.snapshot.current.status == FACE_SUBMITTED,
          "已提交未确认的面仍是 submitted")
    check(dialog_b.snapshot.confirmed_count == 2,
          f"已确认的面保留：{dialog_b.snapshot.confirmed_count} 面（第 1 张正反面）")
    check("已取消" in dialog_b.next_label.text(),
          f"下一步提示：{dialog_b.next_label.text()[:40]}…")
    check(window.repository.get_task("SP7GUI-4").status == TaskStatus.CANCELLED,
          "任务状态为已取消")
    check(not dialog_b.submit_button.isEnabled(), "取消后不能提交")

    # ---------------------------------------------------------------
    print("\n【检查 11】删除任务：补打轮次与异常记录一并清理")
    # ---------------------------------------------------------------
    check(window.repository.count_duplex_faces("SP7GUI-8") > 8,
          f"补打轮次留下了历史记录：{window.repository.count_duplex_faces('SP7GUI-8')} 行")
    check(window.repository.count_duplex_anomalies("SP7GUI-8") == 2,
          "两条异常记录在库")
    window.repository.delete_task("SP7GUI-8")
    check(window.repository.get_duplex_session("SP7GUI-8") is None, "双面会话已清理")
    check(window.repository.count_duplex_faces("SP7GUI-8") == 0, "打印面记录已清理")
    check(window.repository.count_duplex_anomalies("SP7GUI-8") == 0, "异常记录已清理")
    check(window.repository.count_print_jobs("SP7GUI-8") == 0, "打印命令记录已清理")


def main() -> int:
    stamp = int(time.time())
    workdir = PROJECT_ROOT / "Data" / "jobs" / f"_verify_stage7_gui_{stamp}"
    sources = workdir / "sources"
    jobs_dir = workdir / "jobs"
    db_path = PROJECT_ROOT / "Data" / "database" / f"_verify_stage7_gui_{stamp}.db"
    sources.mkdir(parents=True, exist_ok=True)
    jobs_dir.mkdir(parents=True, exist_ok=True)

    ensure_dirs()
    setup_logging()

    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow(
        AppConfig(
            printer_name="HP LaserJet 1020",
            printer=PrinterSettings(
                sumatra_path=str(sumatra.find_sumatra() or ""),
                print_settings="simplex",
            ),
            jobs_dir=jobs_dir,
            database_file=db_path,
            sheets_per_batch=5,
        )
    )
    print(f"工作目录：{workdir}")
    print("SmartPrint 阶段七界面验收（异常恢复与补打）")

    try:
        for name, pages in DOCUMENTS.items():
            register_task(window.repository, make_pdf(sources / name, pages), f"SP7GUI-{pages}")
        window._refresh_tasks()
        app.processEvents()
        install_fake_duplex_environment(window)
        run_checks(window, workdir)
    finally:
        try:
            window.intake.stop()
        except Exception:
            pass
        window.close()
        app.processEvents()
        shutil.rmtree(workdir, ignore_errors=True)
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
    print(f"阶段七界面验收：{passed}/{total} 项通过")
    if passed != total:
        print("未通过的项目：")
        for ok, description in results:
            if not ok:
                print(f"  - {description}")
    print("=" * 60)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
