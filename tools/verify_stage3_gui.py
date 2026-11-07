"""SmartPrint 阶段三界面验收脚本（离屏运行，不需要人工点击）。

覆盖的验收点：

1. 连续提交多个 PDF：任务自动出现在表格中，列内容（页数、纸张、时间、状态）正确。
2. 点击表头排序：按页数正序 / 倒序。
3. 状态筛选与关键字搜索。
4. 任务详情与任务记录（事件）显示。
5. PDF 预览窗口：页数、翻页、缩放、适应宽度。
6. 界面状态操作（标记为已准备）与删除任务。
7. 关闭窗口再重新打开：任务记录、状态都还在。

脚本使用独立的测试目录与测试数据库（`Data/jobs/_verify_stage3_gui_时间戳/`），
跑完自动删除，不会污染正式任务库。

运行方式（项目根目录下）：

    conda activate py313
    python tools\\verify_stage3_gui.py
"""

from __future__ import annotations

import os
import shutil
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # 离屏运行，不弹窗

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pymupdf  # noqa: E402
from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core.config import IncomingSettings, load_config  # noqa: E402
from app.core.task_status import TaskStatus  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from app.ui.pdf_preview_dialog import PdfPreviewDialog  # noqa: E402
from app.utils.logging_setup import setup_logging  # noqa: E402
from app.utils.paths import ensure_dirs  # noqa: E402

A4_WIDTH_PT = 595.28
A4_HEIGHT_PT = 841.89

#: 模拟连续提交的 4 份 PDF：文件名 → 页数
DOCUMENTS: dict[str, int] = {
    "阶段三-三页.pdf": 3,
    "阶段三-七页.pdf": 7,
    "阶段三-十二页.pdf": 12,
    "阶段三-三十七页.pdf": 37,
}

results: list[tuple[bool, str]] = []


def check(condition: bool, description: str) -> bool:
    results.append((bool(condition), description))
    print(f"  [{'PASS' if condition else 'FAIL'}] {description}")
    return bool(condition)


def make_pdf(path: Path, pages: int) -> Path:
    document = pymupdf.open()
    for index in range(pages):
        page = document.new_page(width=A4_WIDTH_PT, height=A4_HEIGHT_PT)
        page.insert_text((72, 100), f"Stage 3 GUI test - page {index + 1} / {pages}", fontsize=24)
    document.save(path)
    document.close()
    return path


def pdf24_name(document_name: str) -> str:
    job_id = f"G3{uuid.uuid4().hex[:6].upper()}"
    return f"{time.strftime('%Y-%m-%d_%H-%M-%S')}_{job_id}_{document_name}"


