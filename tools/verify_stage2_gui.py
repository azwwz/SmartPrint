"""SmartPrint 阶段二界面验收脚本（离屏运行，不需要人工点击）。

做三件事：

1. 以离屏方式启动真正的 SmartPrint 主窗口（MainWindow）与任务接收服务。
2. 模拟 PDF24：把一份 PDF 以 PDF24 文件名模板放进待处理目录。
3. 确认任务出现在界面的任务列表里，并且已经写入 SQLite。

为了避免污染正式任务库，脚本使用独立的测试目录
（`Data/jobs/_verify_stage2_gui_时间戳/`）和独立的测试数据库，跑完自动删除。

运行方式（项目根目录下）：

    conda activate py313
    python tools\\verify_stage2_gui.py
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
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core.config import load_config  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from app.utils.logging_setup import setup_logging  # noqa: E402
from app.utils.paths import ensure_dirs  # noqa: E402

# 每次运行使用不同的 PDF24 任务 ID，否则会被「同任务 ID 去重」正确拦下
TEST_JOB_ID = f"GUI{uuid.uuid4().hex[:6].upper()}"
TEST_DOC_NAME = "阶段二界面验收.pdf"
TEST_PAGES = 6


def make_test_pdf(path: Path, pages: int) -> Path:
    document = pymupdf.open()
    for index in range(pages):
        page = document.new_page(width=595.28, height=841.89)
        page.insert_text((72, 100), f"GUI test page {index + 1} / {pages}", fontsize=22)
    document.save(path)
    document.close()
    return path


def main() -> int:
    ensure_dirs()
    setup_logging()

    # 验收使用独立的测试目录与测试数据库，跑完后删除，不影响 Data/incoming 与正式任务库
    workdir = PROJECT_ROOT / "Data" / "jobs" / f"_verify_stage2_gui_{time.strftime('%Y%m%d_%H%M%S')}"
    incoming = workdir / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    test_config = replace(
        load_config(),
        incoming_dir=incoming,
        database_file=workdir / "gui_test.db",
    )

    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    target = incoming / f"{stamp}_{TEST_JOB_ID}_{TEST_DOC_NAME}"
    source_pdf = make_test_pdf(workdir / "gui_source.pdf", TEST_PAGES)

    app = QApplication(sys.argv)
    window = MainWindow(test_config)
    window.show()

    state = {"dropped": False, "found": False, "deadline": time.monotonic() + 40}

    def tick() -> None:
        if not state["dropped"]:
            state["dropped"] = True
            shutil.copy2(source_pdf, target)
            print(f"已模拟 PDF24 保存文件：{target.name}")
            return

        task = next(
            (t for t in window.repository.list_tasks() if t.file_name == target.name), None
        )
        if task is not None:
            state["found"] = True
            rows = window.table.rowCount()
            # 表格第 2 列显示的是任务列表里的「文件名」列，内容是去掉扩展名的文档名
            matched_row = next(
                (
                    row
                    for row in range(rows)
                    if window.table.item(row, 1)
                    and window.table.item(row, 1).text() == task.document_name
                ),
                None,
            )
            if matched_row is None and time.monotonic() <= state["deadline"]:
                # 数据库已经登记，但界面还在等下一次轮询刷新——继续等，
                # 不要在这一刻就判失败（早期版本这里会偶发误报）。
                return
            print(f"数据库任务：{task.task_uid} {task.document_name} "
                  f"{task.page_count} 页 {task.size_text} {task.status_label}")
            print(f"界面表格行数：{rows}，命中行：{matched_row}")
            print(f"任务列表标题：{window.summary_label.text()}")
            print(f"状态栏：{window.statusBar().currentMessage()}")
            print("接收日志最后几行：")
            for line in window.log_view.toPlainText().strip().splitlines()[-4:]:
                print(f"  {line}")

            ok = (
                task.page_count == TEST_PAGES
                and task.paper_size == "A4"
                and matched_row is not None
                and window.table.item(matched_row, 2).text() == str(TEST_PAGES)
            )
            print("[PASS] 界面显示自动接收的任务" if ok else "[FAIL] 界面显示自动接收的任务")
            window._result_ok = ok
            window.close()
            app.quit()
            return

        if time.monotonic() > state["deadline"]:
            print("[FAIL] 超时：界面未出现自动接收的任务")
            window._result_ok = False
            window.close()
            app.quit()

    from PySide6.QtCore import QTimer

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(500)

    app.exec()

    shutil.rmtree(workdir, ignore_errors=True)
    ok = getattr(window, "_result_ok", False)
    print("阶段二界面验收通过。" if ok else "阶段二界面验收未通过。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
