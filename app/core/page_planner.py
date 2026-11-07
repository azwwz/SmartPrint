"""打印纸张与页面映射计算（阶段四，纯逻辑，不依赖 PDF 库）。

手动双面打印的核心是「哪一页印在哪一面」。一张纸两个页面：

    第 1 张纸：正面第 1 页，背面第 2 页
    第 2 张纸：正面第 3 页，背面第 4 页
    ...

批次按「纸张数量」切分（默认每批 5 张纸，可在设置里改成 1–20 张），而不是按 PDF 页数切分：
默认的 5 张纸一批最多包含 10 个页面。最后一批不足一批时按实际张数计算，
即使只剩 1 张纸也只算 1 张，不会为了凑成整批而凭空增加页面。
每批张数只影响「一次翻多少张纸」，不影响页码映射，也不影响补打轮。

批次只是「纸张管理和翻面管理的单位」，不是提交单位：真正交给打印程序的
永远是**一个页面对应的一个单页 PDF**，文件名按页码命名（readme 五、Data/jobs）：

    page_001.pdf    第 1 页
    page_003.pdf    第 3 页
    ...

页码统一使用「从 1 开始的 PDF 页码」，与 PDF 阅读器里看到的页码一致。
本模块只做计算，不读 PDF 文件；真正的页面提取在 app/core/pdf_splitter.py。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

# 默认每批纸张数量（与 config.json 的 paper.sheets_per_batch 保持一致）
DEFAULT_SHEETS_PER_BATCH = 5

# 正面：奇数页升序
FRONT_ORDER_ODD_FIRST = "odd_first"
FRONT_ORDERS = (FRONT_ORDER_ODD_FIRST,)

# 背面：偶数页升序 / 偶数页降序（降序 = 批次内按物理纸张编号从大到小，默认值）
BACK_ORDER_ASCENDING = "even_ascending"
BACK_ORDER_DESCENDING = "even_descending"
BACK_ORDERS = (BACK_ORDER_ASCENDING, BACK_ORDER_DESCENDING)

DEFAULT_FRONT_ORDER = FRONT_ORDER_ODD_FIRST
#: 默认背面降序：最后打印的正面那张纸落在出纸托盘最上方，翻面后先被送进打印机，
#: 所以背面必须按物理纸张编号从大到小提交（见 2.4 节「反面逆序」）。
#: ``even_ascending`` 仍然保留，供旧任务与对比用。
DEFAULT_BACK_ORDER = BACK_ORDER_DESCENDING

#: 打印面
FACE_FRONT = "front"
FACE_BACK = "back"
FACE_LABELS = {FACE_FRONT: "正面", FACE_BACK: "背面"}


class PlanError(ValueError):
    """参数不合法，无法计算打印批次。"""


#: 单页文件名的前缀宽度，够用到 999 页
PAGE_FILE_DIGITS = 3


def describe_pages(pages: Iterable[int]) -> str:
    """把页码列表格式化成「1、3、5」这样的文本。"""
    return "、".join(str(page) for page in pages)


def page_file_name(page_number: int) -> str:
    """单页 PDF 的文件名：第 3 页 → ``page_003.pdf``。"""
    return f"page_{int(page_number):0{PAGE_FILE_DIGITS}d}.pdf"


def sheet_number_of_page(page_number: int) -> int:
    """页码落在第几张纸上：1、2 → 第 1 张；3、4 → 第 2 张。"""
    return (int(page_number) + 1) // 2


def face_of_page(page_number: int) -> str:
    """页码在纸张的哪一面：奇数页 → 正面，偶数页 → 背面。"""
    return FACE_FRONT if int(page_number) % 2 == 1 else FACE_BACK


def face_label(face: str) -> str:
    return FACE_LABELS.get(face, face)


def normalize_front_order(value: object) -> str:
    """把配置里的正面顺序转成合法值，无法识别时回退到默认值。"""
    text = str(value or "").strip().lower()
    return text if text in FRONT_ORDERS else DEFAULT_FRONT_ORDER


def normalize_back_order(value: object) -> str:
    """把配置里的背面顺序转成合法值，无法识别时回退到默认值（降序）。

    旧版本配置里可能还是 ``even_ascending``，它仍然是合法值，
    只会影响旧任务/手动选择的顺序，不会被自动改写。
    """
    text = str(value or "").strip().lower()
    return text if text in BACK_ORDERS else DEFAULT_BACK_ORDER


@dataclass(frozen=True)
class SheetPlan:
    """一张纸对应的两个页码。"""

    number: int  # 纸张序号，从 1 开始
    batch_index: int  # 所属批次，从 1 开始
    front_page: int  # 正面页码
    back_page: int | None  # 背面页码；None 表示这张纸只有正面

    @property
    def has_back(self) -> bool:
        return self.back_page is not None

    @property
    def pages(self) -> tuple[int, ...]:
        if self.back_page is None:
            return (self.front_page,)
        return (self.front_page, self.back_page)

    @property
    def back_text(self) -> str:
        return str(self.back_page) if self.back_page is not None else "无（空白背面）"

    @property
    def label(self) -> str:
        return f"第 {self.number} 张：正面 {self.front_page}，背面 {self.back_text}"

    @property
    def front_file_name(self) -> str:
        return page_file_name(self.front_page)

    @property
    def back_file_name(self) -> str | None:
        return page_file_name(self.back_page) if self.back_page is not None else None

    @property
    def file_names(self) -> tuple[str, ...]:
        return tuple(page_file_name(page) for page in self.pages)


@dataclass(frozen=True)
class BatchPlan:
    """一个批次：若干张纸，以及正面 / 反面的页码顺序。

    批次只是纸张管理与翻面管理的单位。每个页码各对应一个单页 PDF，
    所以这里给出的是一组文件名，而不是一份「本批正面 PDF」。
    """

    index: int  # 批次序号，从 1 开始
    total_batches: int
    sheets: tuple[SheetPlan, ...]
    front_pages: tuple[int, ...]
    back_pages: tuple[int, ...]

    @property
    def sheet_count(self) -> int:
        return len(self.sheets)

    @property
    def page_count(self) -> int:
        return len(self.front_pages) + len(self.back_pages)

    @property
    def is_last(self) -> bool:
        return self.index == self.total_batches

    @property
    def has_back(self) -> bool:
        return bool(self.back_pages)

    @property
    def blank_back_sheets(self) -> tuple[int, ...]:
        """本批中没有背面内容的纸张序号（全局序号）。"""
        return tuple(sheet.number for sheet in self.sheets if not sheet.has_back)

    @property
    def front_file_names(self) -> tuple[str, ...]:
        """本批正面各页对应的单页文件名，按打印顺序排列。"""
        return tuple(page_file_name(page) for page in self.front_pages)

    @property
    def back_file_names(self) -> tuple[str, ...]:
        """本批背面各页对应的单页文件名，按打印顺序排列。"""
        return tuple(page_file_name(page) for page in self.back_pages)

    @property
    def file_names(self) -> tuple[str, ...]:
        """本批全部单页文件名（正面在前，背面在后）。"""
        return self.front_file_names + self.back_file_names

    @property
    def front_pages_text(self) -> str:
        return describe_pages(self.front_pages)

    @property
    def back_pages_text(self) -> str:
        return describe_pages(self.back_pages) if self.back_pages else "无"

    @property
    def status_text(self) -> str:
        tags = []
        if self.is_last:
            tags.append("最后一批")
        if not self.has_back:
            tags.append("只有正面")
        return "、".join(tags) if tags else "—"

    @property
    def summary_text(self) -> str:
        return (
            f"第 {self.index} 批：{self.sheet_count} 张纸，"
            f"正面 {self.front_pages_text}，背面 {self.back_pages_text}"
        )


@dataclass(frozen=True)
class PrintStep:
    """一次提交：把某一页的单页 PDF 送去打印，然后等用户确认。

    阶段四只负责算出这些步骤与对应的单页文件；真正的提交在阶段五/六。
    """

    batch_index: int  # 所属批次，从 1 开始
    sheet_number: int  # 这张纸的全局序号，从 1 开始
    face: str  # front / back
    page_number: int  # 要打印的页码

    @property
    def face_text(self) -> str:
        return face_label(self.face)

    @property
    def file_name(self) -> str:
        return page_file_name(self.page_number)

    @property
    def label(self) -> str:
        return (
            f"第 {self.batch_index} 批 · 第 {self.sheet_number} 张 · "
            f"{self.face_text} · 第 {self.page_number} 页"
        )


@dataclass(frozen=True)
class PagePlan:
    """整份 PDF 的打印计划。"""

    page_count: int
    sheets_per_batch: int
    sheets: tuple[SheetPlan, ...]
    batches: tuple[BatchPlan, ...]
    front_order: str = DEFAULT_FRONT_ORDER
    back_order: str = DEFAULT_BACK_ORDER

    @property
    def sheet_count(self) -> int:
        return len(self.sheets)

    @property
    def batch_count(self) -> int:
        return len(self.batches)

    @property
    def front_pages(self) -> tuple[int, ...]:
        return tuple(sheet.front_page for sheet in self.sheets)

    @property
    def back_pages(self) -> tuple[int, ...]:
        """全部纸张的背面页码，按纸张编号升序（与提交顺序无关）。"""
        return tuple(sheet.back_page for sheet in self.sheets if sheet.back_page is not None)

    @property
    def back_pages_in_print_order(self) -> tuple[int, ...]:
        """全部纸张的背面页码，按实际提交顺序（逐批、批内按配置排序）。"""
        return tuple(page for batch in self.batches for page in batch.back_pages)

    @property
    def blank_back_sheets(self) -> tuple[int, ...]:
        return tuple(sheet.number for sheet in self.sheets if not sheet.has_back)

    @property
    def last_batch(self) -> BatchPlan | None:
        return self.batches[-1] if self.batches else None

    @property
    def page_file_names(self) -> tuple[str, ...]:
        """全部页面的单页文件名，按页码升序。"""
        return tuple(page_file_name(page) for page in range(1, self.page_count + 1))

    @property
    def print_sequence(self) -> tuple[tuple[int, str, int | None], ...]:
        """实际的打印顺序：逐批、先正面后背面，每次只有一个页面。

        返回 ``(纸张序号, 打印面 front/back, 页码)`` 的三元组序列，
        与 readme「六、打印流程」一致：正面 1,3,5… 每页单独提交并确认，
        翻面后再按纸张编号降序 10,8,6,4,2 每页单独提交并确认。
        """
        return tuple(
            (step.sheet_number, step.face, step.page_number) for step in self.print_steps
        )

    @property
    def print_steps(self) -> tuple[PrintStep, ...]:
        """把打印计划展开成「一次提交一个页面」的步骤列表。

        顺序就是实际提交顺序：逐批处理，一批之内先按纸张编号升序打印正面，
        翻面确认后再按 ``back_order``（默认降序）打印背面。
        """
        steps: list[PrintStep] = []
        for batch in self.batches:
            for sheet in batch.sheets:
                steps.append(
                    PrintStep(
                        batch_index=batch.index,
                        sheet_number=sheet.number,
                        face=FACE_FRONT,
                        page_number=sheet.front_page,
                    )
                )
            for page in batch.back_pages:
                steps.append(
                    PrintStep(
                        batch_index=batch.index,
                        sheet_number=sheet_number_of_page(page),
                        face=FACE_BACK,
                        page_number=page,
                    )
                )
        return tuple(steps)

    def batch(self, index: int) -> BatchPlan:
        """按 1 起始的批次序号取批次。"""
        if not 1 <= index <= len(self.batches):
            raise IndexError(f"批次序号超出范围：{index}")
        return self.batches[index - 1]

    def sheet(self, number: int) -> SheetPlan:
        """按 1 起始的纸张序号取纸张。"""
        if not 1 <= number <= len(self.sheets):
            raise IndexError(f"纸张序号超出范围：{number}")
        return self.sheets[number - 1]

    @property
    def summary_text(self) -> str:
        return (
            f"{self.page_count} 页 → {self.sheet_count} 张纸 → {self.batch_count} 批"
            f"（每批 {self.sheets_per_batch} 张）"
        )


def plan_pages(
    page_count: int,
    sheets_per_batch: int = DEFAULT_SHEETS_PER_BATCH,
    front_order: str = DEFAULT_FRONT_ORDER,
    back_order: str = DEFAULT_BACK_ORDER,
) -> PagePlan:
    """根据总页数计算纸张、批次与正反面页码。

    Args:
        page_count: PDF 总页数，必须大于 0。
        sheets_per_batch: 每批纸张数量，必须大于 0。
        front_order: 正面页码顺序，目前只支持 ``odd_first``（奇数页升序）。
        back_order: 背面页码顺序，``even_ascending`` 或 ``even_descending``。

    Raises:
        PlanError: 页数、批次大小或顺序参数不合法。
    """
    if isinstance(page_count, bool) or not isinstance(page_count, int):
        raise PlanError(f"页数必须是整数：{page_count!r}")
    if page_count < 1:
        raise PlanError(f"页数必须大于 0，当前为 {page_count}")

    if isinstance(sheets_per_batch, bool) or not isinstance(sheets_per_batch, int):
        raise PlanError(f"每批纸张数量必须是整数：{sheets_per_batch!r}")
    if sheets_per_batch < 1:
        raise PlanError(f"每批纸张数量必须大于 0，当前为 {sheets_per_batch}")

    if front_order not in FRONT_ORDERS:
        raise PlanError(f"不支持的正面页码顺序：{front_order!r}")
    if back_order not in BACK_ORDERS:
        raise PlanError(f"不支持的背面页码顺序：{back_order!r}")

    sheet_count = (page_count + 1) // 2
    sheets: list[SheetPlan] = []
    for index in range(sheet_count):
        front_page = index * 2 + 1
        candidate_back = index * 2 + 2
        back_page = candidate_back if candidate_back <= page_count else None
        sheets.append(
            SheetPlan(
                number=index + 1,
                batch_index=index // sheets_per_batch + 1,
                front_page=front_page,
                back_page=back_page,
            )
        )

    total_batches = (sheet_count + sheets_per_batch - 1) // sheets_per_batch
    batches: list[BatchPlan] = []
    for batch_no in range(1, total_batches + 1):
        start = (batch_no - 1) * sheets_per_batch
        group = tuple(sheets[start : start + sheets_per_batch])
        front_pages = tuple(sheet.front_page for sheet in group)
        back_pages: tuple[int, ...] = tuple(
            sheet.back_page for sheet in group if sheet.back_page is not None
        )
        if back_order == BACK_ORDER_DESCENDING:
            back_pages = tuple(reversed(back_pages))
        batches.append(
            BatchPlan(
                index=batch_no,
                total_batches=total_batches,
                sheets=group,
                front_pages=front_pages,
                back_pages=back_pages,
            )
        )

    return PagePlan(
        page_count=page_count,
        sheets_per_batch=sheets_per_batch,
        sheets=tuple(sheets),
        batches=tuple(batches),
        front_order=front_order,
        back_order=back_order,
    )
