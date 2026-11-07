"""SmartPrint 阶段六界面验收脚本（离屏运行，不需要人工点击，也不会真出纸）。

覆盖的验收点：

1. 主窗口接入：「手动双面打印」出现在工具栏、任务菜单和右键菜单，快捷键 Ctrl+D。
2. 选中任务才可用；未选中或源文件丢失时禁用。
3. 双面打印窗口能打开，能看清「当前批次 / 当前纸张 / 当前打印面 / 当前页码 / 状态」。
4. 纸张清单表格：一张纸一行，正面 / 背面页码与状态都在，当前纸张高亮。
5. 开始后进入「打印正面」，只有「提交当前页」可用。
6. 提交一面后：按钮切换为「打印正常 / 打印异常」，「提交当前页」被禁用
   （未确认之前不能提交下一页）。
7. 打印正常后进度 +1，自动指向下一面。
8. 本批正面全部确认后：只有「确认已翻面」可用，且不能提交背面。
9. 确认翻面后进入「打印背面」，逐面提交背面页码（按纸张编号降序）…
10. 打印异常：整张纸的正反两面都显示「已作废」，当前循环继续处理其它纸张
    （补打轮由 tools/verify_stage7_gui.py 验收）。
11. 暂停 / 恢复按钮能正确切换并保留进度。
12. 主窗口详情显示「双面打印」进度与待补打张数；运行日志与任务记录同步。
13. 删除任务时双面打印记录（含补打轮）一并清理。

脚本使用独立的测试目录与测试数据库（`Data/jobs/_verify_stage6_gui_时间戳/`），
跑完自动删除，不会污染正式任务库，也不会真的出纸。

运行方式（项目根目录下）：

    conda activate py313
    python tools\\verify_stage6_gui.py
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
    PHASE_COMPLETED,
    PHASE_CONFIRM,
    PHASE_ERROR,
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
from app.ui.main_window import MainWindow  # noqa: E402
from app.utils.logging_setup import setup_logging  # noqa: E402
from app.utils.paths import ensure_dirs  # noqa: E402

A4_WIDTH_PT = 595.28
A4_HEIGHT_PT = 841.89

#: 测试任务：文件名 → 页数
DOCUMENTS: dict[str, int] = {
    "阶段六界面-五页.pdf": 5,
    "阶段六界面-丢失.pdf": 2,
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
        page.insert_text((72, 150), f"Stage 6 GUI test - page {number} / {pages}", fontsize=12)
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
            or [
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


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def run_checks(window: MainWindow, workdir: Path) -> None:
    app = QApplication.instance()
    assert app is not None

    print("\n【检查 1】主窗口接入：手动双面打印动作")
    check(window.table.rowCount() == len(DOCUMENTS), f"表格显示 {window.table.rowCount()} 个任务")
    toolbar_actions = window._toolbar.actions()
    check(window.act_duplex in toolbar_actions, "工具栏中有「手动双面打印」按钮")
    check(
        window.act_duplex.shortcut().toString() == "Ctrl+D",
        f"快捷键为 Ctrl+D（实际 {window.act_duplex.shortcut().toString()}）",
    )
    check(
        "逐面" in window.act_duplex.toolTip(),
        f"按钮提示说明逐面提交与确认：{window.act_duplex.toolTip()}",
    )
    check(
        "手动双面打印" in window.hint_label.text()
        and "确认完整出纸后才继续下一页" in window.hint_label.text(),
        "主窗口提示说明了逐面确认的规则",
    )

    task_menu_actions = []
    for menu_action in window.menuBar().actions():
        menu = menu_action.menu()
        if menu is not None and menu_action.text().startswith("任务"):
            task_menu_actions = menu.actions()
    check(window.act_duplex in task_menu_actions, "任务菜单中有「手动双面打印」")

    window.table.clearSelection()
    window._update_action_state()
    check(not window.act_duplex.isEnabled(), "未选中任务时「手动双面打印」不可用")

    gone = select_task(window, "阶段六界面-丢失.pdf")
    check(gone is not None, "找到「丢失」任务")
    Path(gone.source_path).unlink()
    window._refresh_tasks()
    select_task(window, "阶段六界面-丢失.pdf")
    check(not window.act_duplex.isEnabled(), "源文件丢失后「手动双面打印」不可用")

    task = select_task(window, "阶段六界面-五页.pdf")
    check(task is not None, "找到 5 页任务")
    check(window.act_duplex.isEnabled(), "选中任务后「手动双面打印」可用")

    print("\n【检查 2】主窗口详情：尚未开始的双面打印")
    detail = window.detail_view.toPlainText()
    check("双面打印" in detail, "详情中有「双面打印」一行")
    check("尚未开始" in window._duplex_text(task), f"提示：{window._duplex_text(task)}")

    print("\n【检查 3】打开双面打印窗口：未开始状态")
    runner = FakeSumatra(returncode=0)
    install_fake_duplex_environment(window, runner=runner)
    dialog = window.create_duplex_dialog(task)
    dialog.show_errors = False
    dialog.confirm_dialogs = False
    dialog.show()
    app.processEvents()

    check(dialog.snapshot is None, "窗口初始没有会话")
    check(dialog.start_button.isEnabled(), "「开始手动双面打印」可用")
    check(not dialog.submit_button.isEnabled(), "未开始时不能提交当前页")
    check(not dialog.confirm_button.isEnabled(), "未开始时不能确认出纸")
    check(not dialog.flip_button.isEnabled(), "未开始时不能确认翻面")
    check("尚未开始" in dialog.headline_label.text(), f"标题：{dialog.headline_label.text()}")
    check("SP6GUI" in dialog.info_label.text() and "5 页" in dialog.info_label.text(),
          f"顶部显示任务信息：{dialog.info_label.text()[:50]}…")
    check(dialog.sheet_table.rowCount() == 0, "未开始时纸张清单为空")

    print("\n【检查 4】开始：按物理纸张拆分，先打正面")
    snapshot = dialog.start()
    app.processEvents()
    check(snapshot is not None, "开始成功")
    check(snapshot.session.total_sheets == 3, f"5 页 → {snapshot.session.total_sheets} 张纸")
    check(snapshot.session.total_batches == 1, f"→ {snapshot.session.total_batches} 批")
    check(dialog.sheet_table.rowCount() == 3, f"纸张清单 {dialog.sheet_table.rowCount()} 行")
    check(dialog.batch_label.text() == "第 1 批 / 共 1 批", f"当前批次：{dialog.batch_label.text()}")
    check(dialog.sheet_label.text().startswith("第 1 张"), f"当前纸张：{dialog.sheet_label.text()}")
    check(dialog.face_label.text() == "正面", f"当前打印面：{dialog.face_label.text()}")
    check(dialog.page_label.text() == "第 1 页", f"当前页码：{dialog.page_label.text()}")
    check("剩余" not in dialog.progress_label.text(), f"总进度：{dialog.progress_label.text()}")
    check(dialog.result_count_label.text() == "5 面",
          f"剩余待确认：{dialog.result_count_label.text()}")
    check(dialog.task_status_label.text() == TaskStatus.PRINTING_FRONT.label,
          f"任务状态：{dialog.task_status_label.text()}")
    check(dialog.submit_button.isEnabled(), "可以提交当前页")
    check(not dialog.confirm_button.isEnabled(), "还没有待确认的面")
    check(not dialog.start_button.isEnabled(), "流程中不能再次开始")
    row0 = [dialog.sheet_table.item(0, column).text() for column in range(8)]
    check(row0[2].endswith("1 张") and row0[5] == "2", f"第 1 张纸：{row0[2]}，正面 {row0[3]}，背面 {row0[5]}")
    check(row0[4] == "待打印", f"正面状态：{row0[4]}")

    print("\n【检查 5】提交一面：等待用户确认，不能提交下一页")
    result = dialog.submit_current()
    app.processEvents()
    check(result is not None and result.submitted, "提交成功")
    check(len(runner.calls) == 1, "只调用了一次打印程序")
    check(runner.printed_files == ["page_001.pdf"], f"打印文件：{runner.printed_files}")
    check(dialog.face_label.text() == "正面" and dialog.page_label.text() == "第 1 页",
          "当前位置仍是第 1 张正面第 1 页")
    check("等待用户确认" in dialog.headline_label.text(),
          f"状态标题：{dialog.headline_label.text()}")
    check(not dialog.submit_button.isEnabled(), "提交后「提交当前页」被禁用")
    check(dialog.confirm_button.isEnabled(), "「打印正常」可用")
    check(dialog.anomaly_button.isEnabled(), "「打印异常」可用")
    check("命令已发送" in dialog.result_label.text(), f"结果提示：{dialog.result_label.text()}")
    check("不会自动重试" in dialog.result_label.text() or "打印正常" in dialog.result_label.text(),
          "结果提示告诉用户下一步要检查纸张")
    result_text = dialog.result_view.toPlainText()
    check("-print-to" in result_text and "page_001.pdf" in result_text,
          "结果区显示命令行与单页文件")
    row0 = [dialog.sheet_table.item(0, column).text() for column in range(8)]
    check(row0[4] == "待确认", f"纸张清单里正面状态：{row0[4]}")
    check("等待确认" in row0[7], f"整张状态：{row0[7]}")

    print("\n【检查 6】打印正常：确认后才推进")
    dialog.confirm_normal()
    app.processEvents()
    check(dialog.page_label.text() == "第 3 页", f"下一面：{dialog.page_label.text()}")
    check(dialog.headline_label.text().startswith("第 2 张正面"),
          f"状态标题：{dialog.headline_label.text()}")
    check(dialog.submit_button.isEnabled(), "确认后可以提交下一页")
    check(not dialog.confirm_button.isEnabled(), "没有待确认的面时禁用「打印正常」")
    check(dialog.progress_label.text().startswith("已确认 1 / 5 面"),
          f"总进度：{dialog.progress_label.text()}")
    row0 = [dialog.sheet_table.item(0, column).text() for column in range(8)]
    check(row0[4] == "已确认", f"第 1 张正面状态：{row0[4]}")
    check(row0[6] == "待打印", f"第 1 张背面仍是：{row0[6]}")
    check("已完成" not in row0[7], "背面还没打印，整张不算完成")

    print("\n【检查 7】正面全部完成：等待翻面，不能提交背面")
    for _ in range(2):
        dialog.submit_current()
        dialog.confirm_normal()
        app.processEvents()
    check(dialog.snapshot.phase == PHASE_WAITING_FLIP,
          f"阶段：{dialog.snapshot.phase_text}")
    check("等待你整理纸张、翻面" in dialog.headline_label.text(),
          f"状态标题：{dialog.headline_label.text()}")
    check(dialog.flip_button.isEnabled(), "「确认已翻面」可用")
    check(not dialog.submit_button.isEnabled(), "未确认翻面时不能提交背面")
    check(dialog.flip_label.text() == "等待确认翻面", f"翻面确认：{dialog.flip_label.text()}")
    check(dialog.task_status_label.text() == TaskStatus.WAITING_FLIP.label,
          f"任务状态：{dialog.task_status_label.text()}")
    check("翻面" in dialog.next_label.text(), f"下一步提示：{dialog.next_label.text()}")

    print("\n【检查 8】确认翻面：逐面打印背面")
    dialog.confirm_flip()
    app.processEvents()
    check(dialog.snapshot.phase == PHASE_BACK, f"阶段：{dialog.snapshot.phase_text}")
    check(dialog.page_label.text() == "第 4 页",
          f"背面第一面（纸堆最上面那张纸的背面）：{dialog.page_label.text()}")
    check(dialog.face_label.text() == "背面", f"当前打印面：{dialog.face_label.text()}")
    check(dialog.flip_label.text() == "已确认", f"翻面确认：{dialog.flip_label.text()}")
    check(dialog.submit_button.isEnabled(), "可以提交背面")
    check(dialog.task_status_label.text() == TaskStatus.PRINTING_BACK.label,
          f"任务状态：{dialog.task_status_label.text()}")

    dialog.submit_current()
    app.processEvents()
    check(runner.printed_files[-1] == "page_004.pdf", f"背面第一面打印：{runner.printed_files[-1]}")
    check(dialog.snapshot.phase == PHASE_CONFIRM, "背面提交后同样等待确认")
    check(not dialog.submit_button.isEnabled(), "背面也必须先确认")
    dialog.confirm_normal()
    app.processEvents()
    check(dialog.page_label.text() == "第 2 页",
          f"下一面（本轮最后一张纸的背面）：{dialog.page_label.text()}")
    anomaly_sheet = dialog.snapshot.current.sheet_number if dialog.snapshot.current else None
    anomaly_page = dialog.snapshot.current.page_number if dialog.snapshot.current else None

    print("\n【检查 9】打印异常：整张纸作废，当前循环继续")
    dialog.submit_current()
    app.processEvents()
    check(dialog.snapshot.phase == PHASE_CONFIRM, f"第 {anomaly_sheet} 张背面已提交")
    dialog.report_anomaly("测试：模拟卡纸")
    app.processEvents()
    check(dialog.snapshot.phase == PHASE_FRONT,
          f"作废后当前循环继续，阶段：{dialog.snapshot.phase_text}")
    check(dialog.snapshot.current_round == 1,
          f"正常轮已无待打面，自动开始第 {dialog.snapshot.current_round} 轮补打")
    check(dialog.page_label.text() == f"第 {anomaly_page - 1} 页",
          f"补打从作废纸张的正面第 {anomaly_page - 1} 页开始：{dialog.page_label.text()}")
    check(dialog.task_status_label.text() == TaskStatus.REPRINT.label,
          f"任务状态：{dialog.task_status_label.text()}")
    voided = [sheet for sheet in dialog.snapshot.sheets if sheet.voided]
    check(len(voided) == 1, f"只有 1 张纸作废（实际 {len(voided)}）")
    voided_sheet = voided[0] if voided else None
    check(
        voided_sheet is not None
        and voided_sheet.front_status == FACE_VOID
        and voided_sheet.back_status == FACE_VOID,
        "作废纸张的正反两面都被标记为已作废（不能只补打一面）",
    )
    row_index = (
        next(
            (row for row, item in enumerate(dialog.snapshot.sheets) if item.voided),
            -1,
        )
        if voided
        else -1
    )
    row2 = [dialog.sheet_table.item(row_index, column).text() for column in range(8)]
    check(row2[4] == "已作废", f"作废纸张正面状态：{row2[4]}")
    check(row2[6] == "已作废", f"作废纸张背面状态：{row2[6]}")
    check("已作废" in row2[7], f"整张状态：{row2[7]}")
    check(dialog.submit_button.isEnabled(), "作废后可以继续提交其它纸张")
    check("整张作废" in dialog.result_label.text(), f"提示：{dialog.result_label.text()}")
    check(
        any("整张作废" in line or "整张纸作废" in line for line in dialog.log_lines),
        "流程日志记录了整张纸作废",
    )
    check("第 1 轮补打" in dialog.reprint_label.text(), f"待补打清单：{dialog.reprint_label.text()}")
    check("待补打 1 次" in dialog.anomaly_label.text(), f"异常记录：{dialog.anomaly_label.text()}")
    anomaly_text = dialog.anomaly_view.toPlainText()
    check(f"第 {anomaly_sheet} 张" in anomaly_text and "卡纸" in anomaly_text,
          f"异常区列出了作废记录与原因（第 {anomaly_sheet} 张，第 {anomaly_page} 页）")

    print("\n【检查 10】主窗口同步：详情、日志与任务记录")
    window._report_duplex_state(task, dialog)
    app.processEvents()
    window._update_detail(task)
    check("双面打印" in window.detail_view.toPlainText(), "详情仍显示「双面打印」")
    duplex_text = window._duplex_text(task)
    check("第 1 轮补打" in duplex_text, f"详情显示补打轮次：{duplex_text}")
    log_text = window.log_view.toPlainText()
    check("手动双面打印" in log_text, "运行日志里有双面打印记录")
    events = window.repository.list_events(task.task_uid)
    kinds = {event.event_type for event in events}
    for kind in ("duplex_start", "duplex_submit", "duplex_confirm", "duplex_flip",
                 "duplex_anomaly"):
        check(kind in kinds, f"任务记录里有 {kind} 事件")
    check(
        any(event.event_type == "duplex_anomaly" and "待补打" in event.message for event in events),
        "异常事件里写明了待补打清单",
    )
    dialog.close()

    print("\n【检查 11】暂停 / 恢复按钮与进度")
    task_pause = select_task(window, "阶段六界面-五页.pdf")
    check(task_pause is not None, "重新选中任务")
    dialog2 = window.create_duplex_dialog(task_pause)
    dialog2.show_errors = False
    dialog2.confirm_dialogs = False
    dialog2.show()
    app.processEvents()
    check(not dialog2.start_button.isEnabled(), "流程中不能再次开始")
    check("已在打印流程中" in dialog2.start_button.text(),
          f"按钮文字：{dialog2.start_button.text()}")

    dialog2.pause_task()
    app.processEvents()
    check(dialog2.snapshot.phase == PHASE_PAUSED, f"阶段：{dialog2.snapshot.phase_text}")
    check(dialog2.pause_button.text() == "恢复任务(&U)", f"按钮文字：{dialog2.pause_button.text()}")
    check(not dialog2.submit_button.isEnabled(), "暂停时不能提交")
    check(dialog2.task_status_label.text() == TaskStatus.PAUSED.label,
          f"任务状态：{dialog2.task_status_label.text()}")

    dialog2.resume_task()
    app.processEvents()
    check(dialog2.snapshot.phase == PHASE_FRONT, f"恢复后阶段：{dialog2.snapshot.phase_text}")
    check(dialog2.pause_button.text() == "暂停任务(&T)", f"按钮文字：{dialog2.pause_button.text()}")
    check(dialog2.submit_button.isEnabled(), "恢复后可以提交")

    print("\n【检查 12】补打轮逐面确认至完成（正常轮已在前面的检查中走完）")
    check(dialog2.snapshot.current_round == 1,
          f"当前处于第 {dialog2.snapshot.current_round} 轮补打")
    check("第 1 轮补打" in dialog2.round_label.text(),
          f"当前轮次：{dialog2.round_label.text()}")
    check(dialog2.sheet_table.rowCount() == 4,
          f"纸张清单同时显示正常轮与补打轮（{dialog2.sheet_table.rowCount()} 行）")
    runner.calls.clear()
    steps = 0
    saw_round_flip = False
    while dialog2.snapshot is not None and not dialog2.snapshot.is_finished and steps < 20:
        snapshot = dialog2.snapshot
        if snapshot.can_submit:
            dialog2.submit_current()
        elif snapshot.can_confirm:
            dialog2.confirm_normal()
        elif snapshot.can_flip:
            if snapshot.is_reprint_round and not saw_round_flip:
                saw_round_flip = True
                check(
                    "本批" not in dialog2.next_label.text()
                    and f"第 {anomaly_sheet} 张纸" in dialog2.next_label.text(),
                    f"补打翻面提示只针对这一张纸：{dialog2.next_label.text()}",
                )
            dialog2.confirm_flip()
        else:
            break
        app.processEvents()
        steps += 1
    check(saw_round_flip, "补打轮里也要单独确认这一张纸翻面")
    check(dialog2.snapshot.phase == PHASE_COMPLETED,
          f"最终阶段：{dialog2.snapshot.phase_text}")
    check(dialog2.snapshot.confirmed_count == 5, f"已确认 {dialog2.snapshot.confirmed_count} 面")
    check(dialog2.snapshot.progress_text.endswith("100%）"), f"进度：{dialog2.snapshot.progress_text}")
    check(
        runner.printed_files
        == [f"page_{anomaly_page - 1:03d}.pdf", f"page_{anomaly_page:03d}.pdf"],
        f"补打只重打作废的那一张纸（正面 {anomaly_page - 1} → 背面 {anomaly_page}）："
        f"{runner.printed_files}",
    )
    check(len(runner.calls) == 2, f"补打一共提交 2 次（实际 {len(runner.calls)}）")
    check(not dialog2.submit_button.isEnabled(), "完成后不能继续提交")
    check("全部完成" in dialog2.headline_label.text(), f"状态标题：{dialog2.headline_label.text()}")
    check(dialog2.start_button.text().startswith("重新开始"), "完成后可以重新开始")
    window._report_duplex_state(task_pause, dialog2)
    app.processEvents()
    check("已完成" in window._duplex_text(task_pause), f"详情：{window._duplex_text(task_pause)}")
    check(
        window.repository.get_task(task_pause.task_uid).status == TaskStatus.COMPLETED,
        "任务状态为已完成",
    )
    dialog2.close()

    print("\n【检查 13】删除任务时清理双面打印记录")
    faces_before = window.repository.count_duplex_faces(task_pause.task_uid)
    check(faces_before == 7, f"删除前有 {faces_before} 条打印面记录（正常轮 5 + 补打轮 2）")
    window.delete_tasks([task_pause.task_uid], delete_files=False)
    app.processEvents()
    check(window.repository.get_task(task_pause.task_uid) is None, "任务已删除")
    check(window.repository.count_duplex_faces(task_pause.task_uid) == 0,
          "打印面记录一并清理")
    check(window.repository.get_duplex_session(task_pause.task_uid) is None,
          "双面打印会话一并清理")


def main() -> int:
    stamp = int(time.time())
    workdir = PROJECT_ROOT / "Data" / "jobs" / f"_verify_stage6_gui_{stamp}"
    sources = workdir / "sources"
    jobs_dir = workdir / "jobs"
    db_path = PROJECT_ROOT / "Data" / "database" / f"_verify_stage6_gui_{stamp}.db"
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
    print("SmartPrint 阶段六界面验收（手动双面打印窗口）")

    try:
        for name, pages in DOCUMENTS.items():
            register_task(window.repository, make_pdf(sources / name, pages), f"SP6GUI-{pages}")
        window._refresh_tasks()
        app.processEvents()
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
    print(f"阶段六界面验收：{passed}/{total} 项通过")
    if passed != total:
        print("未通过的项目：")
        for ok, description in results:
            if not ok:
                print(f"  - {description}")
    print("=" * 60)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