def run_checks(window: MainWindow, workdir: Path) -> None:
    app = QApplication.instance()
    assert app is not None

    print("\n【检查 1】连续提交多个 PDF，任务自动出现在表格中")
    table = window.table
    check(table.rowCount() == 4, f"表格显示 4 行任务（实际 {table.rowCount()}）")
    check(table.columnCount() == 9, f"表格共 9 列（实际 {table.columnCount()}）")
    headers = [table.horizontalHeaderItem(i).text() for i in range(table.columnCount())]
    check(
        headers
        == [
            "任务号",
            "文件名",
            "页数",
            "预计纸张",
            "批次数",
            "纸张",
            "创建时间",
            "状态",
            "打印进度",
        ],
        f"表头为：{headers}",
    )
    check("任务总数：4" in window.summary_label.text(), f"统计栏：{window.summary_label.text()}")
    check(
        all(table.item(row, 7).text() == "待处理" for row in range(4)),
        "4 个新任务状态都是「待处理」",
    )
    check(
        all(table.item(row, 5).text() == "A4 纵向" for row in range(4)),
        "纸张列显示 A4 纵向",
    )
    check(
        all(len(table.item(row, 6).text()) == 19 for row in range(4)),
        "创建时间列格式为 YYYY-MM-DD HH:MM:SS",
    )
    seven_row = next(
        row for row in range(4) if table.item(row, 1).text() == "阶段三-七页.pdf"
    )
    check(table.item(seven_row, 2).text() == "7", "七页文档的页数为 7")
    check(table.item(seven_row, 3).text() == "4 张", "七页文档预计用纸 4 张")
    check(table.item(seven_row, 4).text() == "1 批", "七页文档预计 1 批")

    print("\n【检查 2】点击表头排序")
    window._on_header_clicked(2)  # 页数：正序
    check(window.table.item(0, 2).text() == "3", "第一次点击「页数」按正序排列（首行 3 页）")
    window._on_header_clicked(2)  # 页数：倒序
    check(window.table.item(0, 2).text() == "37", "第二次点击「页数」改为倒序（首行 37 页）")
    check("页数 倒序" in window.sort_label.text(), f"排序提示：{window.sort_label.text()}")
    window._on_header_clicked(6)  # 创建时间：默认倒序
    check(window.table.item(0, 6).text() == window._tasks[0].created_at_text, "按创建时间排序生效")

    print("\n【检查 3】状态筛选与搜索")
    window._on_clear_filter()
    window.filter_combo.setCurrentIndex(2)  # 待处理
    check(window.table.rowCount() == 4, "筛选「待处理」显示 4 个任务")
    window.filter_combo.setCurrentIndex(6)  # 已完成
    check(window.table.rowCount() == 0, "筛选「已完成」显示 0 个任务")
    window.filter_combo.setCurrentIndex(0)
    window.search_edit.setText("三十七")
    window._refresh_tasks()
    check(window.table.rowCount() == 1, f"搜索「三十七」命中 1 个（实际 {window.table.rowCount()}）")
    window.search_edit.setText("不存在的文档")
    window._refresh_tasks()
    check(window.table.rowCount() == 0, "无匹配关键字时列表为空")
    window._on_clear_filter()
    check(window.table.rowCount() == 4, "清除筛选后恢复显示 4 个任务")

    print("\n【检查 4】任务详情与任务记录")
    window._op_row = next(
        row for row in range(4) if window.table.item(row, 1).text() == "阶段三-三十七页.pdf"
    )
    window.table.selectRow(window._op_row)
    window._on_selection_changed()
    detail_text = window.detail_view.toPlainText()
    check("37 页" in detail_text, "详情中显示页数 37")
    check("19 张" in detail_text and "4 批" in detail_text, "详情中显示预计用纸 19 张 / 4 批")
    check("A4 纵向" in detail_text, "详情中显示纸张大小")
    check("received" in window.event_view.toPlainText(), "任务记录中显示接收事件")
    check(window.act_preview.isEnabled(), "选中任务后「预览 PDF」可用")

    print("\n【检查 5】PDF 预览")
    task = window._current_task()
    dialog = window.create_preview_dialog(task)
    dialog.show()
    app.processEvents()
    check(dialog.page_count == 37, f"预览窗口识别到 37 页（实际 {dialog.page_count}）")
    check(dialog.current_page == 1, "预览默认显示第 1 页")
    check(dialog.rendered_pixmap_size[0] > 0, f"第 1 页渲染尺寸 {dialog.rendered_pixmap_size}")
    dialog.next_page()
    check(dialog.current_page == 2, "下一页按钮生效（第 2 页）")
    dialog.goto_page(37)
    check(dialog.current_page == 37, "跳转到第 37 页")
    check(
        not dialog.next_button.isEnabled(),
        "最后一页时「下一页」按钮被禁用",
    )
    width_before = dialog.rendered_pixmap_size[0]
    dialog.zoom_in()
    width_after = dialog.rendered_pixmap_size[0]
    check(width_after > width_before, f"放大后图像变大（{width_before} → {width_after}）")
    dialog.fit_width()
    check(
        dialog.rendered_pixmap_size[0] > 0 and "适应宽度" in dialog.size_label.text(),
        f"适应宽度生效（{dialog.size_label.text()}）",
    )
    dialog.close()

    missing_dialog = PdfPreviewDialog(workdir / "不存在的文件.pdf")
    check(bool(missing_dialog.error_message), "源文件缺失时预览窗口给出错误提示")
    check(missing_dialog.page_count == 0, "缺失文件的预览不显示页数")
    missing_dialog.close()

    print("\n【检查 6】状态操作与删除任务")
    seven_row = next(
        row for row in range(window.table.rowCount())
        if window.table.item(row, 1).text() == "阶段三-七页.pdf"
    )
    window.table.selectRow(seven_row)
    window._on_selection_changed()
    seven_uid = window._tasks[seven_row].task_uid
    window.act_ready.trigger()
    app.processEvents()
    status_after = window.repository.get_task(seven_uid).status
    check(status_after == TaskStatus.READY, "「标记为已准备」后数据库状态为已准备")
    check(
        any(
            window.table.item(row, 1).text() == "阶段三-七页.pdf"
            and window.table.item(row, 7).text() == "已准备"
            for row in range(window.table.rowCount())
        ),
        "表格中该任务状态显示为「已准备」",
    )

    delete_uid = next(
        task.task_uid for task in window._tasks if task.document_name == "阶段三-十二页.pdf"
    )
    delete_path = window.repository.get_task(delete_uid).source_path
    outcomes = window.delete_tasks([delete_uid])
    app.processEvents()
    check(outcomes[0].record_deleted, "删除任务记录成功")
    check(
        outcomes[0].file_action == "archived" and not delete_path.exists(),
        "源 PDF 已从 incoming 归档",
    )
    check(window.table.rowCount() == 3, f"删除后表格剩余 3 行（实际 {window.table.rowCount()}）")
    check(window.repository.get_task(delete_uid) is None, "任务已从数据库移除")

    window.close()
    app.processEvents()


