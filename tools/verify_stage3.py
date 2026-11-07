"""SmartPrint 阶段三验收脚本（逻辑部分，不依赖界面）。

覆盖的验收点：

1. 任务列表：显示全部 / 按状态筛选 / 关键字搜索 / 排序 / 数量统计。
2. 任务详情数据：页数、纸张、预计用纸、创建时间、事件记录。
3. 状态管理：合法流转写入事件，非法流转被拒绝。
4. 删除任务：记录与事件一起删除，源 PDF 默认归档、可选直接删除，
   删除后重新扫描不会又被登记回来。
5. 退出后恢复：换一个数据库连接重新读取，任务与状态仍然存在。

运行方式（项目根目录下）：

    conda activate py313
    python tools\\verify_stage3.py
"""

from __future__ import annotations

import logging
import shutil
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pymupdf  # noqa: E402

from app.core.config import AppConfig, IncomingSettings, load_config  # noqa: E402
from app.core.models import Task  # noqa: E402
from app.core.pdf_inspector import inspect_pdf  # noqa: E402
from app.core.task_status import ACTIVE_STATUSES, TaskStatus  # noqa: E402
from app.database.db import Database  # noqa: E402
from app.database.task_repository import TaskRepository  # noqa: E402
from app.services.task_admin import (  # noqa: E402
    FILE_ARCHIVED,
    FILE_DELETED,
    TaskAdminError,
    TaskAdminService,
)
from app.services.task_intake import TaskIntakeService  # noqa: E402

A4_WIDTH_PT = 595.28
A4_HEIGHT_PT = 841.89

results: list[tuple[bool, str]] = []


def check(condition: bool, description: str) -> bool:
    results.append((bool(condition), description))
    print(f"  [{'PASS' if condition else 'FAIL'}] {description}")
    return bool(condition)


def make_pdf(path: Path, pages: int) -> Path:
    document = pymupdf.open()
    for index in range(pages):
        page = document.new_page(width=A4_WIDTH_PT, height=A4_HEIGHT_PT)
        page.insert_text((72, 100), f"Stage3 test page {index + 1} / {pages}", fontsize=24)
    document.save(path)
    document.close()
    return path


def register_task(
    repository: TaskRepository,
    path: Path,
    document_name: str,
    status: TaskStatus = TaskStatus.PENDING,
    created_offset_seconds: int = 0,
) -> Task:
    """按任务接收的规则构造并写入一个任务（不经过 watchdog，保证结果确定）。"""
    inspection = inspect_pdf(path)
    created = datetime.now() + timedelta(seconds=created_offset_seconds)
    task = Task(
        task_uid=f"SP-TEST-{created_offset_seconds:03d}",
        file_name=path.name,
        document_name=document_name,
        source_path=path,
        source_job_id=f"JOB{created_offset_seconds:04d}",
        file_size=path.stat().st_size,
        page_count=inspection.page_count,
        paper_size=inspection.paper_size,
        orientation=inspection.orientation,
        width_pt=inspection.width_pt,
        height_pt=inspection.height_pt,
        status=status,
        created_at=created,
        updated_at=created,
    )
    repository.add_task(task)
    repository.add_event(task.task_uid, "received", f"PDF 已接收：{path.name}")
    return task


def build_config(db_path: Path, incoming_dir: Path) -> AppConfig:
    return replace(
        load_config(),
        incoming_dir=incoming_dir,
        database_file=db_path,
        incoming=IncomingSettings(
            stability_seconds=0.5,
            poll_interval_seconds=0.1,
            max_wait_seconds=20.0,
            rescan_interval_seconds=0.0,
            min_size_bytes=512,
        ),
    )


