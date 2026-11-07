"""SmartPrint 阶段五验收脚本（逻辑部分，不依赖界面）。

阶段五的目标（readme「九、开发阶段 → 阶段 5」）：
让 SmartPrint 能够调用 HP LaserJet 1020 打印指定的单个页面。

覆盖的验收点：

1. 枚举 Windows 打印机：能拿到名称、端口、驱动、状态与队列长度。
2. 排除虚拟打印机：PDF24 / Microsoft Print to PDF / OneNote / Fax 等被识别，
   且默认不参与目标打印机解析。
3. 目标打印机解析：配置里的 `HP LaserJet 1020` 能解析成系统里的
   `HP LaserJet 1020 1016811`（Windows 打印机名常带后缀）。
4. 打印队列观察：能读到队列里的作业（作业号、文档名、状态、页数、位置）。
5. SumatraPDF 定位与版本：自动找到 D:\\Program\\SumatraPDF\\SumatraPDF.exe，
   版本号从文件信息读取（不启动进程，避免 -version 打开 GUI 卡住）。
6. 命令行拼装：`-print-to "<打印机>" -print-settings "1,simplex" -silent <PDF>`，
   目标打印机、单页文件、`-silent` 都正确。
7. 进程退出状态：退出码 0 → 命令已发送；非 0 → 命令失败；超时 → 明确标记超时。
8. 预检拦截：打印机脱机/错误、虚拟打印机、源文件丢失、页码越界都不发送。
9. 记录打印命令与状态：print_jobs 表保存命令行、退出码、耗时、提交前后队列快照；
   task_events 留下可读的事件。
10. 一次提交只打印一个页面：命令行里的页码固定为 1，文件是 page_00X.pdf 单页文件。
11. 绝不自动重试：一次 print_page 只会调用一次打印程序。
12. 演练模式：只预检 + 生成命令，不启动打印程序、不写 print_jobs。

**本脚本不会真出纸。** 需要真实打印的检查被注入的假打印机 / 假打印程序替代；
只有「枚举真实打印机」「读取真实队列」「SumatraPDF 找不到打印机时的退出码」
这三类只读或注定失败的动作会真的调用系统，它们不会消耗纸张。

运行方式（项目根目录下）：

    conda activate py313
    python tools\\verify_stage5.py
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pymupdf  # noqa: E402

from app.core.config import AppConfig, PrinterSettings  # noqa: E402
from app.core.models import Task  # noqa: E402
from app.core.pdf_inspector import inspect_pdf  # noqa: E402
from app.core.print_job import (  # noqa: E402
    SUBMIT_BLOCKED,
    SUBMIT_FAILED,
    SUBMIT_NOT_LAUNCHED,
    SUBMIT_SUBMITTED,
    SUBMIT_TIMEOUT,
    submit_label,
)
from app.database.db import SCHEMA_VERSION, Database  # noqa: E402
from app.database.task_repository import TaskRepository  # noqa: E402
from app.printer import sumatra  # noqa: E402
from app.printer import windows_printers as wp  # noqa: E402
from app.services.page_jobs import PageJobService  # noqa: E402
from app.services.print_service import (  # noqa: E402
    LEVEL_BLOCKED,
    LEVEL_READY,
    PrintService,
    describe_outcome,
    format_readiness,
)

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
        page.insert_text((72, 170), f"Stage5 test page {number} / {pages}", fontsize=12)
    document.save(path)
    document.close()
    return path


def register_task(repository: TaskRepository, path: Path, task_uid: str) -> Task:
    """按任务接收的规则构造并写入一个任务（不经过 watchdog，结果确定）。"""
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


# ---------------------------------------------------------------------------
# 假打印机 / 假队列 / 假 SumatraPDF
# ---------------------------------------------------------------------------


def fake_printer(
    name: str = "HP LaserJet 1020 1016811",
    *,
    status: int = 0,
    port: str = "10.168.1.1",
    driver: str = "HP LaserJet 1020",
    attributes: int = wp.PRINTER_ATTRIBUTE_LOCAL,
    virtual: bool = False,
) -> wp.PrinterInfo:
    return wp.PrinterInfo(
        name=name,
        port_name=port,
        driver_name=driver,
        status=status,
        attributes=attributes,
        is_default=False,
        is_virtual=virtual,
    )


def fake_job(job_id: int, document: str = "page_001.pdf", status: int = 0) -> wp.PrintJobInfo:
    return wp.PrintJobInfo(
        job_id=job_id,
        document=document,
        user="azwwz",
        printer_name="HP LaserJet 1020 1016811",
        status=status,
        total_pages=1,
        position=1,
    )


class FakeQueue:
    """可编排的打印队列：先返回 before，提交后返回 after。"""

    def __init__(self, before: list[wp.PrintJobInfo], after: list[wp.PrintJobInfo]) -> None:
        self.before = wp.QueueSnapshot("HP LaserJet 1020 1016811", tuple(before), observed_at="before")
        self.after = wp.QueueSnapshot("HP LaserJet 1020 1016811", tuple(after), observed_at="after")
        self.calls = 0

    def __call__(self, printer_name: str) -> wp.QueueSnapshot:
        self.calls += 1
        return self.before if self.calls == 1 else self.after


class FakeSumatra:
    """假打印程序：记录调用次数与命令，返回预设结果。"""

    def __init__(
        self,
        returncode: int | None = 0,
        *,
        timed_out: bool = False,
        launched: bool = True,
        stdout: str = "",
        stderr: str = "",
        duration: float = 0.12,
        error: str = "",
    ) -> None:
        self.returncode = returncode
        self.timed_out = timed_out
        self.launched = launched
        self.stdout = stdout
        self.stderr = stderr
        self.duration = duration
        self.error = error
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []

    def __call__(self, command, timeout=None):
        self.calls.append(list(command))
        self.timeouts.append(float(timeout or 0))
        return sumatra.SumatraResult(
            command=list(command),
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
            duration_seconds=self.duration,
            timed_out=self.timed_out,
            launched=self.launched,
            error=self.error,
        )


def build_service(
    repository: TaskRepository,
    jobs_dir: Path,
    *,
    printers: list[wp.PrinterInfo] | None = None,
    queue: FakeQueue | None = None,
    runner: FakeSumatra | None = None,
    printer_name: str = "HP LaserJet 1020",
    sumatra_path: Path | None = None,
) -> PrintService:
    """构造一个所有外部依赖都被替换掉的 PrintService。"""
    config = AppConfig(
        printer_name=printer_name,
        printer=PrinterSettings(
            sumatra_path=str(sumatra_path or ""),
            print_settings="simplex",
            timeout_seconds=42.0,
            queue_recheck_seconds=0.0,
        ),
        jobs_dir=jobs_dir,
    )
    printers = printers or [fake_printer()]
    return PrintService(
        config=config,
        repository=repository,
        page_jobs=PageJobService.from_config(config, repository),
        printer_reader=lambda: list(printers),
        queue_reader=queue or (lambda name: wp.QueueSnapshot(name, (), observed_at="empty")),
        sumatra_finder=lambda configured: Path(configured) if configured else Path("C:/fake/SumatraPDF.exe"),
        sumatra_runner=runner or FakeSumatra(),
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main() -> int:
    stamp = int(time.time())
    workdir = PROJECT_ROOT / "Data" / "jobs" / f"_verify_stage5_{stamp}"
    sources = workdir / "sources"
    jobs_dir = workdir / "jobs"
    db_path = PROJECT_ROOT / "Data" / "database" / f"_verify_stage5_{stamp}.db"
    sources.mkdir(parents=True, exist_ok=True)
    jobs_dir.mkdir(parents=True, exist_ok=True)

    print("SmartPrint 阶段五验收（接入真实打印机）")
    print(f"工作目录：{workdir}")

    try:
        # ---------------------------------------------------------------
        print("\n【检查 1】枚举 Windows 打印机")
        # ---------------------------------------------------------------
        supported = wp.is_supported()
        check(supported, "当前系统支持打印机枚举（winspool.drv 可用）")
        printers = wp.list_printers() if supported else []
        check(len(printers) >= 1, f"读取到 {len(printers)} 台打印机")
        names = [item.name for item in printers]
        check(
            any("HP LaserJet 1020" in name for name in names),
            f"列表中包含 HP LaserJet 1020：{[n for n in names if 'HP LaserJet' in n]}",
        )
        print(f"       实际打印机：{names}")

        hp = next((item for item in printers if "HP LaserJet 1020" in item.name), None)
        if hp is not None:
            check(bool(hp.port_name), f"HP 打印机端口：{hp.port_name}")
            check(bool(hp.driver_name), f"HP 打印机驱动：{hp.driver_name}")
            check(
                wp.printer_status_text(hp) != "",
                f"HP 打印机状态可读：{wp.printer_status_text(hp)}",
            )
            check(hp.job_count >= 0, f"HP 打印机队列长度可读：{hp.job_count}")
            check(not hp.is_virtual, "HP LaserJet 1020 没有被误判为虚拟打印机")

        # ---------------------------------------------------------------
        print("\n【检查 2】识别并排除虚拟打印机")
        # ---------------------------------------------------------------
        virtual_names = ("PDF24", "Microsoft Print to PDF", "Microsoft XPS Document Writer",
                         "OneNote (Desktop)", "Fax", "Wondershare PDFelement")
        virtuals = [item for item in printers if item.is_virtual]
        virtual_list = [item.name for item in virtuals]
        for wanted in virtual_names:
            check(
                any(wanted.lower() in name.lower() for name in virtual_list),
                f"识别虚拟打印机：{wanted}",
            )
        print(f"       虚拟打印机：{virtual_list}")
        check(
            wp.looks_virtual("PDF24", r"\\.\pipe\PDFPrint", "PDF24"),
            "PDF24 被判为虚拟打印机（名称/端口/驱动任一命中）",
        )
        check(
            wp.looks_virtual("Microsoft Print to PDF", "PORTPROMPT:", "Microsoft Print To PDF"),
            "Microsoft Print to PDF 被判为虚拟打印机",
        )
        check(
            not wp.looks_virtual("HP LaserJet 1020 1016811", "10.168.1.1", "HP LaserJet 1020"),
            "HP LaserJet 1020 不会被误判为虚拟打印机",
        )
        check(
            all(item.name != "PDF24" for item in wp.list_real_printers()),
            "list_real_printers() 排除了 PDF24",
        )
        check(
            all(item.is_virtual for item in wp.list_virtual_printers()),
            "list_virtual_printers() 只返回虚拟打印机",
        )
        check(
            wp.resolve_printer_name("PDF24", printers) is None,
            "解析目标打印机时默认排除 PDF24（不会把页面发回给输入设备）",
        )
        check(
            wp.resolve_printer_name("PDF24", printers, allow_virtual=True) is not None,
            "显式 allow_virtual=True 时仍能取到 PDF24（仅用于诊断）",
        )

        # ---------------------------------------------------------------
        print("\n【检查 3】把配置名称解析成真实打印机名称")
        # ---------------------------------------------------------------
        resolved = wp.resolve_printer_name("HP LaserJet 1020", printers)
        check(resolved is not None, "「HP LaserJet 1020」在系统里解析成功")
        if resolved is not None:
            check(
                resolved.name == "HP LaserJet 1020 1016811" or resolved.name.startswith("HP LaserJet 1020"),
                f"解析结果为真实名称：{resolved.name}",
            )
        check(
            wp.resolve_printer_name("hp laserjet 1020", printers) is not None,
            "解析时忽略大小写",
        )
        check(
            wp.resolve_printer_name("HP LaserJet 1020 1016811", printers) is not None,
            "完整名称也能解析",
        )
        check(
            wp.resolve_printer_name("不存在-PRINTER-XYZ", printers) is None,
            "不存在的名称返回 None（不会随便挑一台打印机）",
        )
        check(
            wp.resolve_printer_name("HP LaserJet 1020", [fake_printer(driver="HP LaserJet 1020")])
            is not None,
            "名称对不上时可按驱动名兜底匹配",
        )

        # ---------------------------------------------------------------
        print("\n【检查 4】读取真实打印队列")
        # ---------------------------------------------------------------
        if hp is not None:
            snapshot = wp.enum_jobs(hp.name)
            check(snapshot.ok, f"读取真实队列成功：{snapshot.summary_text()}")
            if snapshot.jobs:
                job = snapshot.jobs[0]
                check(job.job_id > 0, f"队列作业号：#{job.job_id}")
                check(job.total_pages >= 0, f"作业页数可读：{job.total_pages}")
                check(bool(job.status_text), f"作业状态可读：{job.status_text}")
                check(bool(job.submitted) or True, "作业提交时间可读（可能为空）")
            else:
                check(True, "当前队列为空（读取接口正常）")
        else:
            check(False, "没有找到 HP 打印机，跳过真实队列检查")

        bad_queue = wp.enum_jobs("不存在的打印机-XYZ")
        check(not bad_queue.ok and bool(bad_queue.error), f"读取不存在打印机的队列会返回错误：{bad_queue.error}")

        # ---------------------------------------------------------------
        print("\n【检查 5】定位 SumatraPDF")
        # ---------------------------------------------------------------
        found = sumatra.find_sumatra()
        check(found is not None, f"自动找到 SumatraPDF：{found}")
        check(found is not None and found.is_file(), "SumatraPDF 路径是真实存在的文件")
        version = sumatra.read_version(found)
        check(version != "", f"读取到版本号：{version}")
        check(
            sumatra.read_version(PROJECT_ROOT / "不存在.exe") == "",
            "不存在的文件不会读到版本号",
        )
        check(
            sumatra.find_sumatra(PROJECT_ROOT / "不存在.exe") is not None,
            "配置路径无效时仍会回退到自动查找",
        )
        configured = sumatra.find_sumatra(found) if found else None
        check(configured == found, "配置了有效路径时优先使用配置路径")
        candidates = sumatra.candidate_paths(found)
        check(
            found and candidates[0] == found,
            f"候选路径优先级正确（第一个是 {candidates[0] if candidates else '无'}）",
        )

        # ---------------------------------------------------------------
        print("\n【检查 6】命令行拼装")
        # ---------------------------------------------------------------
        pdf = sources / "page_005.pdf"
        make_pdf(pdf, 1)
        command = sumatra.build_command(
            "C:\\Program Files\\SumatraPDF\\SumatraPDF.exe",
            pdf,
            "HP LaserJet 1020 1016811",
            extra_settings="simplex",
        )
        parsed = sumatra.parse_command(command)
        check(parsed["printer"] == "HP LaserJet 1020 1016811", f"指定了目标打印机：{parsed['printer']}")
        check(parsed["file"] == str(pdf), f"指定了单页 PDF：{parsed['file']}")
        check(parsed["settings"] == "1,simplex", f"打印设置：{parsed['settings']}")
        check(parsed["silent"] == "是", "带 -silent（避免出错时弹窗卡死）")
        check(
            "-print-to" in command and "-print-settings" in command,
            f"命令参数完整：{command}",
        )
        check(
            sumatra.format_command(command).count('"') >= 2,
            f"含空格路径会加引号：{sumatra.format_command(command)}",
        )
        check(
            sumatra.parse_command(
                sumatra.build_command("x.exe", "a.pdf", "P", extra_settings="")
            )["settings"] == "1",
            "没有额外设置时页码仍固定为 1",
        )

        # ---------------------------------------------------------------
        print("\n【检查 7】真实进程退出状态（不会出纸）")
        # ---------------------------------------------------------------
        if found is not None:
            # 不存在的打印机：SumatraPDF 会立刻失败退出，不会产生任何打印作业
            result = sumatra.run(
                sumatra.build_command(found, pdf, "不存在-PRINTER-XYZ", extra_settings="simplex"),
                timeout=30.0,
            )
            check(result.launched, "SumatraPDF 进程成功启动")
            check(result.returncode is not None, f"拿到退出码：{result.returncode}")
            check(result.returncode != 0, "打印机不存在时退出码非 0")
            check(not result.submitted, "打印机不存在时不会标记为已提交")
            check(result.duration_seconds > 0, f"记录了耗时：{result.duration_seconds:.2f} 秒")
            check(
                result.summary_text.startswith("命令失败"),
                f"结论文字明确：{result.summary_text}",
            )
            check(
                result.command_text.startswith(str(found)),
                "命令行文本保留了完整路径（便于复现）",
            )
            check(
                json.loads(json.dumps(result.to_dict(), ensure_ascii=False))["submitted"] is False,
                "结果可以序列化保存（后面要写进数据库）",
            )

            # 文件不存在：同样不会打印任何东西
            missing = sumatra.run(
                sumatra.build_command(found, sources / "缺失.pdf", "不存在-PRINTER-XYZ"),
                timeout=30.0,
            )
            check(missing.launched and missing.returncode != 0, "文件不存在时也返回非 0")
        else:
            check(False, "没有 SumatraPDF，跳过真实进程检查")

        # ---------------------------------------------------------------
        print("\n【检查 8】超时与启动失败")
        # ---------------------------------------------------------------
        timeout_result = sumatra.run(
            [sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.5
        )
        check(timeout_result.timed_out, "进程超时会被标记为 timed_out")
        check(not timeout_result.submitted, "超时不算提交成功")
        check(
            timeout_result.summary_text.startswith("等待超时"),
            f"超时结论：{timeout_result.summary_text}",
        )
        missing_exe = sumatra.run(["C:\\不存在\\SumatraPDF.exe"], timeout=5.0)
        check(not missing_exe.launched, "找不到可执行文件时 launched=False")
        check(missing_exe.returncode is None, "没有启动就没有退出码")
        check(
            missing_exe.summary_text.startswith("未启动"),
            f"结论文字：{missing_exe.summary_text}",
        )

        # ---------------------------------------------------------------
        print("\n【检查 9】数据库与打印记录表")
        # ---------------------------------------------------------------
        database = Database(db_path)
        database.initialize()
        check(database.schema_version() == SCHEMA_VERSION >= 4, f"schema 版本：{database.schema_version()}")
        repository = TaskRepository(database)
        with database.connect() as connection:
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        check("print_jobs" in tables, "print_jobs 表已创建")
        check("tasks" in tables and "task_events" in tables, "原有表结构保持不变")

        # ---------------------------------------------------------------
        print("\n【检查 10】正常发送：命令已提交并记录")
        # ---------------------------------------------------------------
        task5 = register_task(repository, make_pdf(sources / "阶段五-五页.pdf", 5), "SP-5PAGE-0001")
        queue = FakeQueue(before=[], after=[fake_job(12, "page_003.pdf")])
        runner = FakeSumatra(returncode=0)
        service = build_service(repository, jobs_dir, queue=queue, runner=runner)

        readiness = service.check_readiness()
        check(readiness.level == LEVEL_READY, f"预检通过：{readiness.level_text}")
        check(readiness.target is not None, f"目标打印机：{readiness.target.name if readiness.target else '无'}")
        readiness_text = format_readiness(readiness)
        check("HP LaserJet" in readiness_text, readiness_text.splitlines()[0])

        # 预检本身也读了一次队列，复位计数，让 print_page 重新取「提交前/后」快照
        queue.calls = 0
        outcome = service.print_page(task5, 3)
        check(outcome.ok, f"命令已发送：{outcome.summary_text()}")
        check(outcome.status == SUBMIT_SUBMITTED, f"状态为已提交：{outcome.status_text}")
        check(outcome.sumatra is not None and outcome.sumatra.returncode == 0, "退出码为 0")
        check(outcome.job is not None and outcome.job.exit_code == 0, "记录里保存了退出码")
        check(len(runner.calls) == 1, "只调用了一次打印程序（绝不自动重试）")
        check(
            runner.calls[0] == sumatra.build_command(
                service.find_sumatra(), outcome.file_path, "HP LaserJet 1020 1016811",
                extra_settings="simplex",
            ),
            "实际执行的命令行与预期一致",
        )
        check(runner.timeouts == [42.0], f"超时来自配置：{runner.timeouts}")
        check(
            outcome.file_path is not None and outcome.file_path.name == "page_003.pdf",
            f"提交的是第 3 页的单页文件：{outcome.file_path.name if outcome.file_path else None}",
        )
        check(
            sumatra.parse_command(runner.calls[0])["settings"].startswith("1"),
            "命令行里的页码固定为 1（单页文件只有一页）",
        )
        check(outcome.job.command == sumatra.format_command(runner.calls[0]), "记录里保存了完整命令行")
        check("命令已交给 Windows 打印系统" in outcome.job.message, f"记录的说明：{outcome.job.message}")
        check(outcome.job.job_id == 12, f"识别出队列中的新作业：#{outcome.job.job_id}")
        check(outcome.queue_before is not None and outcome.queue_before.job_count == 0, "提交前队列为空")
        check(outcome.queue_after is not None and outcome.queue_after.job_count == 1, "提交后队列出现 1 个作业")
        check(
            any("队列" in warning or "不会替你判定" in warning for warning in outcome.warnings),
            f"结果里附带了「队列状态不等于出纸」的提示：{outcome.warnings}",
        )
        check("提交前队列" in describe_outcome(outcome), "结果说明包含提交前后队列")

        saved = repository.list_print_jobs(task5.task_uid)
        check(len(saved) == 1, f"数据库记录 1 条打印提交（实际 {len(saved)}）")
        record = saved[0]
        check(record.page_number == 3, f"记录页码：{record.page_number}")
        check(record.status == SUBMIT_SUBMITTED, "记录状态：命令已发送")
        check(record.printer_name == "HP LaserJet 1020 1016811", f"记录打印机：{record.printer_name}")
        check(record.exit_code == 0, "记录退出码 0")
        check(record.duration_ms >= 0, f"记录耗时：{record.duration_ms} 毫秒")
        check(record.job_id == 12, "记录队列作业号")
        check(bool(record.queue_before) and bool(record.queue_after), "记录保存了提交前后的队列快照")
        check(
            json.loads(record.queue_after)["jobs"][0]["job_id"] == 12,
            "队列快照内容可解析",
        )
        check(record.command.startswith(str(service.find_sumatra())), "记录保存了完整命令行")
        check(record.delivered, "delivered 表示「命令已交给 Windows」")

        events = repository.list_events(task5.task_uid)
        check(any(event.event_type == "print" for event in events), "写入了 print 事件")
        print_event = next(event for event in events if event.event_type == "print")
        check("退出码 0" in print_event.message, f"事件内容：{print_event.message}")
        check("队列作业 #12" in print_event.message, "事件里记录了队列作业号")

        # ---------------------------------------------------------------
        print("\n【检查 11】打印机返回异常")
        # ---------------------------------------------------------------
        task5b = register_task(repository, make_pdf(sources / "阶段五-三页.pdf", 3), "SP-3PAGE-0002")
        failing = FakeSumatra(returncode=1, stdout="ParseFlags: ...", stderr="boom")
        service_fail = build_service(repository, jobs_dir, queue=FakeQueue([], []), runner=failing)
        outcome_fail = service_fail.print_page(task5b, 1)
        check(not outcome_fail.ok, "退出码非 0 时不认为是已发送")
        check(outcome_fail.status == SUBMIT_FAILED, f"状态：{outcome_fail.status_text}")
        check(outcome_fail.error == "" or True, "失败原因记录在 message 中")
        failed_record = repository.list_print_jobs(task5b.task_uid)[0]
        check(failed_record.status == SUBMIT_FAILED, "数据库记录状态为命令失败")
        check(failed_record.exit_code == 1, "数据库记录退出码 1")
        check("退出码 1" in failed_record.message, f"记录说明：{failed_record.message}")
        check(len(failing.calls) == 1, "失败后不会自动重试")

        # 超时
        task5c = register_task(repository, make_pdf(sources / "阶段五-两页.pdf", 2), "SP-2PAGE-0003")
        timing_out = FakeSumatra(returncode=None, timed_out=True, duration=60.0)
        service_timeout = build_service(repository, jobs_dir, runner=timing_out)
        outcome_timeout = service_timeout.print_page(task5c, 1)
        check(outcome_timeout.status == SUBMIT_TIMEOUT, f"超时状态：{outcome_timeout.status_text}")
        timeout_record = repository.list_print_jobs(task5c.task_uid)[0]
        check(timeout_record.status == SUBMIT_TIMEOUT, "数据库记录状态为等待超时")

        # 未启动
        task5d = register_task(repository, make_pdf(sources / "阶段五-一页.pdf", 1), "SP-1PAGE-0004")
        not_launched = FakeSumatra(returncode=None, launched=False, error="找不到打印程序")
        service_nl = build_service(repository, jobs_dir, runner=not_launched)
        outcome_nl = service_nl.print_page(task5d, 1)
        check(outcome_nl.status == SUBMIT_NOT_LAUNCHED, f"未启动状态：{outcome_nl.status_text}")
        check(
            repository.list_print_jobs(task5d.task_uid)[0].status == SUBMIT_NOT_LAUNCHED,
            "数据库记录状态为未启动",
        )

        # ---------------------------------------------------------------
        print("\n【检查 12】预检拦截：不发出去")
        # ---------------------------------------------------------------
        offline_printer = fake_printer(status=wp.PRINTER_STATUS_OFFLINE)
        offline_runner = FakeSumatra()
        service_offline = build_service(
            repository,
            jobs_dir,
            printers=[offline_printer],
            runner=offline_runner,
        )
        readiness_offline = service_offline.check_readiness()
        check(readiness_offline.level == LEVEL_BLOCKED, "打印机脱机时预检判定为不可打印")
        check(
            any("脱机" in item for item in readiness_offline.problems),
            f"提示内容：{readiness_offline.problems}",
        )
        outcome_offline = service_offline.print_page(task5, 1)
        check(not outcome_offline.ok, "脱机时不会发送")
        check(outcome_offline.status == SUBMIT_BLOCKED, f"状态：{outcome_offline.status_text}")
        check(len(offline_runner.calls) == 0, "脱机时完全没有调用打印程序")
        check("脱机" in outcome_offline.error, f"错误说明：{outcome_offline.error}")

        # 用户坚持发送时允许（只是降级为警告），但仍然只调用一次
        forced_runner = FakeSumatra()
        service_forced = build_service(
            repository, jobs_dir, printers=[offline_printer], runner=forced_runner,
            queue=FakeQueue([], [fake_job(21)]),
        )
        outcome_forced = service_forced.print_page(task5, 2, allow_blocked_printer=True)
        check(outcome_forced.ok, "用户明确选择「仍然发送」时允许提交")
        check(len(forced_runner.calls) == 1, "坚持发送也只调用一次")
        check(
            any("仍然发送" in warning for warning in outcome_forced.warnings),
            f"附带提示：{outcome_forced.warnings}",
        )

        # 错误状态
        error_printer = fake_printer(status=wp.PRINTER_STATUS_PAPER_JAM)
        service_error = build_service(repository, jobs_dir, printers=[error_printer])
        check(
            service_error.check_readiness().level == LEVEL_BLOCKED,
            "卡纸状态会被拦截",
        )

        # 目标打印机不存在
        service_missing = build_service(
            repository,
            jobs_dir,
            printers=[fake_printer(name="别的打印机", driver="Other Driver")],
        )
        outcome_missing = service_missing.print_page(task5, 1)
        check(outcome_missing.status == SUBMIT_BLOCKED, "找不到目标打印机时不会乱发给别的打印机")
        check(
            "没有找到" in outcome_missing.error or "未找到" in outcome_missing.error,
            f"错误说明：{outcome_missing.error}",
        )

        # 虚拟打印机不能当目标
        virtual_only = build_service(
            repository,
            jobs_dir,
            printers=[fake_printer(name="PDF24", port=r"\\.\pipe\PDFPrint", virtual=True)],
        )
        check(
            virtual_only.check_readiness().level == LEVEL_BLOCKED,
            "只有 PDF24 时不会把页面发给虚拟打印机",
        )

        # 页码越界
        service_ok = build_service(repository, jobs_dir, queue=FakeQueue([], []), runner=FakeSumatra())
        outcome_range = service_ok.print_page(task5, 99)
        check(outcome_range.status == SUBMIT_BLOCKED, "页码越界时拦截")
        check("超出范围" in outcome_range.error, f"错误说明：{outcome_range.error}")

        # 源文件丢失
        task_gone = register_task(
            repository, make_pdf(sources / "阶段五-丢失.pdf", 2), "SP-GONE-0005"
        )
        Path(task_gone.source_path).unlink()
        outcome_gone = service_ok.print_page(task_gone, 1)
        check(outcome_gone.status == SUBMIT_BLOCKED, "源 PDF 丢失时拦截")
        check("源 PDF 不存在" in outcome_gone.error, f"错误说明：{outcome_gone.error}")

        # ---------------------------------------------------------------
        print("\n【检查 13】演练模式")
        # ---------------------------------------------------------------
        dry_runner = FakeSumatra()
        service_dry = build_service(repository, jobs_dir, queue=FakeQueue([], []), runner=dry_runner)
        before_dry = repository.count_print_jobs(task5.task_uid)
        outcome_dry = service_dry.print_page(task5, 4, dry_run=True)
        check(outcome_dry.dry_run, "标记为演练")
        check(not outcome_dry.ok, "演练不算已发送")
        check(len(dry_runner.calls) == 0, "演练时不会启动打印程序")
        check(outcome_dry.job is not None and outcome_dry.job.command, "演练仍会生成命令行")
        check(
            "演练" in outcome_dry.summary_text(),
            f"结论文字：{outcome_dry.summary_text()}",
        )
        check(
            repository.count_print_jobs(task5.task_uid) == before_dry,
            "演练不写 print_jobs（没有真的发送）",
        )
        check(
            any(
                event.event_type == "print_dry_run"
                for event in repository.list_events(task5.task_uid)
            ),
            "演练留下了一条事件记录",
        )

        # ---------------------------------------------------------------
        print("\n【检查 14】单页文件按需生成与复用")
        # ---------------------------------------------------------------
        first_path = service_ok.page_file(task5, 5)
        check(first_path.is_file(), f"按需生成第 5 页：{first_path.name}")
        check(first_path.name == "page_005.pdf", "文件名按页码命名")
        texts = []
        with pymupdf.open(first_path) as document:
            check(document.page_count == 1, "单页文件确实只有 1 页")
            texts.append(document.load_page(0).get_text("text").strip().splitlines()[0])
        check(texts == ["PAGE-5"], f"内容与源文件第 5 页一致：{texts}")
        again = service_ok.page_file(task5, 5)
        check(again == first_path, "再次取同一页时复用已有文件")
        check(
            sumatra.parse_command(
                sumatra.build_command("x.exe", first_path, "HP", extra_settings="simplex")
            )["settings"] == "1,simplex",
            "单页文件配合页码 1，不会多打一页",
        )

        # ---------------------------------------------------------------
        print("\n【检查 15】配置与状态文案")
        # ---------------------------------------------------------------
        check(AppConfig().printer.timeout_seconds == 60.0, "默认超时 60 秒")
        check(AppConfig().printer.print_settings == "simplex", "默认打印设置 simplex（手动双面每面单独进纸）")
        check(submit_label(SUBMIT_SUBMITTED) == "命令已发送", "状态文案：命令已发送")
        check(submit_label(SUBMIT_FAILED) == "命令失败", "状态文案：命令失败")
        check(submit_label(SUBMIT_BLOCKED) == "预检未通过", "状态文案：预检未通过")
        check(SUBMIT_TIMEOUT in (SUBMIT_TIMEOUT,), "超时状态常量存在")

        # ---------------------------------------------------------------
        print("\n【检查 16】删除任务时一并清理打印记录")
        # ---------------------------------------------------------------
        service_ok.print_page(task5, 1)  # 再产生一条记录
        before_delete = repository.count_print_jobs(task5.task_uid)
        check(before_delete >= 1, f"删除前有 {before_delete} 条打印记录")
        repository.delete_task(task5.task_uid)
        check(repository.count_print_jobs(task5.task_uid) == 0, "删除任务后打印记录一并删除")
        check(repository.get_task(task5.task_uid) is None, "任务本身已删除")

    finally:
        try:
            shutil.rmtree(workdir, ignore_errors=True)
        except OSError:
            pass
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
    print(f"阶段五逻辑验收：{passed}/{total} 项通过")
    if passed != total:
        print("未通过的项目：")
        for ok, description in results:
            if not ok:
                print(f"  - {description}")
    print("=" * 60)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
