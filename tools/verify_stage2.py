"""SmartPrint 阶段二验收脚本（不依赖界面，纯逻辑验收）。

覆盖的验收点：

1. 启动时扫描已有 PDF → 登记为任务，页数与纸张尺寸正确。
2. 目录监控（watchdog）→ 运行中新出现的 PDF 能自动登记。
3. 写入过程 → 边写边出现的 PDF 会等待写入完成，不会读到半个文件。
4. 无效文件 → 不会登记成任务，记录为接收失败。
5. 重复文件 → 同一 PDF24 任务 ID 不会重复登记。
6. 重启扫描 → 已登记的文件不会再次登记，任务记录仍然存在。

运行方式（项目根目录下）：

    conda activate py313
    python tools\\verify_stage2.py
"""

from __future__ import annotations

import logging
import shutil
import sys
import time
from dataclasses import replace
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pymupdf  # noqa: E402

from app.core.config import AppConfig, IncomingSettings, load_config  # noqa: E402
from app.database.db import Database  # noqa: E402
from app.database.task_repository import TaskRepository  # noqa: E402
from app.services.task_intake import TaskIntakeService  # noqa: E402

A4_WIDTH_PT = 595.28
A4_HEIGHT_PT = 841.89

results: list[tuple[bool, str]] = []


def check(condition: bool, description: str) -> bool:
    results.append((bool(condition), description))
    print(f"  [{'PASS' if condition else 'FAIL'}] {description}")
    return bool(condition)


def make_pdf(path: Path, pages: int, width: float = A4_WIDTH_PT, height: float = A4_HEIGHT_PT) -> Path:
    """生成指定页数的测试 PDF。"""
    document = pymupdf.open()
    for index in range(pages):
        page = document.new_page(width=width, height=height)
        page.insert_text((72, 100), f"Test page {index + 1} / {pages}", fontsize=24)
    document.save(path)
    document.close()
    return path


def pdf24_name(stamp: str, job_id: str, document_name: str) -> str:
    """按 PDF24 文件名模板拼出文件名。"""
    return f"{stamp}_{job_id}_{document_name}"