def main() -> int:
    logging.basicConfig(level=logging.CRITICAL, format="%(levelname)s %(name)s: %(message)s")

    jobs_dir = PROJECT_ROOT / "Data" / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    workdir = jobs_dir / f"_verify_stage3_{time.strftime('%Y%m%d_%H%M%S')}"
    workdir.mkdir(parents=True, exist_ok=True)
    incoming = workdir / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    db_path = workdir / "stage3_test.db"
    archive_dir = workdir / "archive"

    print(f"临时目录：{workdir}\n")

    database = Database(db_path)
    database.initialize()
    repository = TaskRepository(database)
    admin = TaskAdminService(repository, incoming_dir=incoming, archive_dir=archive_dir)

    # ------------------------------------------------------------- 准备 6 个任务
    specs = [
        ("A-单页.pdf", 1),
        ("B-双页.pdf", 2),
        ("C-五页.pdf", 5),
        ("D-十页.pdf", 10),
        ("E-学习资料37页.pdf", 37),
        ("F-已打印完成.pdf", 12),
    ]
    created_tasks: list[Task] = []
    for index, (doc_name, pages) in enumerate(specs):
        pdf_name = f"2026-11-05_20-0{index}-00_JOB{index:04d}_{doc_name}"
        path = make_pdf(incoming / pdf_name, pages)
        status = TaskStatus.COMPLETED if doc_name.startswith("F-") else TaskStatus.PENDING
        created_tasks.append(
            register_task(repository, path, doc_name, status, created_offset_seconds=index)
        )
    # 其中一个任务改成「已准备」，用于筛选与流转测试
    admin.set_status(created_tasks[1].task_uid, TaskStatus.READY, "验收预置")
    ready_task = repository.get_task(created_tasks[1].task_uid)

    print("【检查 1】任务列表与状态统计")
    check(repository.count_tasks() == 6, f"任务总数为 6（实际 {repository.count_tasks()}）")
    counts = repository.count_by_status()
    check(counts[TaskStatus.PENDING] == 4, f"待处理任务 4 个（实际 {counts[TaskStatus.PENDING]}）")
    check(counts[TaskStatus.READY] == 1, f"已准备任务 1 个（实际 {counts[TaskStatus.READY]}）")
    check(counts[TaskStatus.COMPLETED] == 1, f"已完成任务 1 个（实际 {counts[TaskStatus.COMPLETED]}）")

    all_tasks = repository.list_tasks()
    check(len(all_tasks) == 6, "默认查询返回全部任务")
    check(
        all_tasks[0].document_name == "F-已打印完成.pdf",
        f"默认按创建时间倒序（第一条：{all_tasks[0].document_name}）",
    )
    check(
        ready_task is not None and ready_task.status == TaskStatus.READY,
        "预置任务状态为「已准备」",
    )
    check(
        ready_task is not None
        and any("验收预置" in event.message for event in repository.list_events(ready_task.task_uid)),
        "状态变更写入了事件记录",
    )

    print("\n【检查 2】状态筛选")
    pending = repository.list_tasks(statuses=[TaskStatus.PENDING])
    check(len(pending) == 4, f"筛选「待处理」得到 4 个（实际 {len(pending)}）")
    check(all(task.status == TaskStatus.PENDING for task in pending), "筛选结果状态都正确")
    finished = repository.list_tasks(statuses=[TaskStatus.READY, TaskStatus.COMPLETED])
    check(len(finished) == 2, f"筛选「已准备 + 已完成」得到 2 个（实际 {len(finished)}）")
    active = repository.list_tasks(statuses=ACTIVE_STATUSES)
    check(len(active) == 5, f"筛选「未结束」得到 5 个（实际 {len(active)}）")
    check(repository.list_tasks(statuses=[]) == repository.list_tasks(), "空筛选条件等于不筛选")

    print("\n【检查 3】关键字搜索")
    check(len(repository.list_tasks(search="学习资料")) == 1, "按中文文件名搜索命中 1 个")
    check(len(repository.list_tasks(search="37页")) == 1, "按文件名片段搜索命中 1 个")
    target_uid = created_tasks[2].task_uid
    check(
        len(repository.list_tasks(search=target_uid)) == 1,
        "按任务号搜索命中 1 个",
    )
    check(len(repository.list_tasks(search="pdf")) == 6, "搜索不区分大小写（.pdf 命中全部）")
    check(len(repository.list_tasks(search="%")) == 0, "百分号按普通字符处理，不会变成通配符")
    check(len(repository.list_tasks(search="不存在的文档")) == 0, "无匹配时返回空列表")

    print("\n【检查 4】排序")
    by_pages_asc = repository.list_tasks(sort_by="page_count", descending=False)
    check(
        [task.page_count for task in by_pages_asc] == [1, 2, 5, 10, 12, 37],
        f"按页数正序：{[task.page_count for task in by_pages_asc]}",
    )
    by_pages_desc = repository.list_tasks(sort_by="page_count", descending=True)
    check(by_pages_desc[0].page_count == 37, "按页数倒序时第一行为 37 页")
    by_name = repository.list_tasks(sort_by="document_name", descending=False)
    check(
        [task.document_name[0] for task in by_name] == list("ABCDEF"),
        f"按文件名正序：{[task.document_name for task in by_name][:3]}…",
    )
    limited = repository.list_tasks(limit=2)
    check(len(limited) == 2, "limit 参数生效")
    fallback = repository.list_tasks(sort_by="drop table tasks;--")
    check(len(fallback) == 6, "非法排序字段被忽略（使用默认排序）")

    print("\n【检查 5】任务详情所需数据")
    big = repository.list_tasks(search="学习资料")[0]
    check(big.page_count == 37, "37 页文档页数正确")
    check(big.estimated_sheets == 19, f"预计用纸 19 张（实际 {big.estimated_sheets}）")
    check(big.estimated_batches(5) == 4, f"每批 5 张时预计 4 批（实际 {big.estimated_batches(5)}）")
    check(big.size_text == "A4 纵向", f"纸张信息为「{big.size_text}」")
    check(bool(big.created_at_text), "创建时间可格式化显示")
    check(big.source_exists, "源文件存在，可以预览")

    print("\n【检查 6】状态流转")
    pending_task = pending[0]
    admin.set_status(pending_task.task_uid, TaskStatus.READY, "验收：标记为已准备")
    updated = repository.get_task(pending_task.task_uid)
    check(updated is not None and updated.status == TaskStatus.READY, "待处理 → 已准备 成功")
    check(admin.can_transition(TaskStatus.READY, TaskStatus.PAUSED), "已准备 → 已暂停 是允许的")
    check(not admin.can_transition(TaskStatus.COMPLETED, TaskStatus.READY), "已完成 不能回到别的状态")
    try:
        admin.set_status(created_tasks[5].task_uid, TaskStatus.READY, "非法流转")
        check(False, "非法流转应被拒绝")
    except TaskAdminError as exc:
        check("不能变更" in str(exc), f"非法流转被拒绝（{exc}）")

    try:
        admin.set_status(pending_task.task_uid, TaskStatus.READY, "重复设置")
        check(False, "重复设置同一状态应被拒绝")
    except TaskAdminError:
        check(True, "重复设置同一状态被拒绝")

    try:
        admin.set_status("SP-NOT-EXIST", TaskStatus.READY)
        check(False, "不存在的任务应报错")
    except TaskAdminError:
        check(True, "对不存在的任务操作时报错")

    events = repository.list_events(updated.task_uid)
    check(len(events) >= 2, f"任务事件累计 {len(events)} 条（接收 + 本次状态变更）")

    print("\n【检查 7】删除任务（默认归档源文件）")
    task_to_delete = repository.list_tasks(search="五页")[0]
    source_before = Path(task_to_delete.source_path)
    check(source_before.exists(), "删除前源文件存在")
    outcomes = admin.delete_tasks([task_to_delete.task_uid])
    outcome = outcomes[0]
    check(outcome.record_deleted and outcome.error is None, "任务记录删除成功")
    check(outcome.file_action == FILE_ARCHIVED, f"源文件按默认策略归档（{outcome.file_action}）")
    check(not source_before.exists(), "incoming 中的源文件已被移走")
    check(
        outcome.file_path is not None and Path(outcome.file_path).exists(),
        f"归档文件存在：{outcome.file_path}",
    )
    check(repository.get_task(task_to_delete.task_uid) is None, "任务已从数据库移除")
    check(len(repository.list_events(task_to_delete.task_uid)) == 0, "任务事件一并删除")
    check(repository.count_tasks() == 5, f"任务总数为 5（实际 {repository.count_tasks()}）")

    print("\n【检查 8】删除任务（同时删除文件）")
    task_to_purge = repository.list_tasks(search="十页")[0]
    purge_path = Path(task_to_purge.source_path)
    outcome = admin.delete_tasks([task_to_purge.task_uid], delete_files=True)[0]
    check(outcome.record_deleted and outcome.file_action == FILE_DELETED, "记录与文件都删除")
    check(not purge_path.exists(), "incoming 中的文件确实被删除")
    check(
        admin.delete_tasks(["SP-NOT-EXIST"])[0].error is not None,
        "删除不存在的任务返回错误信息而不是崩溃",
    )

    print("\n【检查 9】退出后恢复任务")
    restarted_db = Database(db_path)
    restarted_repo = TaskRepository(restarted_db)
    restored = restarted_repo.list_tasks()
    check(len(restored) == 4, f"重新连接数据库后有 4 个任务（实际 {len(restored)}）")
    restored_status = {task.document_name: task.status for task in restored}
    check(
        restored_status.get("B-双页.pdf") == TaskStatus.READY,
        "重启后「已准备」状态仍然保留",
    )
    check(
        restored_status.get("F-已打印完成.pdf") == TaskStatus.COMPLETED,
        "重启后「已完成」状态仍然保留",
    )
    check(
        restarted_repo.count_tasks() == 4 and len(restarted_repo.list_events(restored[0].task_uid)) >= 1,
        "重启后任务事件仍然存在",
    )

    print("\n【检查 10】删除后重新扫描不会又登记回来")
    config = build_config(db_path, incoming)
    service = TaskIntakeService(config, restarted_repo, incoming)
    service.start()
    time.sleep(2.5)
    service.stop()
    check(
        restarted_repo.count_tasks() == 4,
        f"重新扫描后任务数量不变（实际 {restarted_repo.count_tasks()}）",
    )
    check(
        not any(task.document_name.startswith("C-五页") for task in restarted_repo.list_tasks()),
        "已删除并归档的文件没有被重新登记",
    )

    print("\n当前任务列表：")
    for task in restarted_repo.list_tasks():
        print(
            f"  {task.task_uid}  {task.document_name:<18} {task.page_count:>3} 页  "
            f"{task.estimated_sheets:>2} 张  {task.status_label}  {task.created_at_text}"
        )

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
    print("阶段三逻辑验收通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
