"""SmartPrint 阶段四界面验收脚本（离屏运行，不需要人工点击）。

覆盖的验收点：

1. 主窗口接入：工具栏 / 任务菜单都有「页面拆分」，选中任务才可用。
2. 任务详情显示「打印批次」，未生成时提示可用「页面拆分」生成单页文件。
3. 页面拆分窗口：三个页签（批次拆分 / 打印顺序 / 页面检查）、表头、最后一批标记。
4. 每批纸张数可调，批次随之重算。
5. 打印顺序页签：一次提交一行，每行对应一个独立的单页文件。
6. 点「生成单页文件」真的写出 page_001.pdf…，并写入数据库。
7. 生成后主窗口详情与批次状态同步刷新。
8. 单页任务：只有 page_001.pdf，没有背面文件，也不会凭空多生成一页。
9. 空白页任务：窗口给出提示，页面表标记空白页。
10. 源文件被换掉（页数变化）后重新生成：旧的单页文件被清理。
11. 源文件丢失：窗口报错、生成按钮禁用、主窗口动作禁用。
12. 删除任务时一并清理生成的单页文件与批次记录。
13. 重开窗口后批次信息仍在。

脚本使用独立的测试目录与测试数据库（`Data/jobs/_verify_stage4_gui_时间戳/`），
跑完自动删除，不会污染正式任务库。

运行方式（项目根目录下）：

    conda activate py313
    python tools\\verify_stage4_gui.py
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
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core.config import AppConfig, load_config  # noqa: E402
from app.core.models import Task  # noqa: E402
from app.core.page_planner import page_file_name  # noqa: E402
from app.core.pdf_inspector import inspect_pdf  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from app.ui.page_plan_dialog import (  # noqa: E402
    BATCH_COLUMNS,
    LAST_BATCH_COLOR,
    PAGE_COLUMNS,
    STEP_COLUMNS,
)
from app.utils.logging_setup import setup_logging  # noqa: E402
from app.utils.paths import ensure_dirs  # noqa: E402

A4_WIDTH_PT = 595.28
A4_HEIGHT_PT = 841.89

#: 测试任务：文件名 → (页数, 空白页)
DOCUMENTS: dict[str, tuple[int, tuple[int, ...]]] = {
    "阶段四-三十七页.pdf": (37, ()),
    "阶段四-十一页.pdf": (11, ()),
    "阶段四-单页.pdf": (1, ()),
    "阶段四-空白页.pdf": (6, (3, 5)),
    "阶段四-丢失.pdf": (2, ()),
}

results: list[tuple[bool, str]] = []


def check(condition: bool, description: str) -> bool:
    results.append((bool(condition), description))
    print(f"  [{'PASS' if condition else 'FAIL'}] {description}")
    return bool(condition)


def make_pdf(path: Path, pages: int, blank_pages: tuple[int, ...] = ()) -> Path:
    document = pymupdf.open()
    for index in range(pages):
        number = index + 1
        page = document.new_page(width=A4_WIDTH_PT, height=A4_HEIGHT_PT)
        if number in blank_pages:
            continue
        page.insert_text((72, 100), f"PAGE-{number}", fontsize=28)
        page.insert_text((72, 150), f"Stage 4 GUI test - page {number} / {pages}", fontsize=12)
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


def read_texts(path: Path) -> list[str]:
    """读出 PDF 每页开头的文字，用来确认单页文件装的是哪一页。"""
    texts: list[str] = []
    with pymupdf.open(path) as document:
        for index in range(document.page_count):
            raw = document.load_page(index).get_text("text")
            texts.append(next((line.strip() for line in raw.splitlines() if line.strip()), ""))
    return texts


def select_task(window: MainWindow, document_name: str) -> Task | None:
    """在表格里按文件名选中任务（表格显示的是不含扩展名的文档名）。"""
    wanted = Path(document_name).stem
    for row in range(window.table.rowCount()):
        if window.table.item(row, 1).text() == wanted:
            window.table.selectRow(row)
            QApplication.instance().processEvents()
            return window._tasks[row]
    return None


def job_files(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir())


def expected_page_files(pages: int) -> list[str]:
    return [page_file_name(number) for number in range(1, pages + 1)]


def run_checks(window: MainWindow, workdir: Path) -> None:
    app = QApplication.instance()
    assert app is not None

    print("\n【检查 1】主窗口接入：页面拆分动作")
    check(window.table.rowCount() == 5, f"表格显示 5 个任务（实际 {window.table.rowCount()}）")
    toolbar_actions = window._toolbar.actions()
    check(window.act_page_plan in toolbar_actions, "工具栏中有「页面拆分」按钮")
    check(
        window.act_page_plan.shortcut().toString() == "Ctrl+B",
        f"快捷键为 Ctrl+B（实际 {window.act_page_plan.shortcut().toString()}）",
    )
    check(
        "单页" in window.act_page_plan.toolTip(),
        f"按钮提示说明了用途：{window.act_page_plan.toolTip()}",
    )
    check(
        "page_001.pdf" in window.hint_label.text(),
        f"主窗口提示说明了单页文件：{window.hint_label.text()}",
    )
    window.table.clearSelection()
    window._update_action_state()
    check(not window.act_page_plan.isEnabled(), "未选中任务时「页面拆分」不可用")

    task37 = select_task(window, "阶段四-三十七页.pdf")
    check(task37 is not None, "找到 37 页任务")
    check(window.act_page_plan.isEnabled(), "选中任务后「页面拆分」可用")

    print("\n【检查 2】任务详情显示打印批次")
    detail = window.detail_view.toPlainText()
    check("打印批次" in detail, "详情中有「打印批次」一行")
    check("尚未生成" in detail, f"未生成时提示：{window._batches_text(task37)}")
    check("19 张" in detail and "4 批" in detail, "详情仍显示预计用纸 19 张 / 4 批")

    print("\n【检查 3】页面拆分窗口：37 页 → 4 批")
    dialog = window.create_page_plan_dialog(task37)
    dialog.show_errors = False
    dialog.show()
    app.processEvents()

    check(dialog.plan is not None, "窗口成功计算出拆分计划")
    check(dialog.plan.sheet_count == 19, f"19 张纸（实际 {dialog.plan.sheet_count}）")
    check(dialog.plan.batch_count == 4, f"4 批（实际 {dialog.plan.batch_count}）")
    check(dialog.tabs.count() == 3, f"窗口分三个页签（实际 {dialog.tabs.count()}）")
    check(
        [dialog.tabs.tabText(index) for index in range(3)] == ["批次拆分", "打印顺序", "页面检查"],
        f"页签名称：{[dialog.tabs.tabText(index) for index in range(3)]}",
    )
    check(
        [dialog.batch_table.horizontalHeaderItem(i).text() for i in range(len(BATCH_COLUMNS))]
        == list(BATCH_COLUMNS),
        f"批次表表头：{[dialog.batch_table.horizontalHeaderItem(i).text() for i in range(len(BATCH_COLUMNS))]}",
    )
    check(
        [dialog.step_table.horizontalHeaderItem(i).text() for i in range(len(STEP_COLUMNS))]
        == list(STEP_COLUMNS),
        f"打印顺序表表头：{[dialog.step_table.horizontalHeaderItem(i).text() for i in range(len(STEP_COLUMNS))]}",
    )
    check(
        [dialog.page_table.horizontalHeaderItem(i).text() for i in range(len(PAGE_COLUMNS))]
        == list(PAGE_COLUMNS),
        "页面检查表表头正确",
    )
    check(dialog.batch_table.rowCount() == 4, f"批次表 4 行（实际 {dialog.batch_table.rowCount()}）")

    row0 = [dialog.batch_table.item(0, column).text() for column in range(len(BATCH_COLUMNS))]
    check(row0[0] == "第 1 / 4 批", f"第 1 批标题：{row0[0]}")
    check(row0[1] == "5 张", f"第 1 批纸张数：{row0[1]}")
    check(row0[2] == "1、3、5、7、9", f"第 1 批正面页码：{row0[2]}")
    check(row0[3] == "10、8、6、4、2", f"第 1 批背面页码：{row0[3]}")
    check(row0[4] == "10 个", f"第 1 批对应 10 个单页文件：{row0[4]}")

    last_row = [dialog.batch_table.item(3, column).text() for column in range(len(BATCH_COLUMNS))]
    check(last_row[1] == "4 张", f"最后一批只有 4 张纸：{last_row[1]}")
    check(last_row[2] == "31、33、35、37", f"最后一批正面页码：{last_row[2]}")
    check(last_row[3] == "36、34、32", f"最后一批背面页码：{last_row[3]}")
    check(last_row[4] == "7 个", f"最后一批 7 个单页文件（4 正面 + 3 背面）：{last_row[4]}")
    check("最后一批" in last_row[5], f"最后一批有标记：{last_row[5]}")
    check(
        dialog.batch_table.item(3, 0).background().color() == LAST_BATCH_COLOR,
        "最后一批整行加底色",
    )
    check(
        "最后一批" not in dialog.batch_table.item(0, 5).text(),
        f"第 1 批没有「最后一批」标记：{dialog.batch_table.item(0, 5).text()}",
    )

    print("\n【检查 4】打印顺序页签：一次提交一行，一行一个单页文件")
    check(dialog.step_table.rowCount() == 37, f"打印顺序 37 行（实际 {dialog.step_table.rowCount()}）")
    first_three = [
        [dialog.step_table.item(row, column).text() for column in range(len(STEP_COLUMNS))]
        for row in range(3)
    ]
    check(
        [row[4] for row in first_three] == ["第 1 页", "第 3 页", "第 5 页"],
        f"前 3 次提交的页码：{[row[4] for row in first_three]}",
    )
    check(
        [row[5] for row in first_three] == ["page_001.pdf", "page_003.pdf", "page_005.pdf"],
        f"前 3 次提交的文件：{[row[5] for row in first_three]}",
    )
    check(
        [row[3] for row in first_three] == ["正面"] * 3,
        f"前 3 次都在正面：{[row[3] for row in first_three]}",
    )
    back_first = [
        dialog.step_table.item(row, column).text() for row in (5,) for column in range(len(STEP_COLUMNS))
    ]
    check(back_first[3] == "背面" and back_first[4] == "第 10 页",
          f"第 6 行开始提交背面，第一张是纸堆最上面那张纸的背面：{back_first[3]} {back_first[4]}")
    last_step = [
        dialog.step_table.item(36, column).text() for column in range(len(STEP_COLUMNS))
    ]
    check(last_step[3] == "背面" and last_step[4] == "第 32 页"
          and last_step[5] == "page_032.pdf",
          f"最后一批先正面后背面，最后一次提交是最后一批里编号最小的纸：{last_step[3]} {last_step[4]}")
    front37 = [
        dialog.step_table.item(33, column).text() for column in range(len(STEP_COLUMNS))
    ]
    check(front37[3] == "正面" and front37[4] == "第 37 页" and front37[5] == "page_037.pdf",
          f"第 37 页是最后一张纸的正面：{front37[3]} {front37[4]} {front37[5]}")
    step_files = {dialog.step_table.item(row, 5).text() for row in range(37)}
    check(len(step_files) == 37, f"37 次提交共用到 37 个不同文件（实际 {len(step_files)}）")

    print("\n【检查 5】页面检查页签")
    check(dialog.page_table.rowCount() == 37, f"页面表 37 行（实际 {dialog.page_table.rowCount()}）")
    check(
        dialog.page_table.item(0, 1).text().startswith("A4"),
        f"页面表显示尺寸：{dialog.page_table.item(0, 1).text()}",
    )
    check(
        "19 张纸" in dialog.plan_label.text() and "4 批" in dialog.plan_label.text(),
        f"计划摘要：{dialog.plan_label.text()}",
    )
    check("每次只提交一个页面" in dialog.summary_label.text(),
          f"底部说明强调逐页提交：{dialog.summary_label.text()}")
    check(dialog.generate_button.isEnabled(), "「生成单页文件」可用")
    check(dialog.generate_button.text().startswith("生成单页文件"),
          f"按钮文案：{dialog.generate_button.text()}")
    check(not dialog.open_dir_button.isEnabled(), "还没生成时「打开任务目录」不可用")

    print("\n【检查 6】调整每批纸张数")
    dialog.sheets_spin.setValue(3)
    app.processEvents()
    check(dialog.batch_table.rowCount() == 7, f"每批 3 张 → 7 批（实际 {dialog.batch_table.rowCount()}）")
    check(
        [dialog.batch_table.item(row, 1).text() for row in range(7)]
        == ["3 张"] * 6 + ["1 张"],
        f"各批张数：{[dialog.batch_table.item(row, 1).text() for row in range(7)]}",
    )
    check(dialog.step_table.rowCount() == 37, "每批张数只影响分组，提交次数仍是 37 次")
    dialog.sheets_spin.setValue(5)
    app.processEvents()
    check(dialog.batch_table.rowCount() == 4, "改回每批 5 张后恢复 4 批")

    print("\n【检查 7】生成单页文件")
    job_dir = window.page_jobs.job_dir_for(task37)
    check(dialog.generate() is True, "生成成功")
    app.processEvents()
    check(dialog.last_result is not None, "窗口记录了生成结果")
    check(dialog.open_dir_button.isEnabled(), "生成后「打开任务目录」可用")

    files = job_files(job_dir)
    check(
        files == sorted(expected_page_files(37) + ["original.pdf", "plan.json"]),
        f"生成的文件：{len(files)} 个（page_001…page_037 + original.pdf + plan.json）",
    )
    check("front_batch_001.pdf" not in files, "不再生成按批次的正面合并文件")
    check("back_batch_001.pdf" not in files, "不再生成按批次的背面合并文件")
    check("已生成 37 个单页文件" in dialog.summary_label.text(),
          f"成功提示：{dialog.summary_label.text()[:80]}…")
    check(str(job_dir) in dialog.summary_label.text(), f"提示中显示任务目录：{job_dir}")

    check(read_texts(job_dir / "page_001.pdf") == ["PAGE-1"], "page_001.pdf 装的是第 1 页")
    check(read_texts(job_dir / "page_010.pdf") == ["PAGE-10"], "page_010.pdf 装的是第 10 页")
    check(read_texts(job_dir / "page_037.pdf") == ["PAGE-37"], "page_037.pdf 装的是第 37 页")
    check(not (job_dir / "page_038.pdf").exists(), "不会为了凑成双面而生成第 38 页")

    batches = window.repository.list_batches(task37.task_uid)
    check(len(batches) == 4, f"数据库写入 4 条批次（实际 {len(batches)}）")
    check([batch.batch_index for batch in batches] == [1, 2, 3, 4], "批次序号 1~4")
    check(batches[0].front_pages == (1, 3, 5, 7, 9), f"第 1 批正面页码：{batches[0].front_pages}")
    check(batches[3].sheet_count == 4, "第 4 批 4 张纸")
    check(
        [path.name for path in batches[0].page_files][:5]
        == ["page_001.pdf", "page_003.pdf", "page_005.pdf", "page_007.pdf", "page_009.pdf"],
        f"第 1 批登记的单页文件：{[p.name for p in batches[0].page_files][:5]}",
    )
    check(
        [path.name for path in batches[3].page_files]
        == ["page_031.pdf", "page_033.pdf", "page_035.pdf", "page_037.pdf",
            "page_036.pdf", "page_034.pdf", "page_032.pdf"],
        f"最后一批登记的单页文件（背面按纸张编号降序）：{[p.name for p in batches[3].page_files]}",
    )
    check(all(batch.missing_files() == [] for batch in batches), "批次记录中的单页文件都存在")

    print("\n【检查 8】主窗口同步刷新")
    window._refresh_tasks()
    window._update_detail(task37)
    detail_after = window.detail_view.toPlainText()
    check(
        "已生成 4 批，19 张纸，37 个单页文件" in detail_after,
        f"详情中显示：{window._batches_text(task37)}",
    )
    check(
        any("pages" in line for line in window.event_view.toPlainText().splitlines()),
        "任务记录中出现生成事件",
    )

    print("\n【检查 9】单页任务：只有一个单页文件")
    task1 = select_task(window, "阶段四-单页.pdf")
    dialog1 = window.create_page_plan_dialog(task1)
    dialog1.show_errors = False
    dialog1.show()
    app.processEvents()
    check(dialog1.plan.batch_count == 1, "单页文件只有 1 批")
    check(dialog1.batch_table.item(0, 1).text() == "1 张", "只占 1 张纸")
    check(dialog1.batch_table.item(0, 2).text() == "1", "正面页码为 1")
    check(dialog1.batch_table.item(0, 3).text() == "无", "背面显示「无」")
    check(dialog1.batch_table.item(0, 4).text() == "1 个", "只需 1 个单页文件")
    check("只有正面" in dialog1.batch_table.item(0, 5).text(),
          f"说明列提示只有正面：{dialog1.batch_table.item(0, 5).text()}")
    check(not dialog1.plan.back_pages, "计划中没有背面页面")
    check(dialog1.generate() is True, "单页文件生成成功")
    app.processEvents()
    dir1 = window.page_jobs.job_dir_for(task1)
    files1 = job_files(dir1)
    check(files1 == ["original.pdf", "page_001.pdf", "plan.json"],
          f"单页任务只生成 page_001.pdf：{files1}")
    check(read_texts(dir1 / "page_001.pdf") == ["PAGE-1"], "page_001.pdf 内容正确")

    print("\n【检查 10】空白页提示")
    task_blank = select_task(window, "阶段四-空白页.pdf")
    dialog_blank = window.create_page_plan_dialog(task_blank)
    dialog_blank.show_errors = False
    dialog_blank.show()
    app.processEvents()
    check(dialog_blank.scan.blank_pages == (3, 5), f"识别空白页：{dialog_blank.scan.blank_pages}")
    check(dialog_blank.warning_label.isVisibleTo(dialog_blank), "空白页提示可见")
    check("空白" in dialog_blank.warning_label.text(),
          f"提示内容：{dialog_blank.warning_label.text()}")
    check(dialog_blank.page_table.item(2, 4).text() == "空白",
          f"页面表把第 3 页标为空白：{dialog_blank.page_table.item(2, 4).text()}")
    check(dialog_blank.page_table.item(0, 4).text() == "有内容",
          f"第 1 页标为有内容：{dialog_blank.page_table.item(0, 4).text()}")
    check(dialog_blank.plan.sheet_count == 3, "6 页含空白页仍算 3 张纸")
    check(dialog_blank.step_table.rowCount() == 6, "空白页同样占一个提交步骤（不跳过页码）")

    print("\n【检查 11】源文件页数变化后重新生成")
    task11 = select_task(window, "阶段四-十一页.pdf")
    dialog11 = window.create_page_plan_dialog(task11)
    dialog11.show_errors = False
    dialog11.show()
    app.processEvents()
    check(dialog11.plan.batch_count == 2, "11 页 → 2 批")
    check(dialog11.generate() is True, "第一次生成成功")
    dir11 = window.page_jobs.job_dir_for(task11)
    check(
        job_files(dir11) == sorted(expected_page_files(11) + ["original.pdf", "plan.json"]),
        f"11 页生成的文件：{job_files(dir11)}",
    )
    dialog11.close()

    make_pdf(task11.source_path, 3)  # 同一个文件换成 3 页
    dialog11b = window.create_page_plan_dialog(task11)
    dialog11b.show_errors = False
    dialog11b.show()
    app.processEvents()
    check(dialog11b.plan.page_count == 3, f"重新读取到 3 页（实际 {dialog11b.plan.page_count}）")
    check(dialog11b.plan.batch_count == 1, "3 页 → 1 批")
    check(dialog11b.generate() is True, "重新生成成功")
    app.processEvents()
    files11 = job_files(dir11)
    check(
        files11 == ["original.pdf", "page_001.pdf", "page_002.pdf", "page_003.pdf", "plan.json"],
        f"旧的 page_004…page_011 已清理：{files11}",
    )
    check(len(window.repository.list_batches(task11.task_uid)) == 1, "数据库批次记录变成 1 条")
    check(window.repository.get_task(task11.task_uid).page_count == 11,
          "任务表仍保留登记时的页数（界面以文件为准）")
    dialog11b.close()

    print("\n【检查 12】源文件丢失")
    missing = select_task(window, "阶段四-丢失.pdf")
    missing.source_path.unlink()
    window._refresh_tasks()
    missing = select_task(window, "阶段四-丢失.pdf")
    check(not window.act_page_plan.isEnabled(), "源文件丢失后「页面拆分」不可用")
    dialog_missing = window.create_page_plan_dialog(missing)
    dialog_missing.show_errors = False
    check(dialog_missing.error_message != "", f"窗口给出错误：{dialog_missing.error_message}")
    check(not dialog_missing.generate_button.isEnabled(), "出错时「生成单页文件」不可用")
    check(dialog_missing.batch_table.rowCount() == 0, "出错时批次表为空")
    check(dialog_missing.step_table.rowCount() == 0, "出错时打印顺序表为空")
    dialog_missing.close()

    print("\n【检查 13】删除任务时清理打印文件")
    task_delete = select_task(window, "阶段四-单页.pdf")
    check(dir1.is_dir(), "删除前任务目录存在")
    outcomes = window.delete_tasks([task_delete.task_uid])
    app.processEvents()
    check(outcomes[0].record_deleted, "任务记录已删除")
    check(not dir1.exists(), "生成的单页文件随任务一起清理")
    check(window.repository.list_batches(task_delete.task_uid) == [], "批次记录已删除")
    check(window.table.rowCount() == 4, f"表格剩余 4 个任务（实际 {window.table.rowCount()}）")

    for open_dialog in (dialog, dialog1, dialog_blank):
        open_dialog.close()

    print("\n【检查 14】重新打开窗口后批次信息仍在")
    reopened = MainWindow(window.config)
    reopened.show()
    app.processEvents()
    task37_reopened = select_task(reopened, "阶段四-三十七页.pdf")
    check(task37_reopened is not None, "重新打开后仍能找到 37 页任务")
    check(
        "已生成 4 批，19 张纸，37 个单页文件" in reopened.detail_view.toPlainText(),
        f"重新打开后仍显示批次：{reopened._batches_text(task37_reopened)}",
    )
    check(reopened.act_page_plan.isEnabled(), "重新打开后「页面拆分」仍可用")
    reopened.close()
    app.processEvents()


def main() -> int:
    ensure_dirs()
    setup_logging()

    workdir = PROJECT_ROOT / "Data" / "jobs" / f"_verify_stage4_gui_{time.strftime('%Y%m%d_%H%M%S')}"
    incoming = workdir / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    staged = workdir / "staged"
    staged.mkdir(parents=True, exist_ok=True)

    base = load_config()
    test_config: AppConfig = replace(
        base,
        incoming_dir=incoming,
        database_file=workdir / "stage4_gui.db",
        archive_dir=workdir / "archive",
        jobs_dir=workdir / "jobs",
        sheets_per_batch=5,
    )

    app = QApplication(sys.argv)
    window = MainWindow(test_config)

    print("已登记测试任务：")
    for index, (document_name, (pages, blank_pages)) in enumerate(DOCUMENTS.items(), start=1):
        source = make_pdf(staged / document_name, pages, blank_pages)
        task = register_task(window.repository, source, f"SP-T4G-{index:03d}")
        print(f"  {task.task_uid}  {document_name}  {pages} 页")

    window._refresh_tasks()
    window.show()
    app.processEvents()

    try:
        run_checks(window, workdir)
    except Exception:
        import traceback

        traceback.print_exc()
        check(False, "界面检查过程中出现异常")

    try:
        window.close()
    except Exception:
        pass
    app.processEvents()

    shutil.rmtree(workdir, ignore_errors=True)

    passed = sum(1 for ok, _ in results if ok)
    total = len(results)
    print(f"\n结果：{passed}/{total} 项通过")
    failed = [desc for ok, desc in results if not ok]
    if failed:
        print("未通过：")
        for desc in failed:
            print(f"  - {desc}")
        return 1
    print("阶段四界面验收通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