def wait_until(predicate, timeout: float, description: str, interval: float = 0.2) -> bool:
    """等待条件成立。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    print(f"  [超时] {description}")
    return False


def build_config(db_path: Path, incoming_dir: Path) -> AppConfig:
    base = load_config()
    return replace(
        base,
        incoming_dir=incoming_dir,
        database_file=db_path,
        incoming=IncomingSettings(
            stability_seconds=1.0,
            poll_interval_seconds=0.2,
            max_wait_seconds=30.0,
            rescan_interval_seconds=0.0,  # 关闭定时扫描，保证验收只考察显式行为
            min_size_bytes=512,
        ),
    )


def main() -> int:
    logging.basicConfig(level=logging.CRITICAL, format="%(levelname)s %(name)s: %(message)s")

    # 临时目录放在项目内（Data/jobs 已在 .gitignore 中），避免系统临时目录权限问题
    # 注意：不要用 tempfile.mkdtemp（它会把目录权限设成 0o700，在受限环境下会无法再写入子项）
    jobs_dir = PROJECT_ROOT / "Data" / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    workdir = jobs_dir / f"_verify_stage2_{time.strftime('%Y%m%d_%H%M%S')}"
    workdir.mkdir(parents=True, exist_ok=True)
    incoming = workdir / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    db_path = workdir / "smartprint_test.db"

    print(f"临时目录：{workdir}\n")

    # ------------------------------------------------------------------ 准备数据
    staged = workdir / "staged"
    staged.mkdir()

    pre_files = {
        pdf24_name("2026-11-05_19-00-01", "AAA00001", "单页.pdf"): 1,
        pdf24_name("2026-11-05_19-00-02", "AAA00002", "双页.pdf"): 2,
        pdf24_name("2026-11-05_19-00-03", "AAA00003", "五页.pdf"): 5,
        pdf24_name("2026-11-05_19-00-04", "AAA00004", "学习资料37页.pdf"): 37,
    }
    for name, pages in pre_files.items():
        make_pdf(staged / name, pages)
        shutil.copy2(staged / name, incoming / name)

    # 无效文件（体积够大但不是 PDF）
    broken = incoming / pdf24_name("2026-11-05_19-00-05", "BAD00001", "坏文件.pdf")
    broken.write_bytes(b"this is not a pdf file\r\n" * 100)

    # 与「单页.pdf」具有相同 PDF24 任务 ID 的重复文件（写入时间更晚，保证先处理原文件）
    time.sleep(0.1)
    duplicate = incoming / pdf24_name("2026-11-05_19-00-06", "AAA00001", "单页重复.pdf")
    duplicate.write_bytes((staged / pdf24_name("2026-11-05_19-00-01", "AAA00001", "单页.pdf")).read_bytes())

    print("【检查 1】启动扫描：已有 PDF 自动登记为任务")
    database = Database(db_path)
    database.initialize()
    repository = TaskRepository(database)
    config = build_config(db_path, incoming)

    service = TaskIntakeService(config, repository, incoming)
    service.start()

    ok = wait_until(lambda: repository.count_tasks() >= 4, timeout=30, description="等待 4 个任务登记")
    ok2 = wait_until(
        lambda: service.state().stats.failed >= 1, timeout=15, description="等待坏文件被判定为失败"
    )
    time.sleep(1.0)
    service.stop()

    tasks = repository.list_tasks()
    check(ok and len(tasks) == 4, f"4 个有效 PDF 登记为任务（实际 {len(tasks)} 个）")
    check(ok2, "无效 PDF 未登记为任务，并记录为接收失败")

    page_map = {task.document_name: task.page_count for task in tasks}
    check(page_map.get("单页.pdf") == 1, "单页.pdf 页数为 1")
    check(page_map.get("双页.pdf") == 2, "双页.pdf 页数为 2")
    check(page_map.get("五页.pdf") == 5, "五页.pdf 页数为 5")
    check(page_map.get("学习资料37页.pdf") == 37, "学习资料37页.pdf 页数为 37")

    check(all(task.paper_size == "A4" for task in tasks), "纸张尺寸识别为 A4")
    check(all(task.orientation == "纵向" for task in tasks), "页面方向识别为纵向")
    check(all(abs(task.width_pt - A4_WIDTH_PT) < 1 for task in tasks), "页面宽度读取正确")
    check(
        all(task.source_job_id in {"AAA00001", "AAA00002", "AAA00003", "AAA00004"} for task in tasks),
        "PDF24 任务 ID 解析正确",
    )
    check(len({task.task_uid for task in tasks}) == len(tasks), "任务号唯一")
    check(all(task.task_uid.startswith("SP-") for task in tasks), "任务号格式为 SP-…")
    check(all(task.status_label == "待处理" for task in tasks), "新任务状态为「待处理」")
    check(all(task.file_hash for task in tasks), "已记录文件 SHA-256")
    check(not repository.exists_source_job_id("BAD00001"), "坏文件未进入数据库")
    check(
        not any("单页重复.pdf" == task.document_name for task in tasks),
        "同一 PDF24 任务 ID 的重复文件未被重复登记",
    )
    check(
        len([event for task in tasks for event in repository.list_events(task.task_uid)]) >= 4,
        "每个任务都有事件记录",
    )

    # -------------------------------------------------------------- 目录监控
    print("\n【检查 2】运行中监控：新出现的 PDF 自动登记")
    service = TaskIntakeService(config, repository, incoming)
    service.start()
    time.sleep(1.5)

    live_name = pdf24_name("2026-11-05_19-10-01", "LIVE0001", "监控新增.pdf")
    shutil.copy2(make_pdf(staged / "live.pdf", 12), incoming / live_name)
    live_ok = wait_until(
        lambda: any(task.document_name == "监控新增.pdf" for task in repository.list_tasks()),
        timeout=25,
        description="等待 watchdog 发现新文件",
    )
    check(live_ok, "watchdog 监控发现新 PDF 并登记")

    live_task = next((t for t in repository.list_tasks() if t.document_name == "监控新增.pdf"), None)
    check(live_task is not None and live_task.page_count == 12, "监控登记的任务页数为 12")

    # ------------------------------------------------------- 写入过程等待写入完成
    print("\n【检查 3】写入过程：等待文件写完再读取")
    slow_source = make_pdf(staged / "slow.pdf", 9)
    slow_payload = slow_source.read_bytes()
    slow_target = incoming / pdf24_name("2026-11-05_19-20-01", "SLOW0001", "分块写入.pdf")

    chunk = max(1, len(slow_payload) // 4)
    with slow_target.open("wb") as handle:
        for offset in range(0, len(slow_payload), chunk):
            handle.write(slow_payload[offset : offset + chunk])
            handle.flush()
            time.sleep(0.6)  # 小于 stability_seconds(1.0)，模拟仍在写入

    slow_ok = wait_until(
        lambda: any(task.document_name == "分块写入.pdf" for task in repository.list_tasks()),
        timeout=25,
        description="等待分块写入的 PDF 登记",
    )
    slow_task = next((t for t in repository.list_tasks() if t.document_name == "分块写入.pdf"), None)
    check(slow_ok, "分块写入的 PDF 最终登记为任务")
    check(slow_task is not None and slow_task.page_count == 9, "分块写入的 PDF 页数读取正确（9 页）")
    check(
        slow_task is not None and slow_task.file_size == len(slow_payload),
        "分块写入的 PDF 字节数完整（说明等到写入完成才读取）",
    )

    service.stop()

    # ------------------------------------------------------------------ 重启扫描
    print("\n【检查 4】重启扫描：已登记文件不重复登记，任务记录仍在")
    before = repository.count_tasks()
    restarted = TaskIntakeService(config, repository, incoming)
    restarted.start()
    time.sleep(3.0)
    restarted.stop()
    after = repository.count_tasks()
    check(before == after, f"重启后任务数量不变（{before} → {after}）")
    check(after == 6, f"任务总数为 6（4 个启动扫描 + 1 个监控发现 + 1 个分块写入，实际 {after}）")

    # ------------------------------------------------------------------ 收尾
    print("\n任务列表：")
    for task in repository.list_tasks():
        print(
            f"  {task.task_uid}  {task.document_name:<22} {task.page_count:>4} 页  "
            f"{task.size_text:<8} {task.status_label}  {task.created_at_text}"
        )

    print("\n事件记录（学习资料37页.pdf）：")
    for task in repository.list_tasks():
        if task.document_name == "学习资料37页.pdf":
            for event in repository.list_events(task.task_uid):
                print(f"  [{event.created_at:%H:%M:%S}] {event.event_type}: {event.message}")

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
    print("阶段二逻辑验收通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