def main() -> int:
    ensure_dirs()
    setup_logging()

    workdir = PROJECT_ROOT / "Data" / "jobs" / f"_verify_stage3_gui_{time.strftime('%Y%m%d_%H%M%S')}"
    incoming = workdir / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    staged = workdir / "staged"
    staged.mkdir(parents=True, exist_ok=True)

    test_config = replace(
        load_config(),
        incoming_dir=incoming,
        database_file=workdir / "stage3_gui.db",
        archive_dir=workdir / "archive",
        incoming=IncomingSettings(
            stability_seconds=0.5,
            poll_interval_seconds=0.1,
            max_wait_seconds=30.0,
            rescan_interval_seconds=0.0,
            min_size_bytes=512,
        ),
    )

    targets: list[Path] = []
    for document_name, pages in DOCUMENTS.items():
        source = make_pdf(staged / document_name, pages)
        target = incoming / pdf24_name(document_name)
        shutil.copy2(source, target)
        targets.append(target)
    print("已模拟 PDF24 连续保存 4 个 PDF：")
    for target in targets:
        print(f"  {target.name}")

    app = QApplication(sys.argv)
    window = MainWindow(test_config)
    window.show()

    state = {"phase": "wait", "deadline": time.monotonic() + 90}

    def tick() -> None:
        if state["phase"] == "wait":
            expected = len(targets)
            if window.table.rowCount() == expected:
                state["phase"] = "check"
                print(f"\n任务已全部登记（{expected} 个），开始界面检查")
                try:
                    run_checks(window, workdir)
                except Exception:
                    import traceback

                    traceback.print_exc()
                    check(False, "界面检查过程中出现异常")
                    window.close()
                    app.quit()
                    return
                state["phase"] = "restart"
                print("\n【检查 7】关闭窗口后重新打开，任务记录仍然存在")
                try:
                    reopened = MainWindow(test_config)
                    reopened.show()
                    app.processEvents()
                    check(
                        reopened.table.rowCount() == 3,
                        f"重新打开后仍有 3 个任务（实际 {reopened.table.rowCount()}）",
                    )
                    ready_rows = [
                        row
                        for row in range(reopened.table.rowCount())
                        if reopened.table.item(row, 7).text() == "已准备"
                    ]
                    check(len(ready_rows) == 1, "重新打开后「已准备」状态仍然保留")
                    check(
                        "已从数据库恢复" in reopened.log_view.toPlainText(),
                        "启动时提示已从数据库恢复任务",
                    )
                    check(
                        "阶段三-十二页.pdf"
                        not in [task.document_name for task in reopened._tasks],
                        "已删除的任务没有重新出现",
                    )
                    reopened.close()
                    app.processEvents()
                except Exception:
                    import traceback

                    traceback.print_exc()
                    check(False, "重新打开窗口时出现异常")
                app.quit()
                return

            if time.monotonic() > state["deadline"]:
                print(f"[FAIL] 超时：只登记了 {window.table.rowCount()}/{expected} 个任务")
                window.close()
                app.quit()

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(300)

    app.exec()

    if state["phase"] != "restart":
        check(False, "未完成界面检查（超时或提前退出）")

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
    print("阶段三界面验收通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
