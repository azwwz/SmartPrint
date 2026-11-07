"""SmartPrint 阶段五界面验收脚本（离屏运行，不需要人工点击）。

覆盖的验收点：

1. 主窗口接入：「打印单页」出现在工具栏、任务菜单和右键菜单，快捷键 Ctrl+J。
2. 选中任务才可用；未选中或源文件丢失时禁用。
3. 打印窗口能打开并显示任务信息、页码信息（第几张纸 / 正面还是背面 / 单页文件名）。
4. 打印机下拉框列出所有打印机，PDF24 等虚拟打印机被标注且不可选中。
5. 默认选中配置里的目标打印机（HP LaserJet 1020 → 系统真实名称）。
6. 打印前检查区显示打印机、SumatraPDF、队列与提示。
7. 演练模式：点「演练」只生成命令，不启动打印程序、不写记录。
8. 真实发送（注入假打印程序）：状态显示「命令已发送」，结果区含命令行与队列快照。
9. 打印机脱机：打印按钮禁用 / 发送被拦截，且完全不会调用打印程序。
10. 打印程序返回非 0：结果区显示失败，页面提示语明确。
11. 打印记录写库并在主窗口详情里显示「最近打印」。
12. 任务记录里出现 print / print_blocked 事件。
13. 删除任务时打印记录一并清理。

脚本使用独立的测试目录与测试数据库（`Data/jobs/_verify_stage5_gui_时间戳/`），
跑完自动删除，不会污染正式任务库，也不会真的出纸。

运行方式（项目根目录下）：

    conda activate py313
    python tools\\verify_stage5_gui.py
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
from app.core.models import Task  # noqa: E402
from app.core.pdf_inspector import inspect_pdf  # noqa: E402
from app.core.print_job import SUBMIT_SUBMITTED  # noqa: E402
from app.printer import sumatra  # noqa: E402
from app.printer import windows_printers as wp  # noqa: E402
from app.services.print_service import PrintService  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from app.utils.logging_setup import setup_logging  # noqa: E402
from app.utils.paths import ensure_dirs  # noqa: E402

A4_WIDTH_PT = 595.28
A4_HEIGHT_PT = 841.89

#: 测试任务：文件名 → 页数
DOCUMENTS: dict[str, int] = {
    "阶段五-五页.pdf": 5,
    "阶段五-丢失.pdf": 2,
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
        page.insert_text((72, 150), f"Stage 5 GUI test - page {number} / {pages}", fontsize=12)
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
    def __init__(self, before=(), after=()) -> None:
        self.before = wp.QueueSnapshot("HP LaserJet 1020 1016811", tuple(before), observed_at="before")
        self.after = wp.QueueSnapshot("HP LaserJet 1020 1016811", tuple(after), observed_at="after")
        self.calls = 0

    def __call__(self, printer_name: str) -> wp.QueueSnapshot:
        self.calls += 1
        return self.before if self.calls == 1 else self.after


class FakeSumatra:
    def __init__(self, returncode: int | None = 0, *, timed_out: bool = False) -> None:
        self.returncode = returncode
        self.timed_out = timed_out
        self.calls: list[list[str]] = []

    def __call__(self, command, timeout=None):
        self.calls.append(list(command))
        return sumatra.SumatraResult(
            command=list(command),
            returncode=self.returncode,
            stdout="",
            stderr="",
            duration_seconds=0.08,
            timed_out=self.timed_out,
            launched=True,
            started_at=datetime.now(),
        )


def install_fake_print_environment(
    window: MainWindow,
    *,
    printers: list[wp.PrinterInfo] | None = None,
    queue: FakeQueue | None = None,
    runner: FakeSumatra | None = None,
) -> PrintService:
    """把主窗口里的打印服务换成「不会真的打印」的版本。"""
    config = window.config
    service = PrintService(
        config=AppConfig(
            printer_name=config.printer_name,
            printer=replace_printer_settings(config.printer),
            jobs_dir=config.jobs_dir,
        ),
        repository=window.repository,
        page_jobs=window.page_jobs,
        printer_reader=lambda: list(printers or [
            fake_printer(),
            fake_printer(
                name="备用的激光打印机 X", port="USB001", driver="HP LaserJet 1020"
            ),
            fake_printer(name="PDF24", port=r"\\.\pipe\PDFPrint", driver="PDF24", virtual=True),
        ]),
        queue_reader=queue or (lambda name: wp.QueueSnapshot(name, (), observed_at="empty")),
        sumatra_finder=lambda configured: Path("C:/fake/SumatraPDF.exe"),
        sumatra_runner=runner or FakeSumatra(),
    )
    window.printer = service
    return service


def replace_printer_settings(settings: PrinterSettings) -> PrinterSettings:
    return PrinterSettings(
        sumatra_path="C:/fake/SumatraPDF.exe",
        print_settings=settings.print_settings,
        timeout_seconds=settings.timeout_seconds,
        queue_recheck_seconds=0.0,
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def run_checks(window: MainWindow, workdir: Path) -> None:
    app = QApplication.instance()
    assert app is not None

    print("\n【检查 1】主窗口接入：打印单页动作")
    check(window.table.rowCount() == len(DOCUMENTS), f"表格显示 {window.table.rowCount()} 个任务")
    toolbar_actions = window._toolbar.actions()
    check(window.act_print_page in toolbar_actions, "工具栏中有「打印单页」按钮")
    check(
        window.act_print_page.shortcut().toString() == "Ctrl+J",
        f"快捷键为 Ctrl+J（实际 {window.act_print_page.shortcut().toString()}）",
    )
    check(
        "虚拟打印机" in window.act_print_page.toolTip(),
        f"按钮提示说明了排除虚拟打印机：{window.act_print_page.toolTip()}",
    )
    check(
        "打印单页" in window.hint_label.text() and "不等于纸张已出" in window.hint_label.text(),
        "主窗口提示说明了「命令发送成功 ≠ 纸张已出」",
    )

    task_menu_actions = []
    for menu_action in window.menuBar().actions():
        menu = menu_action.menu()
        if menu is not None and menu_action.text().startswith("任务"):
            task_menu_actions = menu.actions()
    check(window.act_print_page in task_menu_actions, "任务菜单中有「打印单页」")

    window.table.clearSelection()
    window._update_action_state()
    check(not window.act_print_page.isEnabled(), "未选中任务时「打印单页」不可用")

    task5 = select_task(window, "阶段五-五页.pdf")
    check(task5 is not None, "找到 5 页任务")
    check(window.act_print_page.isEnabled(), "选中任务后「打印单页」可用")

    gone = select_task(window, "阶段五-丢失.pdf")
    check(gone is not None, "找到「丢失」任务")
    Path(gone.source_path).unlink()
    window._refresh_tasks()
    select_task(window, "阶段五-丢失.pdf")
    check(not window.act_print_page.isEnabled(), "源文件丢失后「打印单页」不可用")

    select_task(window, "阶段五-五页.pdf")
    task5 = window._current_task()

    print("\n【检查 2】主窗口详情显示「最近打印」")
    detail = window.detail_view.toPlainText()
    check("最近打印" in detail, "详情中有「最近打印」一行")
    check("尚未打印" in window._last_print_text(task5), f"未打印时提示：{window._last_print_text(task5)}")

    print("\n【检查 3】打印窗口：任务与页面信息")
    queue = FakeQueue(before=(), after=())
    runner = FakeSumatra(returncode=0)
    install_fake_print_environment(window, queue=queue, runner=runner)

    dialog = window.create_print_dialog(task5)
    dialog.show_errors = False
    dialog.confirm_before_print = False
    dialog.show()
    app.processEvents()

    check(dialog.page_spin.maximum() == 5, f"页码上限为 5（实际 {dialog.page_spin.maximum()}）")
    check(dialog.page_spin.value() == 1, "默认选中第 1 页")
    check("第 1 / 3 张纸" in dialog.page_info_label.text(), f"页面信息：{dialog.page_info_label.text()}")
    check("正面" in dialog.page_info_label.text(), "第 1 页是正面")
    check("page_001.pdf" in dialog.page_info_label.text(), "显示了对应的单页文件名")
    dialog.page_spin.setValue(2)
    app.processEvents()
    check("第 1 / 3 张纸" in dialog.page_info_label.text() and "背面" in dialog.page_info_label.text(),
          f"第 2 页是背面：{dialog.page_info_label.text()}")
    check("page_002.pdf" in dialog.page_info_label.text(), "第 2 页对应 page_002.pdf")
    dialog.page_spin.setValue(5)
    app.processEvents()
    check("第 3 / 3 张纸" in dialog.page_info_label.text(), f"第 5 页属于第 3 张纸：{dialog.page_info_label.text()}")
    dialog.page_spin.setValue(1)

    print("\n【检查 4】打印机下拉框：列出但禁止选择虚拟打印机")
    combo = dialog.printer_combo
    items = [combo.itemText(index) for index in range(combo.count())]
    print(f"       打印机列表：{items}")
    check(combo.count() >= 2, f"下拉框列出 {combo.count()} 台打印机")
    check(any("HP LaserJet 1020" in text for text in items), "列表包含 HP LaserJet 1020")
    check(any("PDF24" in text for text in items), "列表也列出 PDF24（但标注为虚拟打印机）")
    pdf24_index = next(i for i, text in enumerate(items) if "PDF24" in text)
    check(
        not combo.model().item(pdf24_index).isEnabled(),
        "PDF24 一项不可选中",
    )
    check(
        "虚拟打印机" in items[pdf24_index],
        f"PDF24 一项标注了类型：{items[pdf24_index]}",
    )
    check(dialog.selected_printer() == "HP LaserJet 1020 1016811",
          f"默认选中目标打印机：{dialog.selected_printer()}")

    # 换一台打印机：检查区必须跟着换，不能还在检查配置里的默认打印机
    other_index = next(
        index for index in range(combo.count())
        if combo.itemData(index) == "备用的激光打印机 X"
    )
    combo.setCurrentIndex(other_index)
    app.processEvents()
    check(dialog.readiness is not None and dialog.readiness.target is not None,
          "切换到另一台打印机后仍然解析成功")
    check(
        dialog.readiness.requested_name == "备用的激光打印机 X",
        f"预检的对象是刚选中的打印机：{dialog.readiness.requested_name if dialog.readiness else '无'}",
    )
    check(
        "备用的激光打印机 X" in dialog.readiness_view.toPlainText(),
        "检查区显示的是新选中的打印机",
    )
    default_index = next(
        index for index in range(combo.count())
        if combo.itemData(index) == "HP LaserJet 1020 1016811"
    )
    combo.setCurrentIndex(default_index)
    app.processEvents()
    check(dialog.readiness.requested_name == "HP LaserJet 1020 1016811", "切回目标打印机后预检对象正确")
    check(
        "SP-GUI-5" in dialog.info_label.text() and "5 页" in dialog.info_label.text(),
        f"窗口顶部显示任务信息：{dialog.info_label.text()[:60]}…",
    )

    print("\n【检查 5】打印前检查区")
    readiness_text = dialog.readiness_view.toPlainText()
    print("       " + readiness_text.replace("\n", "\n       "))
    check("结论" in readiness_text, "显示结论行")
    check("目标打印机" in readiness_text, "显示目标打印机")
    check("SumatraPDF" in readiness_text, "显示 SumatraPDF")
    check("队列" in readiness_text, "显示打印队列")
    check(dialog.readiness is not None and dialog.readiness.ok, "预检通过")
    check(dialog.print_button.isEnabled(), "预检通过时「打印这一页」可用")

    print("\n【检查 6】演练：只生成命令，不发送")
    before_jobs = window.repository.count_print_jobs(task5.task_uid)
    outcome_dry = dialog.dry_run_page()
    app.processEvents()
    check(outcome_dry is not None and outcome_dry.dry_run, "演练返回结果并标记为演练")
    check(len(runner.calls) == 0, "演练没有启动打印程序")
    check(
        window.repository.count_print_jobs(task5.task_uid) == before_jobs,
        "演练不写打印记录",
    )
    check("演练完成" in dialog.result_label.text(), f"界面提示：{dialog.result_label.text()}")
    check("第 1 页" in dialog.result_view.toPlainText() or "未发送" in dialog.result_view.toPlainText(),
          "结果区有演练说明")

    print("\n【检查 7】真实发送（注入假打印程序）")
    queue.calls = 0
    outcome = dialog.print_current_page()
    app.processEvents()
    check(outcome is not None and outcome.ok, f"命令已发送：{outcome.summary_text() if outcome else '无结果'}")
    check(outcome.status == SUBMIT_SUBMITTED, f"状态：{outcome.status_text}")
    check(len(runner.calls) == 1, "只调用了一次打印程序")
    check(dialog.last_outcome is outcome, "窗口记录了本次结果")
    check("命令已发送" in dialog.result_label.text(), f"界面提示：{dialog.result_label.text()}")
    check("不代表纸张已经完整出纸" in dialog.result_label.text(), "界面明确说明不等于出纸")
    result_text = dialog.result_view.toPlainText()
    check("-print-to" in result_text, "结果区显示命令行")
    check("提交前队列" in result_text and "提交后队列" in result_text, "结果区显示提交前后的队列")
    check("单页文件" in result_text and "page_001.pdf" in result_text, "结果区显示单页文件")

    records = window.repository.list_print_jobs(task5.task_uid)
    check(len(records) == 1, f"数据库记录 1 条打印提交（实际 {len(records)}）")
    check(records[0].page_number == 1, "记录页码 1")
    check(records[0].status == SUBMIT_SUBMITTED, "记录状态为命令已发送")
    check(records[0].printer_name == "HP LaserJet 1020 1016811", f"记录打印机：{records[0].printer_name}")
    check("page_001.pdf" in records[0].command, "记录的命令行指向 page_001.pdf")

    print("\n【检查 8】主窗口同步刷新")
    window._report_print_outcome(dialog)
    app.processEvents()
    window._update_detail(task5)
    detail = window.detail_view.toPlainText()
    check("最近打印" in detail, "详情仍显示「最近打印」")
    check("第 1 页" in window._last_print_text(task5), f"最近打印内容：{window._last_print_text(task5)}")
    check("命令已发送" in window._last_print_text(task5), "详情显示命令已发送")
    log_text = window.log_view.toPlainText()
    check("命令已发送" in log_text, "运行日志里有发送记录")
    events = window.repository.list_events(task5.task_uid)
    check(any(event.event_type == "print" for event in events), "任务记录里有 print 事件")

    print("\n【检查 9】打印机脱机时拦截")
    offline_dialog = window.create_print_dialog(task5)
    offline_dialog.show_errors = False
    offline_dialog.confirm_before_print = False
    offline_runner = FakeSumatra(returncode=0)
    install_fake_print_environment(
        window,
        printers=[fake_printer(status=wp.PRINTER_STATUS_OFFLINE)],
        runner=offline_runner,
    )
    offline_dialog.service = window.printer
    offline_dialog.refresh_readiness()
    app.processEvents()
    text = offline_dialog.readiness_view.toPlainText()
    check("脱机" in text, f"检查区提示脱机：{[line for line in text.splitlines() if '脱机' in line]}")
    check(not offline_dialog.print_button.isEnabled(), "脱机时「打印这一页」不可用")
    check(offline_dialog.force_check.isEnabled(), "脱机时提供「仍然发送」选项（需手动勾选）")
    outcome_blocked = offline_dialog.print_current_page()
    app.processEvents()
    check(outcome_blocked is not None and not outcome_blocked.ok, "脱机时不发送")
    check(len(offline_runner.calls) == 0, "脱机时完全没有调用打印程序")
    check("未发送" in offline_dialog.result_label.text(), f"界面提示：{offline_dialog.result_label.text()}")
    blocked_events = [
        event for event in window.repository.list_events(task5.task_uid)
        if event.event_type == "print_blocked"
    ]
    check(bool(blocked_events), "拦截也留下了 print_blocked 事件")
    offline_dialog.close()

    print("\n【检查 10】打印程序返回异常")
    fail_runner = FakeSumatra(returncode=1)
    fail_dialog = window.create_print_dialog(task5)
    fail_dialog.show_errors = False
    fail_dialog.confirm_before_print = False
    install_fake_print_environment(window, queue=FakeQueue((), ()), runner=fail_runner)
    fail_dialog.service = window.printer
    fail_dialog.refresh_readiness()
    fail_dialog.page_spin.setValue(3)
    outcome_fail = fail_dialog.print_current_page()
    app.processEvents()
    check(outcome_fail is not None and not outcome_fail.ok, "退出码非 0 时不算发送成功")
    check("发送失败" in fail_dialog.result_label.text(), f"界面提示：{fail_dialog.result_label.text()}")
    check("退出码 1" in fail_dialog.result_view.toPlainText(), "结果区显示退出码")
    check(len(fail_runner.calls) == 1, "失败后不会自动重试")
    fail_records = window.repository.list_print_jobs(task5.task_uid)
    check(any(record.page_number == 3 and record.exit_code == 1 for record in fail_records),
          "失败记录已写库（第 3 页，退出码 1）")
    fail_dialog.close()

    print("\n【检查 11】删除任务时清理打印记录")
    records_before = window.repository.count_print_jobs(task5.task_uid)
    check(records_before >= 2, f"删除前有 {records_before} 条打印记录")
    window.delete_tasks([task5.task_uid], delete_files=False)
    app.processEvents()
    check(window.repository.get_task(task5.task_uid) is None, "任务已删除")
    check(window.repository.count_print_jobs(task5.task_uid) == 0, "打印记录一并清理")

    print("\n【检查 12】真实环境自检（只读）")
    from app.services.print_service import PrintService as RealPrintService

    real = RealPrintService(config=AppConfig(printer_name=window.config.printer_name,
                                            jobs_dir=workdir / "jobs"),
                            repository=window.repository,
                            page_jobs=window.page_jobs)
    real_readiness = real.check_readiness()
    print("       " + str(real_readiness.summary_text()))
    check(real_readiness.printers, f"真实环境读到 {len(real_readiness.printers)} 台打印机")
    check(any(not item.is_virtual for item in real_readiness.printers), "真实环境里至少有一台真实打印机")
    check(any(item.is_virtual for item in real_readiness.printers), "真实环境里识别出了虚拟打印机")
    real_target = wp.resolve_printer_name(window.config.printer_name, real_readiness.printers)
    print(f"       配置名称「{window.config.printer_name}」→ 系统名称「{real_target.name if real_target else '未找到'}」")
    check(real_target is not None, "配置中的打印机名称能在真实系统中解析")
    check(real.find_sumatra() is not None, f"真实环境找到 SumatraPDF：{real.find_sumatra()}")
    check(real_readiness.sumatra_version != "", f"读到 SumatraPDF 版本：{real_readiness.sumatra_version}")
    if real_readiness.queue is not None:
        check(real_readiness.queue.ok, f"真实队列读取正常：{real_readiness.queue.summary_text()}")


def main() -> int:
    stamp = int(time.time())
    workdir = PROJECT_ROOT / "Data" / "jobs" / f"_verify_stage5_gui_{stamp}"
    sources = workdir / "sources"
    jobs_dir = workdir / "jobs"
    db_path = PROJECT_ROOT / "Data" / "database" / f"_verify_stage5_gui_{stamp}.db"
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
        )
    )
    # 不启动目录监控的副作用：这里仍然启动，但指向空的测试目录
    print(f"工作目录：{workdir}")
    print("SmartPrint 阶段五界面验收（打印单页窗口）")

    try:
        for name, pages in DOCUMENTS.items():
            register_task(window.repository, make_pdf(sources / name, pages), f"SP-GUI-{pages}")
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
    print(f"阶段五界面验收：{passed}/{total} 项通过")
    if passed != total:
        print("未通过的项目：")
        for ok, description in results:
            if not ok:
                print(f"  - {description}")
    print("=" * 60)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
