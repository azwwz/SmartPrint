"""SmartPrint 阶段八界面验收脚本（离屏运行，不需要人工点击，也不会真出纸）。

覆盖的验收点：

1. 任务列表改成 9 列：新增「批次数」与「打印进度」，数字列右对齐。
2. 打印进度列按任务状态显示：未开始「—」、进行中「已完成 x / y 张纸」、
   有异常未补打时追加「待补打 N 张」、补打轮显示「第 N 轮补打」、
   完成后「已完成」、取消后「已取消」。
3. 阶段八的动作齐全：设置、异常记录、运行日志、备份数据、从备份恢复、归档已完成任务。
4. 状态栏与底部提示带上打印机、每批张数、翻面方向。
5. 双面打印窗口里的纸张示意图：显示当前纸张正反面页码与状态，
   等待翻面时给出「本批纸张 / 这一张纸」的翻面范围说明。
6. 设置窗口：默认值来自配置，改动后能保存到配置文件，
   打印机名为空时拒绝保存；保存后主窗口立刻按新配置刷新。
7. 翻面方向改成「沿短边」后，示意图提示随之改成上下翻。
8. 日志查看窗口：读日志尾部、按级别过滤、按关键字过滤。
9. 异常记录总览：默认只看待补打，能切换查看全部，能跳回任务的双面打印窗口。
10. 备份与归档服务接到主窗口上，备份能写出压缩包，已完成任务能归档。
11. 「关于」文案包含阶段八说明。

脚本使用独立的测试目录与测试数据库（`Data/jobs/_verify_stage8_gui_时间戳/`），
跑完自动删除，不会污染正式任务库，也不会真的出纸。

运行方式（项目根目录下）：

    conda activate py313
    python tools\\verify_stage8_gui.py
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # 离屏运行，不弹窗

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pymupdf  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core.config import (  # noqa: E402
    FLIP_DIRECTIONS,
    AppConfig,
    PrinterSettings,
    load_config,
)
from app.core.duplex import (  # noqa: E402
    PHASE_BACK,
    PHASE_CANCELLED,
    PHASE_COMPLETED,
    PHASE_FRONT,
    PHASE_WAITING_FLIP,
)
from app.core.models import Task  # noqa: E402
from app.core.pdf_inspector import inspect_pdf  # noqa: E402
from app.core.task_status import TaskStatus  # noqa: E402
from app.printer import sumatra  # noqa: E402
from app.printer import windows_printers as wp  # noqa: E402
from app.services.duplex_service import DuplexPrintService  # noqa: E402
from app.services.print_service import PrintService  # noqa: E402
from app.ui.anomaly_dialog import ANOMALY_COLUMNS, AnomalyDialog  # noqa: E402
from app.ui.duplex_dialog import DuplexDialog, _SHEET_COLUMNS  # noqa: E402
from app.ui.log_view_dialog import LEVEL_CHOICES, LogViewDialog  # noqa: E402
from app.ui.main_window import (  # noqa: E402
    _COLUMN_SORT_KEYS,
    _TASK_COLUMNS,
    MainWindow,
)
from app.ui.settings_dialog import SettingsDialog  # noqa: E402
from app.utils.logging_setup import setup_logging  # noqa: E402
from app.utils.paths import ensure_dirs  # noqa: E402

A4_WIDTH_PT = 595.28
A4_HEIGHT_PT = 841.89

#: 测试任务：文件名 → 页数
DOCUMENTS: dict[str, int] = {
    "阶段八界面-四页.pdf": 4,      # 主流程：异常 → 待补打 → 补打轮 → 完成
    "阶段八界面-两页.pdf": 2,      # 暂停前取消
    "阶段八界面-八页.pdf": 8,      # 没有双面会话，进度列显示「—」
    "阶段八界面-十页.pdf": 10,     # 示意图与翻面方向
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
        page.insert_text((72, 150), f"Stage 8 GUI test - page {number} / {pages}", fontsize=12)
    document.save(path)
    document.close()
    return path


def register_task(repository, path: Path, task_uid: str, *, pages: int) -> Task:
    inspection = inspect_pdf(path)
    now = datetime.now()
    task = Task(
        task_uid=task_uid,
        file_name=path.name,
        document_name=path.stem,
        source_path=path,
        source_job_id=f"JOB-{task_uid}",
        file_size=path.stat().st_size,
        page_count=inspection.page_count or pages,
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


def row_of(window: MainWindow, document_name: str) -> int:
    wanted = Path(document_name).stem
    for row in range(window.table.rowCount()):
        if window.table.item(row, 1).text() == wanted:
            return row
    return -1


def cell(window: MainWindow, row: int, column: int) -> str:
    item = window.table.item(row, column)
    return item.text() if item is not None else ""


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
            flip_direction=config.flip_direction,
            database_file=config.database_file,
            archive_root=config.archive_root,
            backup_dir=config.backup_dir,
        ),
        repository=window.repository,
        page_jobs=window.page_jobs,
        printer_reader=lambda: [
            fake_printer(),
            fake_printer(name="PDF24", port=r"\\.\pipe\PDFPrint", driver="PDF24", virtual=True),
        ],
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


def sheet_row(dialog: DuplexDialog, sheet_number: int, round_index: int = 0) -> int:
    for row in range(dialog.sheet_table.rowCount()):
        if (
            dialog.sheet_table.item(row, 2).text() == f"第 {sheet_number} 张"
            and dialog.sheet_table.item(row, 0).text()
            == ("正常" if round_index == 0 else f"补打 {round_index}")
        ):
            return row
    return -1


def submit_and_confirm(dialog: DuplexDialog) -> None:
    """提交当前页并确认「完整出纸」。"""
    dialog.submit_current()
    QApplication.instance().processEvents()
    dialog.confirm_normal()
    QApplication.instance().processEvents()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def run_checks(window: MainWindow, workdir: Path) -> None:
    app = QApplication.instance()
    assert app is not None

    # ---------------------------------------------------------------
    print("\n【检查 1】任务列表：9 列结构与打印进度")
    # ---------------------------------------------------------------
    check(len(_TASK_COLUMNS) == 9, f"任务列表共 {len(_TASK_COLUMNS)} 列")
    check(_TASK_COLUMNS[4] == "批次数" and _TASK_COLUMNS[8] == "打印进度",
          f"新增列：{_TASK_COLUMNS[4]} / {_TASK_COLUMNS[8]}")
    headers = [
        window.table.horizontalHeaderItem(index).text()
        for index in range(window.table.columnCount())
    ]
    check(headers == list(_TASK_COLUMNS), f"表头：{' / '.join(headers)}")

    plain_row = row_of(window, "阶段八界面-八页")
    check(plain_row >= 0, "找到没有双面会话的任务")
    check(cell(window, plain_row, 8) == "—",
          f"没有双面会话时进度列显示「{cell(window, plain_row, 8)}」")
    check(cell(window, plain_row, 4).endswith("批"),
          f"批次数列显示「{cell(window, plain_row, 4)}」")
    item = window.table.item(plain_row, 2)
    check(bool(item.textAlignment() & Qt.AlignRight), "页数列右对齐")
    check(sorted(_COLUMN_SORT_KEYS) == [0, 1, 2, 3, 4, 5, 6, 7],
          f"可排序列：{sorted(_COLUMN_SORT_KEYS)}")

    # ---------------------------------------------------------------
    print("\n【检查 2】阶段八的动作、状态栏与底部提示")
    # ---------------------------------------------------------------
    check("设置" in window.act_settings.text(), f"动作：{window.act_settings.text()}")
    check(window.act_settings.shortcut().toString() == "Ctrl+,",
          f"设置快捷键：{window.act_settings.shortcut().toString()}")
    check("异常记录" in window.act_anomalies.text(), f"动作：{window.act_anomalies.text()}")
    check(window.act_anomalies.shortcut().toString() == "Ctrl+Shift+E",
          f"异常记录快捷键：{window.act_anomalies.shortcut().toString()}")
    check("运行日志" in window.act_log.text(), f"动作：{window.act_log.text()}")
    check(window.act_log.shortcut().toString() == "Ctrl+L",
          f"运行日志快捷键：{window.act_log.shortcut().toString()}")
    check("备份数据" in window.act_backup.text(), f"动作：{window.act_backup.text()}")
    check("从备份恢复" in window.act_restore.text(), f"动作：{window.act_restore.text()}")
    check("归档" in window.act_archive.text(), f"动作：{window.act_archive.text()}")
    toolbar_actions = set(window._toolbar.actions())
    check(window.act_settings in toolbar_actions
          and window.act_anomalies in toolbar_actions
          and window.act_log in toolbar_actions,
          "工具栏里有设置、异常记录、运行日志")

    status_text = window.statusBar().currentMessage()
    check(window.config.printer_name in status_text, "状态栏显示打印机名")
    check(f"每批 {window.config.sheets_per_batch} 张纸" in status_text, "状态栏显示每批张数")
    check(window.config.flip_direction_text in status_text, "状态栏显示翻面方向")
    hint = window.hint_label.text()
    check("阶段八" in hint, "底部提示写明阶段八新增内容")

    # ---------------------------------------------------------------
    print("\n【检查 3】双面打印窗口：纸张示意图与翻面提示（沿长边）")
    # ---------------------------------------------------------------
    task_diagram = window.repository.get_task("SP8GUI-10")
    assert task_diagram is not None
    dialog_d = open_dialog(window, task_diagram)
    check(dialog_d.diagram.sheet_number is None or dialog_d.diagram.description == "",
          f"未开始时示意图为空：{dialog_d.diagram.description!r}")
    check("长边" in dialog_d.flip_hint_label.text(),
          f"翻面说明：{dialog_d.flip_hint_label.text()[:30]}…")

    dialog_d.start()
    app.processEvents()
    check("第 1 张纸" in dialog_d.diagram.description,
          f"示意图标题：{dialog_d.diagram.description}")
    check("第 1 页" in dialog_d.diagram.description
          and "第 2 页" in dialog_d.diagram.description,
          "示意图里同时标出正反面页码")
    check("蓝框" in dialog_d.diagram_note_label.text()
          or "当前" in dialog_d.diagram_note_label.text(),
          f"示意图说明：{dialog_d.diagram_note_label.text()[:40]}…")

    submit_and_confirm(dialog_d)          # 第 1 张正面（第 1 页）
    dialog_d.submit_current()             # 第 2 张正面（第 3 页）先提交
    app.processEvents()
    dialog_d.report_anomaly("模拟卡纸：第 2 张")
    app.processEvents()
    check(dialog_d.snapshot.phase == PHASE_FRONT
          and dialog_d.snapshot.current.page_number == 5,
          f"整张作废后继续下一面：第 {dialog_d.snapshot.current.page_number} 页")
    check("第 2 张" in dialog_d.anomaly_view.toPlainText(), "异常区记录了第 2 张纸")
    check("模拟卡纸" in dialog_d.anomaly_view.toPlainText(), "异常区记录了原因")

    submit_and_confirm(dialog_d)          # 第 3 张正面（第 5 页）
    dialog_d.submit_current()             # 第 4 张正面（第 7 页）
    app.processEvents()
    dialog_d.confirm_normal()
    app.processEvents()
    dialog_d.submit_current()             # 第 5 张正面（第 9 页）
    app.processEvents()
    dialog_d.confirm_normal()
    app.processEvents()
    check(dialog_d.snapshot.phase == PHASE_WAITING_FLIP,
          f"正面全部确认后等待翻面：{dialog_d.snapshot.phase}")
    note = dialog_d.diagram_note_label.text()
    check("本批纸张" in note, f"正常轮翻面范围是本批纸张：{note[:36]}…")
    check("左右翻" in note, f"翻面说明是左右翻：{note[:36]}…")
    check("第 1 张" in dialog_d.diagram.description or "第 5 张" in dialog_d.diagram.description,
          f"等待翻面时示意图仍显示当前纸张：{dialog_d.diagram.description}")

    # ---------------------------------------------------------------
    print("\n【检查 4】设置窗口：默认值、改动、保存")
    # ---------------------------------------------------------------
    config_path = workdir / "config" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    settings = SettingsDialog(window.config, None, config_path=config_path)
    settings.show_errors = False
    check(settings.sheets_spin.value() == window.config.sheets_per_batch,
          f"默认每批 {settings.sheets_spin.value()} 张纸")
    check(settings.flip_combo.currentData() == window.config.flip_direction,
          f"默认翻面方向：{settings.flip_combo.currentData()}")
    check(settings.flip_combo.count() == len(FLIP_DIRECTIONS),
          f"翻面方向共 {settings.flip_combo.count()} 项")
    check(settings.backup_keep_spin.value() == window.config.backup.keep,
          f"默认保留备份份数：{settings.backup_keep_spin.value()}")
    check(abs(settings.timeout_spin.value() - window.config.printer.timeout_seconds) < 0.01,
          f"默认打印超时：{settings.timeout_spin.value():.0f} 秒")

    settings.sheets_spin.setValue(4)
    settings.flip_combo.setCurrentIndex(settings.flip_combo.findData("short_edge"))
    settings.backup_keep_spin.setValue(7)
    settings.backup_jobs_check.setChecked(True)
    settings.timeout_spin.setValue(90)
    changes = settings.changes()
    check(changes["paper"]["sheets_per_batch"] == 4, "改动：每批 4 张纸")
    check(changes["duplex"]["flip_direction"] == "short_edge", "改动：沿短边翻面")
    check(changes["backup"] == {"keep": 7, "include_jobs": True}, "改动：备份保留 7 份并含单页文件")
    check(abs(changes["printer"]["timeout_seconds"] - 90.0) < 0.01, "改动：超时 90 秒")

    settings.printer_combo.setEditText("")
    settings._on_save()
    check(settings.saved_config is None, "打印机名为空时拒绝保存")
    check("打印机" in settings.info_label.text(), f"提示：{settings.info_label.text()}")
    check(not config_path.exists(), "拒绝保存时不会写配置文件")

    settings.printer_combo.setEditText(window.config.printer_name)
    settings.sumatra_edit.setText("")
    settings._on_save()
    check(settings.saved_config is not None, "填好打印机名后可以保存")
    check(config_path.exists(), f"配置文件已写出：{config_path.name}")
    saved = load_config(config_path)
    check(saved.sheets_per_batch == 4, f"配置文件里每批 {saved.sheets_per_batch} 张纸")
    check(saved.flip_direction == "short_edge", f"配置文件里翻面方向 {saved.flip_direction}")
    check(saved.backup.keep == 7 and saved.backup.include_jobs, "配置文件里备份设置正确")

    # ---------------------------------------------------------------
    print("\n【检查 5】保存设置后主窗口立刻按新配置刷新")
    # ---------------------------------------------------------------
    # 设置窗口只保存打印机、纸张、翻面方向与备份四项，不含数据库路径；
    # 验收时把路径换回本次测试的临时目录，避免动到正式任务库。
    applied = replace(
        settings.saved_config,
        database_file=window.config.database_file,
        jobs_dir=window.config.jobs_dir,
        backup_dir=workdir / "backups",
        archive_root=workdir / "archive",
    )
    window._apply_config(applied)
    app.processEvents()
    check(window.config.sheets_per_batch == 4, "主窗口使用新的每批张数")
    check(window.config.flip_direction == "short_edge", "主窗口使用新的翻面方向")
    status_text = window.statusBar().currentMessage()
    check("每批 4 张纸" in status_text, f"状态栏已更新：{status_text[-40:]}")
    check("短边" in status_text, "状态栏翻面方向变成沿短边")
    check("每批 4 张纸" in window.hint_label.text(), "底部提示已更新")
    check(window.duplex.config.sheets_per_batch == 4, "双面服务已按新配置重建")

    install_fake_duplex_environment(window)   # 设置改了配置，重新装回假打印环境
    dialog_d2 = open_dialog(window, task_diagram)
    check("短边" in dialog_d2.flip_hint_label.text(),
          f"短边模式的翻面说明：{dialog_d2.flip_hint_label.text()[:30]}…")
    check("上下翻" in dialog_d2.flip_hint_label.text(), "短边模式说明是上下翻")
    check(dialog_d2.snapshot.phase == PHASE_WAITING_FLIP
          and "张纸" in dialog_d2.diagram.description,
          f"重新打开后仍在等待翻面（{dialog_d2.snapshot.phase}）："
          f"{dialog_d2.diagram.description}")
    check("上下翻" in dialog_d2.diagram_note_label.text(),
          f"等待翻面时按短边提示：{dialog_d2.diagram_note_label.text()[:36]}…")

    # ---------------------------------------------------------------
    print("\n【检查 6】打印进度列：进行中 / 待补打 / 完成")
    # ---------------------------------------------------------------
    window._refresh_tasks()
    app.processEvents()
    row10 = row_of(window, "阶段八界面-十页")
    progress = cell(window, row10, 8)
    check("已完成" in progress and "张纸" in progress, f"进行中任务进度：{progress}")
    check("待补打 1 张" in progress, f"有作废纸张时提示待补打：{progress}")

    task_main = window.repository.get_task("SP8GUI-4")
    assert task_main is not None
    dialog_m = open_dialog(window, task_main)
    dialog_m.start()
    app.processEvents()
    submit_and_confirm(dialog_m)              # 第 1 张正面（第 1 页）
    dialog_m.submit_current()                 # 第 2 张正面（第 3 页）
    app.processEvents()
    dialog_m.report_anomaly("模拟卡纸：主流程")
    app.processEvents()
    check(dialog_m.snapshot.phase == PHASE_WAITING_FLIP,
          f"第 2 张作废后等待翻面：{dialog_m.snapshot.phase}")
    window._refresh_tasks()
    app.processEvents()
    progress = cell(window, row_of(window, "阶段八界面-四页"), 8)
    check("已完成 0 / 2 张纸" in progress, f"作废后纸张进度：{progress}")
    check("待补打 1 张" in progress, f"作废后提示待补打：{progress}")

    dialog_m.confirm_flip()                   # 翻面，开始打背面
    app.processEvents()
    check(dialog_m.snapshot.phase == PHASE_BACK, f"翻面后进入打印背面：{dialog_m.snapshot.phase}")
    submit_and_confirm(dialog_m)              # 第 1 张背面（第 2 页）
    check(dialog_m.snapshot.current_round == 1,
          f"正常循环结束后进入第 {dialog_m.snapshot.current_round} 轮补打")
    check(dialog_m.snapshot.phase == PHASE_FRONT, "补打轮从正面开始")
    check(dialog_m.snapshot.current.page_number == 3,
          f"补打的是第 {dialog_m.snapshot.current.page_number} 页")
    check("第 1 轮补打" in dialog_m.round_label.text(),
          f"当前轮次：{dialog_m.round_label.text()}")
    check(dialog_m.task_status_label.text() == TaskStatus.REPRINT.label,
          f"补打轮任务状态：{dialog_m.task_status_label.text()}")
    window._refresh_tasks()
    app.processEvents()
    progress = cell(window, row_of(window, "阶段八界面-四页"), 8)
    check("第 1 轮补打" in progress, f"补打轮进度列：{progress}")
    check("已完成 1 / 2 张纸" in progress, f"补打轮纸张进度：{progress}")

    submit_and_confirm(dialog_m)              # 补打：第 2 张正面（第 3 页）
    check(dialog_m.snapshot.phase == PHASE_WAITING_FLIP,
          f"补打轮正面确认后等待翻面：{dialog_m.snapshot.phase}")
    note = dialog_m.diagram_note_label.text()
    check("第 1 轮补打" in note and "第 2 张纸" in note,
          f"补打轮只要求翻这一张纸：{note[:44]}…")
    check("本批纸张" not in note, "补打轮不再提示「本批纸张」")
    dialog_m.confirm_flip()                   # 补打轮只翻这一张纸
    app.processEvents()
    check(dialog_m.snapshot.phase == PHASE_BACK,
          f"补打轮翻面后打背面：{dialog_m.snapshot.phase}")
    submit_and_confirm(dialog_m)              # 补打：第 2 张背面（第 4 页）
    check(dialog_m.snapshot.phase == PHASE_COMPLETED,
          f"补打完成后阶段：{dialog_m.snapshot.phase}")
    check(dialog_m.snapshot.confirmed_count == 4,
          f"四个面全部确认：{dialog_m.snapshot.confirmed_count}")
    window._refresh_tasks()
    app.processEvents()
    progress = cell(window, row_of(window, "阶段八界面-四页"), 8)
    check(progress == "已完成", f"完成后进度列：{progress}")

    # ---------------------------------------------------------------
    print("\n【检查 7】取消任务后的进度列")
    # ---------------------------------------------------------------
    task_cancel = window.repository.get_task("SP8GUI-2")
    assert task_cancel is not None
    dialog_c = open_dialog(window, task_cancel)
    dialog_c.start()
    app.processEvents()
    dialog_c.submit_current()
    app.processEvents()
    dialog_c.cancel_task()
    app.processEvents()
    check(dialog_c.snapshot.phase == PHASE_CANCELLED, f"取消后阶段：{dialog_c.snapshot.phase}")
    window._refresh_tasks()
    app.processEvents()
    progress = cell(window, row_of(window, "阶段八界面-两页"), 8)
    check(progress == "已取消", f"取消后进度列：{progress}")

    # ---------------------------------------------------------------
    print("\n【检查 8】运行日志窗口：读取、级别过滤、关键字过滤")
    # ---------------------------------------------------------------
    log_path = workdir / "logs" / "smartprint.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "\n".join(
            [
                "2026-11-06 20:00:00 [INFO] smartprint.intake: 接收任务 SP1",
                "2026-11-06 20:00:01 [INFO] smartprint.print: 提交 page_001.pdf",
                "2026-11-06 20:00:02 [WARNING] smartprint.print: 打印队列非空",
                "2026-11-06 20:00:03 [ERROR] smartprint.print: 打印失败",
                "2026-11-06 20:00:04 [INFO] smartprint.duplex: 用户确认第 1 张正面",
                "2026-11-06 20:00:05 [INFO] smartprint.intake: 接收任务 SP2",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    log_dialog = LogViewDialog(None, log_path=log_path)
    log_dialog.show_errors = False
    log_dialog.reload()
    check(len(log_dialog.lines) == 6, f"读到 {len(log_dialog.lines)} 行日志")
    check(len(log_dialog.visible_lines) == 6, "默认显示全部行")
    check("共 6 行" in log_dialog.info_label.text(), f"信息栏：{log_dialog.info_label.text()}")
    check(log_dialog.auto_check.isChecked(), "自动刷新默认勾选")
    check(len(LEVEL_CHOICES) == 4, f"级别下拉 {len(LEVEL_CHOICES)} 项")

    log_dialog.level_combo.setCurrentIndex(1)      # 仅错误
    log_dialog.apply_filter()
    check(len(log_dialog.visible_lines) == 1, f"仅错误：{len(log_dialog.visible_lines)} 行")
    log_dialog.level_combo.setCurrentIndex(2)      # 仅警告及以上
    log_dialog.apply_filter()
    check(len(log_dialog.visible_lines) == 2, f"警告及以上：{len(log_dialog.visible_lines)} 行")
    log_dialog.level_combo.setCurrentIndex(0)
    log_dialog.keyword_edit.setText("SP2")
    log_dialog.apply_filter()
    check(len(log_dialog.visible_lines) == 1, "关键字过滤命中 1 行")
    check("SP2" in log_dialog.visible_lines[0], f"命中的行：{log_dialog.visible_lines[0][:40]}…")
    log_dialog.tail_spin.setValue(100)
    log_dialog.reload()
    check(len(log_dialog.lines) >= 1, "读取行数改小后仍能读")

    # ---------------------------------------------------------------
    print("\n【检查 9】异常记录总览")
    # ---------------------------------------------------------------
    opened: list[str] = []
    anomaly_dialog = AnomalyDialog(
        window.repository, None, open_duplex=lambda uid: opened.append(uid)
    )
    anomaly_dialog.show_errors = False
    check(anomaly_dialog.table.columnCount() == len(ANOMALY_COLUMNS),
          f"异常表格 {anomaly_dialog.table.columnCount()} 列")
    check(len(anomaly_dialog.rows) == 2,
          f"库里共 {len(anomaly_dialog.rows)} 条异常记录（十页一条待补打 + 四页一条已补打）")
    check(anomaly_dialog.unresolved_check.isChecked(), "默认只看待补打")
    visible_rows = anomaly_dialog.table.rowCount()
    check(visible_rows == 1, f"待补打的记录 {visible_rows} 条")
    check(anomaly_dialog.table.item(0, 9).text() == "待补打",
          f"状态列：{anomaly_dialog.table.item(0, 9).text()}")
    check("张纸" not in anomaly_dialog.table.item(0, 4).text(), "纸张列只显示编号")
    check(anomaly_dialog.table.item(0, 5).text() in ("正面", "背面"),
          f"异常面列：{anomaly_dialog.table.item(0, 5).text()}")

    anomaly_dialog.unresolved_check.setChecked(False)
    anomaly_dialog.apply_filter()
    check(anomaly_dialog.table.rowCount() == 2,
          f"取消过滤后显示全部 {anomaly_dialog.table.rowCount()} 条")
    statuses = {anomaly_dialog.table.item(row, 9).text()
                for row in range(anomaly_dialog.table.rowCount())}
    check("已补打完成" in statuses, f"状态列包含已补打完成：{statuses}")

    anomaly_dialog.table.selectRow(0)
    app.processEvents()
    detail = anomaly_dialog.detail_view.toPlainText()
    check("任务号" in detail and "原因" in detail, "选中后详情区显示任务号与原因")
    check(anomaly_dialog.open_button.isEnabled(), "选中后可以跳回双面打印窗口")
    anomaly_dialog._on_open_duplex()
    check(len(opened) == 1, f"跳转回调被调用：{opened}")
    check(anomaly_dialog.keyword_edit.text() == "", "关键字框默认为空")
    anomaly_dialog.keyword_edit.setText("不存在的原因")
    anomaly_dialog.apply_filter()
    check(anomaly_dialog.table.rowCount() == 0, "关键字无命中时表格为空")

    # ---------------------------------------------------------------
    print("\n【检查 10】备份与归档接到主窗口上")
    # ---------------------------------------------------------------
    check(window.backup_service.backup_dir == window.config.backup_dir,
          f"备份目录：{window.backup_service.backup_dir}")
    check(window.backup_service.keep == window.config.backup.keep,
          f"备份保留 {window.backup_service.keep} 份")
    entry = window.backup_service.create(label="gui")
    check(entry.path.exists(), f"备份文件已生成：{entry.path.name}")
    check(entry.task_count == len(DOCUMENTS), f"备份里 {entry.task_count} 个任务")
    check(len(window.backup_service.list_backups()) >= 1, "能列出备份")
    check(window.archive_service.archive_root == window.config.archive_root,
          f"归档目录：{window.archive_service.archive_root}")

    done = window.repository.get_task("SP8GUI-4")
    check(done is not None and window.archive_service.can_archive(done),
          "已完成任务可以归档")
    result = window.archive_service.archive_tasks([done], include_pages=False)
    check(result.ok, f"归档结果：{result.summary_text}")
    check(len(window.archive_service.list_archives()) == 1, "归档列表里有 1 个任务")
    check(window.repository.count_duplex_faces("SP8GUI-4") > 0,
          "归档后数据库里的打印面记录仍在（归档只是复制文件）")

    # ---------------------------------------------------------------
    print("\n【检查 11】关于文案与降级场景")
    # ---------------------------------------------------------------
    about = window._about_text()
    check("阶段八" in about, "关于里写明阶段八")
    check(window.config.printer_name not in ("", None), f"当前打印机：{window.config.printer_name}")
    check("纸张示意图" in about and "备份" in about, "关于里列出阶段八功能")
    empty_task = window.repository.get_task("SP8GUI-8")
    check(window._task_progress_text(empty_task) == "—", "没有双面会话的任务进度是「—」")
    window._on_header_clicked(8)
    check(window._sort_key in ("created_at", "status", "task_uid"),
          f"点击不可排序的进度列表头不改变排序键：{window._sort_key}")


def main() -> int:
    stamp = int(time.time())
    workdir = PROJECT_ROOT / "Data" / "jobs" / f"_verify_stage8_gui_{stamp}"
    sources = workdir / "sources"
    jobs_dir = workdir / "jobs"
    config_dir = workdir / "config"
    db_path = PROJECT_ROOT / "Data" / "database" / f"_verify_stage8_gui_{stamp}.db"
    for path in (sources, jobs_dir, config_dir, workdir / "backups", workdir / "archive"):
        path.mkdir(parents=True, exist_ok=True)

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
            incoming_dir=workdir / "incoming",
            sheets_per_batch=5,
            backup_dir=workdir / "backups",
            archive_root=workdir / "archive",
        )
    )
    print(f"工作目录：{workdir}")
    print("SmartPrint 阶段八界面验收（界面完善与打包）")

    try:
        for name, pages in DOCUMENTS.items():
            register_task(
                window.repository,
                make_pdf(sources / name, pages),
                f"SP8GUI-{pages}",
                pages=pages,
            )
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
    print(f"阶段八界面验收：{passed}/{total} 项通过")
    if passed != total:
        print("未通过的项目：")
        for ok, description in results:
            if not ok:
                print(f"  - {description}")
    print("=" * 60)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
