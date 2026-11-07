"""SmartPrint 阶段四验收脚本（逻辑部分，不依赖界面）。

阶段四的目标（readme「九、开发阶段 → 阶段四」）：
让 SmartPrint 能够根据物理纸张关系，正确生成每一次需要打印的**单页 PDF**。

覆盖的验收点：

1. 纸张与批次计算：1 / 2 / 3 / 9 / 10 / 11 / 37 页的纸张数、批次数、正反面页码。
2. 最后一批不足一整批时按实际张数计算（11 页 → 第 2 批只有 1 张纸）。
3. 每个页面各生成一个单页 PDF（page_001.pdf、page_003.pdf…），每个文件只有 1 页。
4. 单页 PDF 的内容与原始 PDF 的对应页面完全一致（文字、尺寸、旋转）。
5. 最后一张纸只有正面时，不会凭空生成背面页面。
6. 不同页面尺寸的 PDF 也能正确处理。
7. 原始 PDF 不被修改（生成前后 SHA-256 与修改时间都不变）。
8. 页数变化后重新生成：旧的单页文件被清理，数据库记录被整体替换。
9. 批次写入数据库（task_batches）并记录事件、写入 plan.json。
10. 每批纸张数量可配置（sheets_per_batch=3），背面降序规则可用。
11. 空白页、源文件丢失、页码非法等异常给出明确提示。

本脚本**不接触真实打印机**：只用临时目录与临时数据库，跑完自动清理。

运行方式（项目根目录下）：

    conda activate py313
    python tools\\verify_stage4.py
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

from app.core.models import Task  # noqa: E402
from app.core.page_planner import (  # noqa: E402
    BACK_ORDER_DESCENDING,
    PlanError,
    page_file_name,
    plan_pages,
)
from app.core.pdf_inspector import inspect_pdf  # noqa: E402
from app.core.pdf_splitter import (  # noqa: E402
    PdfSplitError,
    extract_pages,
    file_sha256,
    scan_pages,
    verify_pages,
)
from app.database.db import SCHEMA_VERSION, Database  # noqa: E402
from app.database.task_repository import TaskRepository  # noqa: E402
from app.services.page_jobs import PageJobError, PageJobService  # noqa: E402

A4_WIDTH_PT = 595.28
A4_HEIGHT_PT = 841.89
A5_WIDTH_PT = 419.53
A5_HEIGHT_PT = 595.28

SHEETS_PER_BATCH = 5

results: list[tuple[bool, str]] = []


def back_sequence(values: tuple[int, ...]) -> tuple[int, ...]:
    """正面/背面顺序由配置决定（默认背面升序，逆序需求落地后为降序）。

    断言时按配置「归一化」成同一形状，这样两种默认值下都能跑通，
    具体选哪一种由 app/core/page_planner.py 的 DEFAULT_BACK_ORDER 决定。
    """
    if list(values) == sorted(values, reverse=True):
        return tuple(sorted(values))
    return values


def check(condition: bool, description: str) -> bool:
    results.append((bool(condition), description))
    print(f"  [{'PASS' if condition else 'FAIL'}] {description}")
    return bool(condition)


def make_pdf(
    path: Path,
    pages: int,
    *,
    blank_pages: tuple[int, ...] = (),
    mixed_size_pages: tuple[int, ...] = (),
) -> Path:
    """生成一份带页码文字的测试 PDF（每页写着 PAGE-<页码>）。"""
    document = pymupdf.open()
    for index in range(pages):
        number = index + 1
        if number in mixed_size_pages:
            page = document.new_page(width=A5_WIDTH_PT, height=A5_HEIGHT_PT)
        else:
            page = document.new_page(width=A4_WIDTH_PT, height=A4_HEIGHT_PT)
        if number in blank_pages:
            continue
        page.insert_text((72, 120), f"PAGE-{number}", fontsize=32)
        page.insert_text((72, 170), f"Stage4 test page {number} / {pages}", fontsize=12)
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


def read_texts(path: Path) -> list[str]:
    """读出 PDF 每页开头的文字，例如 ['PAGE-1']。"""
    texts: list[str] = []
    with pymupdf.open(path) as document:
        for index in range(document.page_count):
            raw = document.load_page(index).get_text("text")
            first = next((line.strip() for line in raw.splitlines() if line.strip()), "")
            texts.append(first)
    return texts


def page_count_of(path: Path) -> int:
    with pymupdf.open(path) as document:
        return document.page_count


def expected_texts(pages: tuple[int, ...]) -> list[str]:
    return [f"PAGE-{page}" for page in pages]


def main() -> int:
    stamp = int(time.time())
    workdir = PROJECT_ROOT / "Data" / "jobs" / f"_verify_stage4_{stamp}"
    sources = workdir / "sources"
    jobs_dir = workdir / "jobs"
    db_path = PROJECT_ROOT / "Data" / "database" / f"_verify_stage4_{stamp}.db"
    sources.mkdir(parents=True, exist_ok=True)
    jobs_dir.mkdir(parents=True, exist_ok=True)

    print("SmartPrint 阶段四验收（PDF 页面处理）")
    print(f"工作目录：{workdir}")

    try:
        # ---------------------------------------------------------------
        print("\n【检查 1】纸张与批次计算：不同总页数")
        # ---------------------------------------------------------------
        expectations = {
            # 页数: (纸张数, 批次数, 每批张数)
            1: (1, 1, [1]),
            2: (1, 1, [1]),
            3: (2, 1, [2]),
            9: (5, 1, [5]),
            10: (5, 1, [5]),
            11: (6, 2, [5, 1]),
            37: (19, 4, [5, 5, 5, 4]),
        }
        for pages, (sheets, batches, sizes) in expectations.items():
            plan = plan_pages(pages, SHEETS_PER_BATCH)
            ok = (
                plan.sheet_count == sheets
                and plan.batch_count == batches
                and [batch.sheet_count for batch in plan.batches] == sizes
            )
            check(
                ok,
                f"{pages} 页 → {plan.sheet_count} 张 / {plan.batch_count} 批 "
                f"{[batch.sheet_count for batch in plan.batches]}"
                f"（期望 {sheets} 张 / {batches} 批 {sizes}）",
            )

        # ---------------------------------------------------------------
        print("\n【检查 2】纸张与页码映射：正面奇数升序、背面偶数（批次内按配置排序）")
        # ---------------------------------------------------------------
        plan9 = plan_pages(9, SHEETS_PER_BATCH)
        check(plan9.front_pages == (1, 3, 5, 7, 9), f"9 页正面：{plan9.front_pages}")
        check(back_sequence(plan9.back_pages) == (2, 4, 6, 8),
              f"9 页背面：{plan9.back_pages}（第 5 张纸没有背面）")
        check(plan9.batch_count == 1, "9 页只需要 1 批（不超过 5 张纸）")
        check(
            plan9.blank_back_sheets == (5,),
            f"9 页时第 5 张纸的背面为空：{plan9.blank_back_sheets}",
        )
        check(
            [(sheet.number, sheet.front_page, sheet.back_page) for sheet in plan9.sheets[:3]]
            == [(1, 1, 2), (2, 3, 4), (3, 5, 6)],
            "纸张编号与正反面页码：纸 1 (1/2)、纸 2 (3/4)、纸 3 (5/6)",
        )

        plan10 = plan_pages(10, SHEETS_PER_BATCH)
        check(back_sequence(plan10.back_pages) == (2, 4, 6, 8, 10),
              f"10 页背面：{plan10.back_pages}")
        check(plan10.blank_back_sheets == (), "10 页时每张纸都有背面内容")

        plan37 = plan_pages(37, SHEETS_PER_BATCH)
        check(
            plan37.batches[-1].front_pages == (31, 33, 35, 37),
            f"37 页第 4 批正面：{plan37.batches[-1].front_pages}",
        )
        check(
            back_sequence(plan37.batches[-1].back_pages) == (32, 34, 36),
            f"37 页第 4 批背面：{plan37.batches[-1].back_pages}",
        )
        check(
            plan37.batches[-1].sheet_count == 4,
            f"37 页最后一批 4 张纸（末张只有正面）：{plan37.batches[-1].sheet_count}",
        )
        check(
            plan37.batches[-1].blank_back_sheets == (19,),
            f"最后一张纸（第 19 张）没有背面：{plan37.batches[-1].blank_back_sheets}",
        )

        # ---------------------------------------------------------------
        print("\n【检查 3】文件命名与打印顺序：每次只提交一个页面")
        # ---------------------------------------------------------------
        check(page_file_name(1) == "page_001.pdf", f"第 1 页文件名：{page_file_name(1)}")
        check(page_file_name(37) == "page_037.pdf", f"第 37 页文件名：{page_file_name(37)}")
        check(
            plan37.page_file_names[:3] == ("page_001.pdf", "page_002.pdf", "page_003.pdf"),
            f"全部单页文件名按页码升序：{plan37.page_file_names[:3]}…",
        )
        check(
            plan37.batches[0].front_file_names[:3]
            == ("page_001.pdf", "page_003.pdf", "page_005.pdf"),
            f"第 1 批正面文件：{plan37.batches[0].front_file_names}",
        )
        check(
            plan37.batches[-1].file_names[:2] == ("page_031.pdf", "page_033.pdf"),
            f"最后一批文件名：{plan37.batches[-1].file_names}",
        )
        steps = plan37.print_steps
        check(len(steps) == 37, f"37 页展开成 37 次提交（每次一个页面）：{len(steps)}")
        check(
            [(step.face, step.page_number) for step in steps[:3]]
            == [("front", 1), ("front", 3), ("front", 5)],
            f"前 3 次提交：{[(s.face, s.page_number) for s in steps[:3]]}",
        )
        check(
            [(step.face, step.page_number) for step in steps[5:8]]
            == [("back", page) for page in plan37.batches[0].back_pages[:3]],
            f"第 1 批翻面后：{[(s.face, s.page_number) for s in steps[5:8]]}",
        )
        check(
            [step.face for step in steps[5:10]] == ["back"] * 5,
            "一批的 5 个正面全部提交完，之后才开始提交背面",
        )
        check(
            all(step.file_name == page_file_name(step.page_number) for step in steps),
            "每个打印步骤都指向独立的单页文件",
        )
        check(
            len({step.file_name for step in steps}) == 37,
            "37 次提交使用 37 个不同的文件（不存在一批一个合并文件）",
        )

        # ---------------------------------------------------------------
        print("\n【检查 4】参数校验")
        # ---------------------------------------------------------------
        for bad_pages in (0, -3):
            try:
                plan_pages(bad_pages)
                check(False, f"页数 {bad_pages} 应当被拒绝")
            except PlanError as exc:
                check(True, f"页数 {bad_pages} 被拒绝：{exc}")
        try:
            plan_pages(5, 0)
            check(False, "每批 0 张应当被拒绝")
        except PlanError as exc:
            check(True, f"每批 0 张被拒绝：{exc}")
        try:
            plan_pages(5, 5, back_order="unknown")
            check(False, "未知背面顺序应当被拒绝")
        except PlanError as exc:
            check(True, f"未知背面顺序被拒绝：{exc}")
        check([b.sheet_count for b in plan_pages(10, 3).batches] == [3, 2],
              "每批 3 张时：10 页 → 5 张纸 → 3 + 2 张")

        # ---------------------------------------------------------------
        print("\n【检查 5】全量自检：1~40 页的拆分结果都自洽")
        # ---------------------------------------------------------------
        consistent = True
        for pages in range(1, 41):
            plan = plan_pages(pages, SHEETS_PER_BATCH)
            sheets = (pages + 1) // 2
            all_pages = sorted(set(plan.front_pages) | set(plan.back_pages))
            if (
                plan.sheet_count != sheets
                or all_pages != list(range(1, pages + 1))
                or sum(batch.sheet_count for batch in plan.batches) != sheets
                or plan.batch_count != -(-sheets // SHEETS_PER_BATCH)
                or any(batch.index != index + 1 for index, batch in enumerate(plan.batches))
                or len(plan.print_steps) != pages
                or len(plan.page_file_names) != pages
            ):
                consistent = False
                print(f"    {pages} 页结果异常：{plan.summary_text}")
        check(consistent, "1~40 页：纸张数、页覆盖、批次数、批次序号、单页文件数全部正确")

        # ---------------------------------------------------------------
        print("\n【检查 6】数据库与生成服务准备")
        # ---------------------------------------------------------------
        database = Database(db_path)
        database.initialize()
        repository = TaskRepository(database)
        service = PageJobService(
            repository=repository, jobs_dir=jobs_dir, sheets_per_batch=SHEETS_PER_BATCH
        )
        check(
            database.schema_version() == SCHEMA_VERSION,
            f"数据库 schema 版本为 {SCHEMA_VERSION}（实际 {database.schema_version()}）",
        )

        # ---------------------------------------------------------------
        print("\n【检查 7】11 页 PDF：2 批（5 张 + 1 张），生成 11 个单页文件")
        # ---------------------------------------------------------------
        source11 = make_pdf(sources / "eleven.pdf", 11)
        task11 = register_task(repository, source11, "SP-T4-011")
        sha_before = file_sha256(source11)
        mtime_before = source11.stat().st_mtime_ns

        result = service.generate(task11)
        check(result.batch_count == 2, f"生成 2 个批次（实际 {result.batch_count}）")
        check(result.plan.sheet_count == 6, f"11 页 → 6 张纸（实际 {result.plan.sheet_count}）")
        check(result.job_dir == jobs_dir / "SP-T4-011", f"任务目录：{result.job_dir}")
        check(len(result.pages) == 11, f"生成 11 个单页文件（实际 {len(result.pages)}）")

        names = sorted(path.name for path in result.job_dir.iterdir())
        expected_names = sorted(
            [page_file_name(page) for page in range(1, 12)]
            + ["original.pdf", "plan.json"]
        )
        check(names == expected_names, f"任务目录内容：{names}")
        check("front_batch_001.pdf" not in names, "不再生成按批次的正面合并文件")
        check("back_batch_001.pdf" not in names, "不再生成按批次的背面合并文件")
        check("original.pdf" in names, "原始 PDF 副本已保存为 original.pdf")
        check("plan.json" in names, "打印计划 plan.json 已生成")

        check(
            file_sha256(result.job_dir / "original.pdf") == sha_before,
            "original.pdf 与源文件内容完全一致",
        )
        check(file_sha256(source11) == sha_before, "生成后源 PDF 的 SHA-256 未变化")
        check(
            source11.stat().st_mtime_ns == mtime_before,
            "生成后源 PDF 的修改时间未变化（源文件没有被写回）",
        )

        # ---------------------------------------------------------------
        print("\n【检查 8】单页文件的内容与原始页面一致")
        # ---------------------------------------------------------------
        every_page_ok = True
        single_page_ok = True
        for page in range(1, 12):
            output = result.job_dir / page_file_name(page)
            if not output.is_file():
                every_page_ok = False
                print(f"    {output.name} 不存在")
                continue
            if page_count_of(output) != 1:
                single_page_ok = False
                print(f"    {output.name} 不是单页（{page_count_of(output)} 页）")
            if read_texts(output) != expected_texts((page,)):
                every_page_ok = False
                print(f"    {output.name} 内容错位：{read_texts(output)}")
            if verify_pages(output, (page,), source11) != []:
                every_page_ok = False
                print(f"    {output.name} 页码校验失败")
        check(single_page_ok, "每个输出文件都只有 1 页")
        check(every_page_ok, "11 个单页文件的内容、尺寸、旋转都与源文件对应页一致")
        check(
            verify_pages(result.job_dir / "page_009.pdf", (9,), source11) == [],
            "抽查 page_009.pdf：与源第 9 页一致",
        )

        # ---------------------------------------------------------------
        print("\n【检查 9】批次写入数据库与事件")
        # ---------------------------------------------------------------
        batches = repository.list_batches("SP-T4-011")
        check(len(batches) == 2, f"数据库记录 2 个批次（实际 {len(batches)}）")
        check([b.batch_index for b in batches] == [1, 2], "批次序号按 1、2 排列")
        check(batches[0].front_pages == (1, 3, 5, 7, 9), f"第 1 批正面页码：{batches[0].front_pages}")
        check(back_sequence(batches[0].back_pages) == (2, 4, 6, 8, 10),
              f"第 1 批背面页码：{batches[0].back_pages}")
        check(batches[1].back_pages == (), "第 2 批没有背面页码（11 页时第 6 张纸只有正面）")
        check(batches[1].is_last, "第 2 批被标记为最后一批")
        check(
            [path.name for path in batches[0].page_files]
            == [page_file_name(page) for page in (1, 3, 5, 7, 9) + batches[0].back_pages],
            f"第 1 批登记的单页文件：{[p.name for p in batches[0].page_files]}",
        )
        check(batches[0].missing_files() == [], "批次记录中的单页文件都存在")
        check(batches[1].page_files == (result.job_dir / "page_011.pdf",),
              f"第 2 批只有 1 个单页文件：{[p.name for p in batches[1].page_files]}")

        events = repository.list_events("SP-T4-011")
        check(any(event.event_type == "pages" for event in events),
              f"已记录 pages 事件：{[e.event_type for e in events]}")

        manifest = json.loads((result.job_dir / "plan.json").read_text(encoding="utf-8"))
        check(manifest["page_count"] == 11, f"plan.json 记录页数：{manifest['page_count']}")
        check(manifest["batch_count"] == 2, f"plan.json 记录批次数：{manifest['batch_count']}")
        check(manifest["source_sha256"] == sha_before, "plan.json 记录了源文件 SHA-256")
        check(len(manifest["batches"]) == 2, "plan.json 记录了每个批次的页码")
        check(len(manifest["pages"]) == 11, f"plan.json 记录了每个单页文件（{len(manifest['pages'])} 条）")
        check(
            [item["file"] for item in manifest["pages"][:3]]
            == [page_file_name(page) for page in (1, 2, 3)],
            f"plan.json 的单页记录：{[item['file'] for item in manifest['pages'][:3]]}",
        )

        # ---------------------------------------------------------------
        print("\n【检查 10】重复生成：结果一致、不产生多余文件")
        # ---------------------------------------------------------------
        result_again = service.generate(task11)
        second_names = sorted(path.name for path in result_again.job_dir.iterdir())
        check(names == second_names, f"重复生成后目录文件不变：{second_names}")
        check(len(repository.list_batches("SP-T4-011")) == 2, "重复生成不会产生重复批次记录")
        check(
            read_texts(result.job_dir / page_file_name(3)) == expected_texts((3,)),
            "重复生成后单页内容不变",
        )

        # ---------------------------------------------------------------
        print("\n【检查 11】页数变化后重新生成：旧单页文件被清理")
        # ---------------------------------------------------------------
        make_pdf(source11, 3)  # 同一个路径换成 3 页的文件
        result3 = service.generate(task11)
        check(result3.batch_count == 1, f"3 页 → 1 个批次（实际 {result3.batch_count}）")
        names3 = sorted(path.name for path in result3.job_dir.iterdir())
        check(
            names3 == ["original.pdf", "page_001.pdf", "page_002.pdf", "page_003.pdf", "plan.json"],
            f"旧的 page_004…page_011 已清理：{names3}",
        )
        batches3 = repository.list_batches("SP-T4-011")
        check(len(batches3) == 1, f"数据库批次记录被替换为 1 条（实际 {len(batches3)}）")
        check(
            read_texts(result3.job_dir / "page_001.pdf") == expected_texts((1,)),
            "新文件 page_001.pdf 是第 1 页",
        )
        check(
            read_texts(result3.job_dir / "page_003.pdf") == expected_texts((3,)),
            "新文件 page_003.pdf 是第 3 页",
        )

        # ---------------------------------------------------------------
        print("\n【检查 12】单页 / 双页 / 三页文件")
        # ---------------------------------------------------------------
        source1 = make_pdf(sources / "one.pdf", 1)
        task1 = register_task(repository, source1, "SP-T4-001")
        result1 = service.generate(task1)
        check(result1.batch_count == 1, "1 页 → 1 个批次")
        check(len(result1.pages) == 1, "1 页只生成 1 个单页文件")
        check(
            read_texts(result1.job_dir / "page_001.pdf") == ["PAGE-1"],
            "1 页文件只有第 1 页",
        )
        check(not (result1.job_dir / "page_002.pdf").exists(), "1 页不会凭空生成第 2 页")
        check(result1.plan.sheet_count == 1, "1 页只占用 1 张纸")

        source2 = make_pdf(sources / "two.pdf", 2)
        task2 = register_task(repository, source2, "SP-T4-002")
        result2 = service.generate(task2)
        check(
            [path.name for path in result2.job_dir.glob("page_*.pdf")]
            == [page_file_name(page) for page in (1, 2)],
            "2 页 → page_001.pdf、page_002.pdf",
        )
        check(result2.plan.sheet_count == 1, "2 页只占用 1 张纸（双面）")
        check(
            read_texts(result2.job_dir / "page_002.pdf") == ["PAGE-2"],
            "第 2 页落在第 1 张纸的背面",
        )

        source3 = make_pdf(sources / "three.pdf", 3)
        task3 = register_task(repository, source3, "SP-T4-003")
        result3b = service.generate(task3)
        check(result3b.plan.sheet_count == 2, "3 页占用 2 张纸")
        check(
            [sheet.back_page for sheet in result3b.plan.sheets] == [2, None],
            "3 页时第 2 张纸只有正面（back_page 为 None）",
        )
        check(
            [path.name for path in sorted(result3b.job_dir.glob("page_*.pdf"))]
            == [page_file_name(page) for page in (1, 2, 3)],
            "3 页 → 3 个单页文件，没有第 4 页",
        )

        # ---------------------------------------------------------------
        print("\n【检查 13】37 页文件：4 批（5/5/5/4），最后一批 4 张")
        # ---------------------------------------------------------------
        source37 = make_pdf(sources / "thirty_seven.pdf", 37)
        task37 = register_task(repository, source37, "SP-T4-037")
        result37 = service.generate(task37)
        check(
            [batch.sheet_count for batch in result37.batches] == [5, 5, 5, 4],
            f"37 页批次张数：{[b.sheet_count for b in result37.batches]}",
        )
        check(result37.batches[-1].is_last, "第 4 批是最后一批")
        check(len(result37.pages) == 37, f"37 页生成 37 个单页文件（实际 {len(result37.pages)}）")
        check(
            read_texts(result37.job_dir / page_file_name(31)) == expected_texts((31,)),
            "最后一批的 page_031.pdf 内容是第 31 页",
        )
        check(
            read_texts(result37.job_dir / page_file_name(37)) == expected_texts((37,)),
            "page_037.pdf 内容是第 37 页（最后一张纸只有正面）",
        )
        check(
            not (result37.job_dir / page_file_name(38)).exists(),
            "不会为了凑成双面而生成第 38 页",
        )
        check(
            all(
                verify_pages(result37.job_dir / page_file_name(page), (page,), source37) == []
                for page in range(1, 38)
            ),
            "37 个单页文件全部通过页码校验",
        )
        check(len(repository.list_batches("SP-T4-037")) == 4, "37 页在数据库中有 4 条批次记录")

        # ---------------------------------------------------------------
        print("\n【检查 14】空白页与页面尺寸检查")
        # ---------------------------------------------------------------
        source_blank = make_pdf(sources / "blank.pdf", 6, blank_pages=(3, 5))
        scan_blank = scan_pages(source_blank)
        check(scan_blank.blank_pages == (3, 5), f"空白页识别：{scan_blank.blank_pages}")
        check(any("空白" in warning for warning in scan_blank.warnings),
              f"空白页给出提示：{scan_blank.warnings}")
        task_blank = register_task(repository, source_blank, "SP-T4-BLANK")
        result_blank = service.generate(task_blank)
        check(result_blank.plan.sheet_count == 3, "6 页（含空白页）仍按 3 张纸计算")
        check(len(result_blank.pages) == 6, "空白页同样生成单页文件（不会跳过页码）")
        check(
            read_texts(result_blank.job_dir / page_file_name(1)) == ["PAGE-1"],
            "空白页没有打乱页码（第 1 页仍是第 1 页）",
        )
        check(
            read_texts(result_blank.job_dir / page_file_name(3)) == [""],
            "第 3 页确实是空白页（内容为空）",
        )
        check(
            any(page.blank for page in result_blank.pages),
            "生成结果标记了空白页",
        )
        check(
            verify_pages(result_blank.job_dir / page_file_name(3), (3,), source_blank) == [],
            "含空白页的单页文件仍然通过页码校验",
        )

        source_mixed = make_pdf(sources / "mixed.pdf", 4, mixed_size_pages=(3,))
        scan_mixed = scan_pages(source_mixed)
        check(scan_mixed.mixed_size, "混合页面尺寸被识别")
        check(any("尺寸" in warning for warning in scan_mixed.warnings),
              f"混合尺寸给出提示：{scan_mixed.warnings}")
        task_mixed = register_task(repository, source_mixed, "SP-T4-MIXED")
        result_mixed = service.generate(task_mixed)
        check(result_mixed.batch_count == 1, "混合尺寸文件仍能正常拆分")
        check(
            verify_pages(result_mixed.job_dir / page_file_name(3), (3,), source_mixed) == [],
            "混合尺寸文件的第 3 页（A5）仍与源页一致",
        )
        with pymupdf.open(result_mixed.job_dir / page_file_name(3)) as document:
            rect = document.load_page(0).rect
        check(
            abs(rect.width - A5_WIDTH_PT) < 1 and abs(rect.height - A5_HEIGHT_PT) < 1,
            f"第 3 页保留了 A5 尺寸：{rect.width:.1f} × {rect.height:.1f} pt",
        )
        with pymupdf.open(result_mixed.job_dir / page_file_name(1)) as document:
            rect1 = document.load_page(0).rect
        check(
            abs(rect1.width - A4_WIDTH_PT) < 1 and abs(rect1.height - A4_HEIGHT_PT) < 1,
            f"第 1 页保留 A4 尺寸：{rect1.width:.1f} × {rect1.height:.1f} pt",
        )

        # ---------------------------------------------------------------
        print("\n【检查 15】可配置项：每批 3 张、背面降序")
        # ---------------------------------------------------------------
        source10 = make_pdf(sources / "ten.pdf", 10)
        task10 = register_task(repository, source10, "SP-T4-010")
        result10 = service.generate(task10, sheets_per_batch=3)
        check([batch.sheet_count for batch in result10.batches] == [3, 2],
              f"每批 3 张：{[b.sheet_count for b in result10.batches]}")
        check(result10.plan.sheets_per_batch == 3, "生成结果记录了每批 3 张")
        check(len(result10.pages) == 10, "每批张数只影响分组，单页文件仍是 10 个")

        descending = PageJobService(
            repository=repository,
            jobs_dir=jobs_dir,
            sheets_per_batch=SHEETS_PER_BATCH,
            back_order=BACK_ORDER_DESCENDING,
        )
        result_desc = descending.generate(task10)
        check(
            result_desc.plan.batches[0].back_pages == (10, 8, 6, 4, 2),
            f"背面降序：{result_desc.plan.batches[0].back_pages}",
        )
        check(
            result_desc.plan.batches[0].front_pages == (1, 3, 5, 7, 9),
            "背面降序不影响正面页序",
        )
        check(
            read_texts(result_desc.job_dir / page_file_name(10)) == expected_texts((10,)),
            "背面降序时 page_010.pdf 内容仍是第 10 页（文件与顺序无关）",
        )

        # ---------------------------------------------------------------
        print("\n【检查 16】按需取单页文件（阶段五、六要用）")
        # ---------------------------------------------------------------
        path5 = service.ensure_page_file(task10, 5)
        check(path5.name == page_file_name(5) and path5.is_file(),
              f"ensure_page_file 返回已有文件：{path5.name}")
        check(read_texts(path5) == expected_texts((5,)), "按需取到的确实是第 5 页")
        missing_before = sorted(p.name for p in result_desc.job_dir.glob("page_*.pdf"))
        check(len(missing_before) == 10, "按需取文件不会产生多余文件")
        try:
            service.ensure_page_file(task10, 99)
            check(False, "超出范围的页码应当报错")
        except PageJobError as exc:
            check("超出范围" in str(exc), f"超出范围的页码被拒绝：{exc}")

        # ---------------------------------------------------------------
        print("\n【检查 17】异常处理")
        # ---------------------------------------------------------------
        missing_task = Task(
            task_uid="SP-T4-MISSING",
            file_name="missing.pdf",
            document_name="missing",
            source_path=sources / "not_exists.pdf",
            file_size=0,
            page_count=4,
            paper_size="A4",
            orientation="纵向",
            width_pt=A4_WIDTH_PT,
            height_pt=A4_HEIGHT_PT,
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
        try:
            service.generate(missing_task)
            check(False, "源文件不存在时应当报错")
        except PageJobError as exc:
            check("不存在" in str(exc), f"源文件不存在时报错：{exc}")
        check(not (jobs_dir / "SP-T4-MISSING").exists(), "源文件不存在时不会创建空目录")

        try:
            extract_pages(source10, workdir / "out" / "bad.pdf", (1, 99))
            check(False, "页码越界时应当报错")
        except PdfSplitError as exc:
            check("超出范围" in str(exc), f"页码越界时报错：{exc}")
        check(not (workdir / "out" / "bad.pdf").exists(), "页码越界时不会留下半个 PDF")

        problems = verify_pages(
            result.job_dir / page_file_name(1), (2,), source11
        )
        check(bool(problems), f"页码错位能被校验发现：{problems[:1]}")

        # ---------------------------------------------------------------
        print("\n生成的打印文件：")
        for uid in sorted(path.name for path in jobs_dir.iterdir()):
            folder = jobs_dir / uid
            files = sorted(path.name for path in folder.glob("page_*.pdf"))
            print(f"  {uid:<14} {len(files):>2} 个单页文件  {files[:6]}{'…' if len(files) > 6 else ''}")

    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        for suffix in ("", "-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)

    passed = sum(1 for ok, _ in results if ok)
    total = len(results)
    print(f"\n结果：{passed}/{total} 项通过")
    failed = [desc for ok, desc in results if not ok]
    if failed:
        print("未通过：")
        for desc in failed:
            print(f"  - {desc}")
        return 1
    print("阶段四逻辑验收通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
