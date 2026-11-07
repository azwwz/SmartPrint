"""SmartPrint 阶段八验收脚本（逻辑部分，不依赖界面，也不会真出纸）。

阶段八的目标（readme「九、开发阶段 → 阶段 8」）：让 SmartPrint 能作为一个普通
Windows 软件长期使用——配置可改、数据能备份、日志能查、异常有台账、任务能归档，
最后用 PyInstaller 打成不依赖 Python 的 exe。

覆盖的验收点：

1. 版本号与标题：0.8.1，标题说明是手动双面打印管理。
2. 翻面方向：只认 long_edge / short_edge，别的值一律回落到长边；
   两种方向的说明文字都能让用户看懂「怎么翻」。
3. 配置保存：改打印机/每批纸张数/翻面方向/超时/备份设置 → 写回 config.json，
   重新加载能读出来；配置文件里手工加的未知字段不会被抹掉；
   DEFAULT_CONFIG 不会被写坏（多次保存后默认值仍是 5 张/长边）。
4. 首次运行生成配置文件：目录里没有 config.json 时自动落一份默认配置。
5. 数据备份：create() 生成 zip、manifest 里有任务数与 schema 版本；
   prune() 按保留份数清理；restore() 能把删掉的任务找回来，
   并先留一份 *.before-restore-*.db 兜底。
6. 任务归档：只有已结束（已完成/已取消）的任务能归档；
   归档目录里有 source.pdf 与 summary.json，事件里留下归档记录；
   可选把 Data/jobs 里的大文件一起清掉。
7. 日志查看的级别过滤：只看错误 / 只看警告及以上 / 关键词过滤。
8. 纸张示意图与翻面提示的文案：正常轮说「本批纸张」，
   补打轮说「第 N 轮补打的第 X 张纸」。
9. 异常总览的查询口径：全表按时间倒序（最新在前），
   单个任务按发生顺序正序，可以限制条数。
10. 程序目录判定：开发态 is_frozen() 为假，项目根目录正确。
11. 命令行开关：`--version` 打印版本号并返回 0。
12. 打包产物：dist/SmartPrint/SmartPrint.exe 存在，并且
    `SmartPrint.exe --self-test <报告>` 能独立跑起来（退出码 0、报告写「打包运行：是」）。
    **这一步会用真实 exe 启动一次程序（离屏、立即退出），不会出纸。**

运行方式（项目根目录下）：

    conda activate py313
    python tools\\verify_stage8.py
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pymupdf  # noqa: E402

from app.core.config import (  # noqa: E402
    DEFAULT_CONFIG,
    FLIP_DIRECTIONS,
    config_to_dict,
    ensure_config_file,
    flip_direction_text,
    load_config,
    normalize_flip_direction,
    read_config_text,
    update_config,
)
from app.core.duplex import DuplexAnomaly  # noqa: E402
from app.core.models import Task  # noqa: E402
from app.core.pdf_inspector import inspect_pdf  # noqa: E402
from app.core.task_status import TaskStatus  # noqa: E402
from app.core.version import APP_NAME, APP_TITLE, APP_VERSION  # noqa: E402
from app.database.db import SCHEMA_VERSION, Database  # noqa: E402
from app.database.task_repository import TaskRepository  # noqa: E402
from app.services.archive_service import ArchiveService  # noqa: E402
from app.services.backup_service import BackupService  # noqa: E402
from app.ui.log_view_dialog import _level_matches  # noqa: E402
from app.ui.sheet_diagram import flip_instruction_text, flip_scope_text  # noqa: E402
from app.utils.pyinstaller import expected_executable, is_packaged_output  # noqa: E402

A4_WIDTH_PT = 595.28
A4_HEIGHT_PT = 841.89

results: list[tuple[bool, str]] = []


def check(condition: bool, description: str) -> bool:
    results.append((bool(condition), description))
    print(f"  [{'PASS' if condition else 'FAIL'}] {description}")
    return bool(condition)


# ---------------------------------------------------------------------------
# 测试素材
# ---------------------------------------------------------------------------


def make_pdf(path: Path, pages: int) -> Path:
    """生成一份每页写着 PAGE-<页码> 的测试 PDF。"""
    document = pymupdf.open()
    for index in range(pages):
        number = index + 1
        page = document.new_page(width=A4_WIDTH_PT, height=A4_HEIGHT_PT)
        page.insert_text((72, 120), f"PAGE-{number}", fontsize=32)
        page.insert_text((72, 170), f"Stage8 page {number} / {pages}", fontsize=12)
    document.save(path)
    document.close()
    return path


def register_task(
    repository: TaskRepository,
    path: Path,
    task_uid: str,
    *,
    status: TaskStatus = TaskStatus.PENDING,
) -> Task:
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
        status=status,
        created_at=now,
        updated_at=now,
    )
    return repository.add_task(task)


def count_tasks(database_file: Path) -> int:
    connection = sqlite3.connect(database_file)
    try:
        return connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# 检查 1–4：版本、翻面方向、配置保存
# ---------------------------------------------------------------------------


def check_version(work_dir: Path) -> None:
    print("\n【检查 1】版本号与标题")
    check(APP_NAME == "SmartPrint", f"程序名是 SmartPrint（实际 {APP_NAME}）")
    check(APP_VERSION == "0.8.1", f"版本号升到 0.8.1（实际 {APP_VERSION}）")
    check("手动双面打印" in APP_TITLE, f"标题说明了用途：{APP_TITLE}")


def check_flip_direction(work_dir: Path) -> None:
    print("\n【检查 2】翻面方向的取值与文案")
    check([key for key, _ in FLIP_DIRECTIONS] == ["long_edge", "short_edge"], "只提供长边/短边两种翻面方向")
    check(normalize_flip_direction("long_edge") == "long_edge", "long_edge 原样保留")
    check(normalize_flip_direction("short_edge") == "short_edge", "short_edge 原样保留")
    check(normalize_flip_direction("SHORT_EDGE") == "short_edge", "大小写不同也能认出来")
    check(normalize_flip_direction("随便写点什么") == "long_edge", "认不出来的值回落到长边，不会乱翻")
    check(normalize_flip_direction(None) == "long_edge", "空值同样回落到长边")
    long_text = flip_direction_text("long_edge")
    short_text = flip_direction_text("short_edge")
    check("长边" in long_text and "翻书" in long_text, f"长边翻面的说明好懂：{long_text}")
    check("短边" in short_text and "上下翻" in short_text, f"短边翻面的说明好懂：{short_text}")


def check_config_save(work_dir: Path) -> None:
    print("\n【检查 3】改设置后写回 config.json")
    config_path = work_dir / "config" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    # 先写一份带未知字段的配置，模拟用户手工改过文件
    config_path.write_text(
        json.dumps(
            {
                "printer": {"name": "HP LaserJet 1020"},
                "custom_note": {"作者备注": "这一行不该被抹掉"},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    defaults_before = json.loads(json.dumps(DEFAULT_CONFIG))
    updated = update_config(
        {
            "printer": {"name": "HP LaserJet 1020 1016811", "timeout_seconds": 90},
            "paper": {"sheets_per_batch": 3},
            "duplex": {"flip_direction": "short_edge"},
            "backup": {"keep": 7, "include_jobs": True},
        },
        path=config_path,
    )

    check(updated.printer_name == "HP LaserJet 1020 1016811", "打印机名改完立刻生效")
    check(updated.printer.timeout_seconds == 90.0, f"超时改成 90 秒（实际 {updated.printer.timeout_seconds}）")
    check(updated.sheets_per_batch == 3, f"每批张数改成 3（实际 {updated.sheets_per_batch}）")
    check(updated.flip_direction == "short_edge", "翻面方向改成短边")
    check(updated.backup.keep == 7 and updated.backup.include_jobs is True, "备份设置改成保留 7 份、含 jobs")

    text = read_config_text(config_path)
    check('"sheets_per_batch": 3' in text, "config.json 文件里确实写进了 3 张")
    check('"flip_direction": "short_edge"' in text, "config.json 文件里确实写进了短边")
    check("这一行不该被抹掉" in text, "手工添加的未知字段被保留，不会被程序抹掉")

    reloaded = load_config(config_path)
    check(reloaded.sheets_per_batch == 3 and reloaded.flip_direction == "short_edge", "重新加载配置文件读到的是新值")

    defaults_after = json.loads(json.dumps(DEFAULT_CONFIG))
    check(
        defaults_after["paper"]["sheets_per_batch"] == defaults_before["paper"]["sheets_per_batch"] == 5,
        "DEFAULT_CONFIG 没有被写坏（默认仍是每批 5 张）",
    )
    check(
        defaults_after["duplex"]["flip_direction"] == "long_edge",
        "DEFAULT_CONFIG 的默认翻面方向仍是长边",
    )

    data = config_to_dict(load_config(config_path))
    check(data["backup"]["keep"] == 7, "config_to_dict 把备份份数一起写回")
    check(data["paths"]["backups"].endswith("Data/backups") or "backups" in data["paths"], "paths 里记着备份目录")


def check_default_config_file(work_dir: Path) -> None:
    print("\n【检查 4】首次运行时自动生成配置文件")
    fresh_dir = work_dir / "fresh-config"
    fresh_dir.mkdir(parents=True, exist_ok=True)
    target = fresh_dir / "config.json"
    check(not target.exists(), "测试目录里一开始没有 config.json")

    created = ensure_config_file(target)
    check(created == target and target.exists(), "ensure_config_file 写出了默认配置")
    data = json.loads(target.read_text(encoding="utf-8"))
    for section in ("printer", "paper", "duplex", "incoming", "database", "paths", "backup"):
        check(section in data, f"默认配置里有 {section} 段")
    check(data["paper"]["sheets_per_batch"] == 5, "默认配置里每批 5 张纸")
    check(data["backup"]["keep"] == 10, "默认保留 10 份备份")
    check(ensure_config_file(target) is None, "已经有配置文件时不再覆盖")


# ---------------------------------------------------------------------------
# 检查 5–6：备份与归档
# ---------------------------------------------------------------------------


def check_backup(work_dir: Path) -> None:
    print("\n【检查 5】数据备份、清理与恢复")
    root = work_dir / "backup"
    data_dir = root / "Data"
    (data_dir / "database").mkdir(parents=True, exist_ok=True)
    jobs_dir = data_dir / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (jobs_dir / "page_001.pdf").write_bytes(b"%PDF-1.4 test")

    database_file = data_dir / "database" / "smartprint.db"
    database = Database(database_file)
    database.initialize()
    repository = TaskRepository(database)

    pdf = make_pdf(root / "doc-a.pdf", 4)
    register_task(repository, pdf, "SP-BACKUP-0001")
    register_task(repository, make_pdf(root / "doc-b.pdf", 4), "SP-BACKUP-0002")
    check(count_tasks(database_file) == 2, "备份前数据库里有 2 个任务")

    service = BackupService(
        database_file=database_file,
        backup_dir=root / "Data" / "backups",
        jobs_dir=jobs_dir,
        keep=5,
        include_jobs=False,
    )
    entry = service.create(label="verify")
    check(entry.path.exists() and entry.path.suffix == ".zip", f"生成备份文件：{entry.path.name}")
    check(entry.task_count == 2, f"备份里记着 2 个任务（实际 {entry.task_count}）")
    check(entry.schema_version == SCHEMA_VERSION, f"备份里记着 schema 版本 {SCHEMA_VERSION}")
    check("verify" in entry.path.name, "备份文件名带上了标签")
    check(entry.size_bytes > 0, f"备份大小可读：{entry.size_text}")

    with zipfile.ZipFile(entry.path) as archive:
        names = archive.namelist()
        check("smartprint.db" in names, "备份包里有数据库快照")
        check("manifest.json" in names, "备份包里有 manifest.json")
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
    check(manifest["task_count"] == 2, "manifest 里的任务数与数据库一致")
    check(not any(name.startswith("jobs/") for name in names), "默认不把 jobs 里的大文件打进去")

    jobs_entry = service.create(label="withjobs", include_jobs=True)
    with zipfile.ZipFile(jobs_entry.path) as archive:
        jobs_names = [name for name in archive.namelist() if name.startswith("jobs/")]
    check(any(name.endswith("page_001.pdf") for name in jobs_names), "需要时可以把 jobs 一起备份")

    backups = service.list_backups()
    check(len(backups) == 2, f"列出 2 份备份（实际 {len(backups)}）")
    check(backups[0].path == jobs_entry.path, "最新的备份排在最前面")
    removed = service.prune(keep=1)
    check(len(removed) == 1, f"按保留份数清理掉 1 份（实际 {len(removed)}）")
    check(len(service.list_backups()) == 1, "清理后只剩 1 份备份")

    # 删掉一个任务，再从备份恢复，任务应该回来
    repository.delete_task("SP-BACKUP-0002")
    check(count_tasks(database_file) == 1, "删掉 1 个任务后只剩 1 条记录")
    restore_source = jobs_entry.path if jobs_entry.path.exists() else service.list_backups()[0].path
    service.restore(restore_source)
    check(count_tasks(database_file) == 2, "从备份恢复后任务回到 2 个")
    leftovers = sorted(path.name for path in database_file.parent.glob("smartprint.before-restore-*.db"))
    check(len(leftovers) == 1, f"恢复前留了一份兜底副本（{leftovers}）")

    check("份备份" in service.summary_text(), f"备份概览文字：{service.summary_text()}")

    # 备份目录里只应留下 zip；临时快照与 SQLite 的 -wal / -shm 都要清干净
    leftovers = sorted(
        path.name
        for path in (root / "Data" / "backups").iterdir()
        if not path.name.endswith(".zip")
    )
    check(not leftovers, f"备份目录里没有遗留的临时文件（{leftovers}）")


def check_archive(work_dir: Path) -> None:
    print("\n【检查 6】任务归档")
    root = work_dir / "archive"
    data_dir = root / "Data"
    (data_dir / "database").mkdir(parents=True, exist_ok=True)
    jobs_dir = data_dir / "jobs"
    task_jobs = jobs_dir / "SP-ARCHIVE-0001"
    task_jobs.mkdir(parents=True, exist_ok=True)
    (task_jobs / "page_001.pdf").write_bytes(b"%PDF-1.4 page1")

    database_file = data_dir / "database" / "smartprint.db"
    database = Database(database_file)
    database.initialize()
    repository = TaskRepository(database)

    pdf = make_pdf(root / "doc.pdf", 4)
    done = register_task(repository, pdf, "SP-ARCHIVE-0001", status=TaskStatus.COMPLETED)
    pending = register_task(
        repository, make_pdf(root / "doc-pending.pdf", 4), "SP-ARCHIVE-0002", status=TaskStatus.PENDING
    )

    service = ArchiveService(repository, root / "Data" / "completed" / "archive", jobs_dir=jobs_dir)
    check(not service.can_archive(pending), "没结束的任务不能归档")
    check(service.can_archive(done), "已完成的任务可以归档")

    result = service.archive_tasks([done], include_pages=True, remove_jobs=True)
    check(result.ok and len(result.archived) == 1, f"归档成功：{result.summary_text}")
    directory = result.outcomes[0].directory
    check(directory is not None and directory.exists(), f"归档目录已创建：{directory}")
    assert directory is not None
    check((directory / "source.pdf").exists(), "归档目录里有原始 PDF")
    check((directory / "summary.json").exists(), "归档目录里有任务摘要")
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    check(summary["task"]["task_uid"] == "SP-ARCHIVE-0001", "摘要里的任务号正确")
    check("duplex_faces" in summary and "duplex_anomalies" in summary, "摘要里带上打印面与异常记录")
    events = repository.list_events("SP-ARCHIVE-0001")
    check(any(event.event_type == "task_archive" for event in events), "事件里留下归档记录")
    check(not task_jobs.exists(), "要求时会把 Data/jobs 里的大文件一起清掉")

    listing = service.list_archives()
    check(len(listing) == 1 and listing[0].task_uid == "SP-ARCHIVE-0001", "归档列表能读回这条归档")
    check(listing[0].document_name == done.document_name, f"归档列表记着文档名：{listing[0].document_name}")
    check("已归档 1 个任务" in service.summary_text(), f"归档概览文字：{service.summary_text()}")


# ---------------------------------------------------------------------------
# 检查 7–9：界面文案与查询口径（不建窗口）
# ---------------------------------------------------------------------------


def check_log_filter(work_dir: Path) -> None:
    print("\n【检查 7】日志按级别过滤")
    info_line = "2026-11-06 10:00:00,001 [INFO] smartprint.ui: 主窗口已创建"
    error_line = "2026-11-06 10:00:01,001 [ERROR] smartprint.services.task_intake: 接收失败：卡纸"
    warn_line = "2026-11-06 10:00:02,001 [WARNING] smartprint.printer: 打印机脱机"
    check(_level_matches(info_line, ""), "不选级别时所有行都通过")
    check(not _level_matches(info_line, "ERROR"), "只看错误时不显示 INFO 行")
    check(_level_matches(error_line, "ERROR"), "只看错误时显示 ERROR 行")
    check(not _level_matches(warn_line, "ERROR"), "只看错误时不显示 WARNING 行")
    check(_level_matches(warn_line, "WARNING"), "警告及以上包含 WARNING")
    check(_level_matches(error_line, "WARNING"), "警告及以上包含 ERROR")
    check(_level_matches(info_line, "INFO"), "只看信息时显示 INFO")


def check_diagram_text(work_dir: Path) -> None:
    print("\n【检查 8】纸张示意图与翻面提示的文案")
    long_text = flip_instruction_text("long_edge")
    short_text = flip_instruction_text("short_edge")
    check("左右翻" in long_text and "长边" in long_text, f"长边翻面提示：{long_text}")
    check("上下翻" in short_text and "短边" in short_text, f"短边翻面提示：{short_text}")
    check("先进入打印机" in long_text or "先进纸" in long_text, "提示里说明哪条边先进入打印机")

    batch_text = flip_scope_text("long_edge", scope="本批纸张")
    check("本批纸张" in batch_text, f"正常轮提示整批翻面：{batch_text}")
    check("试打" in batch_text, "提示首次使用先试打一张确认方向")

    single_text = flip_scope_text("long_edge", scope="本批纸张", sheet_number=3, round_index=1)
    check("第 1 轮补打" in single_text, f"补打轮说明是哪一轮：{single_text}")
    check("第 3 张纸" in single_text, "补打轮只让用户取这一张纸")
    check("本批纸张" not in single_text, "补打轮不再提整批")


def check_anomaly_query(work_dir: Path, repository: TaskRepository) -> None:
    print("\n【检查 9】异常记录总览的查询口径")
    now = datetime.now()
    records = [
        DuplexAnomaly(
            task_uid="SP-ANOMALY-A",
            round_index=0,
            sheet_number=2,
            face="front",
            front_page=3,
            back_page=4,
            reason="卡纸",
            action="整张纸作废",
            created_at=now,
        ),
        DuplexAnomaly(
            task_uid="SP-ANOMALY-A",
            round_index=1,
            sheet_number=2,
            face="back",
            front_page=3,
            back_page=4,
            reason="纸张污损",
            action="整张纸作废",
            created_at=now,
        ),
        DuplexAnomaly(
            task_uid="SP-ANOMALY-B",
            round_index=0,
            sheet_number=1,
            face="front",
            front_page=1,
            back_page=2,
            reason="缺纸",
            action="整张纸作废",
            created_at=now,
        ),
    ]
    for record in records:
        repository.add_duplex_anomaly(record)

    everything = repository.list_duplex_anomalies(None, limit=500)
    check(len(everything) == 3, f"全表能查到 3 条异常（实际 {len(everything)}）")
    check(everything[0].task_uid == "SP-ANOMALY-B", "总览按时间倒序，最新的排在最前")
    limited = repository.list_duplex_anomalies(None, limit=2)
    check(len(limited) == 2, "总览支持限制条数")

    of_task = repository.list_duplex_anomalies("SP-ANOMALY-A")
    check(len(of_task) == 2, "按任务查询只返回该任务的记录")
    check(of_task[0].round_index == 0 and of_task[1].round_index == 1, "单个任务内按发生顺序正序排列")
    check(of_task[0].front_page == 3 and of_task[0].back_page == 4, "记录里带着正反面页码")
    check("待补打" in of_task[0].result_text or of_task[0].resolved is False, "还没补打完成的记录显示待补打")


# ---------------------------------------------------------------------------
# 检查 10–12：程序目录、命令行开关、打包产物
# ---------------------------------------------------------------------------


def check_paths(work_dir: Path) -> None:
    print("\n【检查 10】程序目录与冻结判定")
    from app.utils import paths as paths_module

    check(paths_module.is_frozen() is False, "开发态下 is_frozen() 为假")
    check(paths_module.PROJECT_ROOT == PROJECT_ROOT, f"项目根目录正确：{paths_module.PROJECT_ROOT}")
    check(paths_module.CONFIG_FILE.parent == PROJECT_ROOT / "config", "配置文件放在 config/ 目录")
    check(paths_module.BACKUPS_DIR == paths_module.DATA_DIR / "backups", "备份目录在 Data/backups")
    check(paths_module.RESOURCES_DIR.is_dir(), "resources 目录存在（打包时会一起带上）")


def check_cli(work_dir: Path) -> None:
    print("\n【检查 11】命令行开关")
    from app.main import main as app_main

    check(app_main(["--version"]) == 0, "--version 返回 0")
    check(app_main(["--version"]) == 0, "--version 可以重复调用")


def check_package(work_dir: Path) -> None:
    print("\n【检查 12】打包产物能独立运行")
    exe = expected_executable()
    spec = PROJECT_ROOT / "SmartPrint.spec"
    check(spec.exists(), f"打包配置文件存在：{spec.name}")
    if not check(exe.exists(), f"打包产物存在：{exe}"):
        check(False, "没有找到 exe：请先运行 python -m PyInstaller --noconfirm --clean SmartPrint.spec")
        return
    check(is_packaged_output(exe), "exe 位于 dist/SmartPrint 目录下（目录版，便于整体拷贝）")
    internal = exe.parent / "_internal"
    check(internal.is_dir(), "_internal 目录存在（依赖库都在里面）")

    report = work_dir / "exe-self-test.txt"
    environment = dict(os.environ)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    started = time.monotonic()
    completed = subprocess.run(
        [str(exe), "--self-test", str(report)],
        cwd=str(exe.parent),
        capture_output=True,
        timeout=180,
        env=environment,
    )
    elapsed = time.monotonic() - started
    check(completed.returncode == 0, f"exe 自检退出码为 0（实际 {completed.returncode}）")
    check(report.exists(), f"exe 写出了自检报告（耗时 {elapsed:.1f} 秒）")
    if report.exists():
        text = report.read_text(encoding="utf-8")
        check("打包运行：是" in text, "报告里确认是打包运行（is_frozen 生效）")
        check("自检结果：成功" in text, "报告里自检结果是成功")
        check(APP_VERSION in text, f"报告里的版本号是 {APP_VERSION}")
        check("任务列表列数：9" in text, "打包后的主窗口是 9 列（阶段八的新列都在）")
        check(str(exe.parent) in text, "报告里的程序目录就是 exe 所在目录")
        created_config = exe.parent / "config" / "config.json"
        check(created_config.exists(), "exe 首次运行会自己在程序目录下建 config/config.json")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main() -> int:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    work_dir = PROJECT_ROOT / "Data" / f"_verify_stage8_{stamp}"
    work_dir.mkdir(parents=True, exist_ok=True)
    print("=" * 72)
    print("SmartPrint 阶段八验收（界面完善与打包）")
    print(f"工作目录：{work_dir}")
    print("=" * 72)

    database: Database | None = None
    try:
        check_version(work_dir)
        check_flip_direction(work_dir)
        check_config_save(work_dir)
        check_default_config_file(work_dir)
        check_backup(work_dir)
        check_archive(work_dir)
        check_log_filter(work_dir)
        check_diagram_text(work_dir)
        check_paths(work_dir)
        check_cli(work_dir)
        check_package(work_dir)

        # 检查 9 需要一个干净的数据库，放在最后建，避免影响前面的备份/归档断言
        anomaly_dir = work_dir / "anomaly"
        (anomaly_dir / "database").mkdir(parents=True, exist_ok=True)
        database = Database(anomaly_dir / "database" / "smartprint.db")
        database.initialize()
        repository = TaskRepository(database)
        check_anomaly_query(work_dir, repository)
    finally:
        if database is not None:
            del database
        shutil.rmtree(work_dir, ignore_errors=True)

    passed = sum(1 for ok, _ in results if ok)
    total = len(results)
    print("\n" + "=" * 72)
    if passed == total:
        print(f"阶段八验收：{passed}/{total} 项通过")
    else:
        print(f"阶段八验收：{passed}/{total} 项通过，{total - passed} 项失败")
        for ok, description in results:
            if not ok:
                print(f"  - 失败：{description}")
    print("=" * 72)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
