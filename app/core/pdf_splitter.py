"""PDF 页面读取与页面提取（阶段四）。

职责有三件：

1. ``scan_pages``：逐页读取尺寸、旋转、文字量，标记可能的空白页；
2. ``extract_pages``：按给定的页码顺序生成新的 PDF 文件；
3. ``verify_pages``：把输出的每一页和源文件对应页比对，确认页码没有错位。

安全约定（重要）：

* 源 PDF 始终以只读方式打开，本模块不会写回源文件；
* 输出先写到同目录的临时文件，成功后才改名替换，中途失败不会留下半个 PDF；
* 提取完成后立即做一次页码比对，发现错位直接报错，而不是等打印出来才发现。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

try:  # PyMuPDF 1.24+ 推荐写法，旧版本回退到 fitz
    import pymupdf
except ImportError:  # pragma: no cover - 兼容旧版本
    import fitz as pymupdf  # type: ignore[no-redef]

from app.core.pdf_inspector import PAPER_SIZES_PT, SIZE_TOLERANCE_PT

# 判断空白页时，少于这个字符数的文字视为「没有内容」
BLANK_TEXT_THRESHOLD = 0

SIZE_TEXT_TOLERANCE_PT = 0.5


class PdfSplitError(Exception):
    """读取或提取 PDF 页面失败。"""


def describe_page_size(width_pt: float, height_pt: float) -> str:
    """把点尺寸转成「A4 纵向（595×842pt）」这样的文本。"""
    width = float(width_pt or 0)
    height = float(height_pt or 0)
    orientation = "横向" if width > height else "纵向"
    name = ""
    short_edge, long_edge = sorted((width, height))
    for paper_name, (paper_width, paper_height) in PAPER_SIZES_PT.items():
        paper_short, paper_long = sorted((paper_width, paper_height))
        if (
            abs(short_edge - paper_short) <= SIZE_TOLERANCE_PT
            and abs(long_edge - paper_long) <= SIZE_TOLERANCE_PT
        ):
            name = paper_name
            break
    size_text = f"{width:.0f}×{height:.0f}pt"
    if name:
        return f"{name} {orientation}（{size_text}）"
    return f"自定义 {orientation}（{size_text}）"


@dataclass(frozen=True)
class PageInfo:
    """单个页面的检查结果。"""

    number: int  # 页码，从 1 开始
    width_pt: float
    height_pt: float
    rotation: int
    text_chars: int
    image_count: int
    drawing_count: int
    blank: bool

    @property
    def size_text(self) -> str:
        return describe_page_size(self.width_pt, self.height_pt)

    @property
    def blank_text(self) -> str:
        return "空白" if self.blank else "有内容"

    def label(self) -> str:
        return f"第 {self.number} 页：{self.size_text}，{self.blank_text}"


@dataclass(frozen=True)
class PageScan:
    """整份 PDF 的页面检查结果。"""

    path: Path
    page_count: int
    pages: tuple[PageInfo, ...]
    blank_pages: tuple[int, ...]
    rotated_pages: tuple[int, ...]
    mixed_size: bool
    warnings: tuple[str, ...]

    @property
    def first_page(self) -> PageInfo | None:
        return self.pages[0] if self.pages else None

    @property
    def paper_size(self) -> str:
        info = self.first_page
        if info is None:
            return ""
        return info.size_text

    @property
    def has_warnings(self) -> bool:
        return bool(self.warnings)

    @property
    def blank_pages_text(self) -> str:
        if not self.blank_pages:
            return "无"
        return "、".join(str(page) for page in self.blank_pages)

    def summary_text(self) -> str:
        return (
            f"{self.path.name}：{self.page_count} 页，{self.paper_size}，"
            f"空白页 {self.blank_pages_text}"
        )


def _normalize_text(page: "pymupdf.Page") -> str:
    """把页面文字压成用于比较的一行文本。"""
    try:
        raw = page.get_text("text") or ""
    except Exception:  # pragma: no cover - 极少见的损坏页面
        return ""
    return " ".join(raw.split())


def _load_document(path: Path) -> "pymupdf.Document":
    pdf_path = Path(path)
    if not pdf_path.is_file():
        raise PdfSplitError(f"PDF 文件不存在：{pdf_path}")
    try:
        document = pymupdf.open(pdf_path)
    except Exception as exc:  # noqa: BLE001 - 统一转成本模块异常
        raise PdfSplitError(f"无法打开 PDF（{pdf_path.name}）：{exc}") from exc
    if document.needs_pass:
        document.close()
        raise PdfSplitError(f"PDF 已加密，无法处理：{pdf_path.name}")
    if document.page_count <= 0:
        document.close()
        raise PdfSplitError(f"PDF 没有任何页面：{pdf_path.name}")
    return document


def scan_pages(path: Path | str) -> PageScan:
    """逐页读取 PDF，返回尺寸、旋转与空白页信息。"""
    pdf_path = Path(path)
    document = _load_document(pdf_path)
    try:
        pages: list[PageInfo] = []
        for index in range(document.page_count):
            page = document.load_page(index)
            rect = page.rect
            text = page.get_text("text") or ""
            text_chars = len(text.strip())
            image_count = len(page.get_images(full=False))
            drawing_count = 0
            blank = False
            if text_chars <= BLANK_TEXT_THRESHOLD and image_count == 0:
                # 没有文字也没有图片时才去数矢量图形，避免全文大文档变慢
                drawing_count = len(page.get_drawings())
                blank = drawing_count == 0
            pages.append(
                PageInfo(
                    number=index + 1,
                    width_pt=round(float(rect.width), 2),
                    height_pt=round(float(rect.height), 2),
                    rotation=int(page.rotation),
                    text_chars=text_chars,
                    image_count=image_count,
                    drawing_count=drawing_count,
                    blank=blank,
                )
            )
        page_count = document.page_count
    finally:
        document.close()

    blank_pages = tuple(page.number for page in pages if page.blank)
    rotated_pages = tuple(page.number for page in pages if page.rotation % 360 != 0)

    warnings: list[str] = []
    first = pages[0]
    other_sizes = [
        page
        for page in pages[1:]
        if abs(page.width_pt - first.width_pt) > SIZE_TEXT_TOLERANCE_PT
        or abs(page.height_pt - first.height_pt) > SIZE_TEXT_TOLERANCE_PT
    ]
    if other_sizes:
        examples = "、".join(
            f"第 {page.number} 页 {page.width_pt:.0f}×{page.height_pt:.0f}pt"
            for page in other_sizes[:3]
        )
        more = "" if len(other_sizes) <= 3 else f" 等 {len(other_sizes)} 页"
        warnings.append(f"页面尺寸不一致：{examples}{more}，打印前请确认纸张设置")
    if blank_pages:
        warnings.append(
            f"第 {'、'.join(str(page) for page in blank_pages)} 页没有文字和图形，"
            "可能是空白页，仍会按页码生成"
        )
    if rotated_pages:
        warnings.append(
            f"第 {'、'.join(str(page) for page in rotated_pages)} 页设置了旋转，"
            "打印方向以 PDF 阅读器显示为准"
        )

    return PageScan(
        path=pdf_path,
        page_count=page_count,
        pages=tuple(pages),
        blank_pages=blank_pages,
        rotated_pages=rotated_pages,
        mixed_size=bool(other_sizes),
        warnings=tuple(warnings),
    )


def _page_mismatch(output_page: "pymupdf.Page", source_page: "pymupdf.Page") -> str | None:
    """比较两个页面，内容一致返回 None，否则返回差异描述。"""
    out_rect = output_page.rect
    src_rect = source_page.rect
    if (
        abs(out_rect.width - src_rect.width) > SIZE_TEXT_TOLERANCE_PT
        or abs(out_rect.height - src_rect.height) > SIZE_TEXT_TOLERANCE_PT
    ):
        return (
            f"尺寸不同（输出 {out_rect.width:.0f}×{out_rect.height:.0f}pt，"
            f"源 {src_rect.width:.0f}×{src_rect.height:.0f}pt）"
        )
    if int(output_page.rotation) != int(source_page.rotation):
        return f"旋转角度不同（输出 {output_page.rotation}°，源 {source_page.rotation}°）"

    out_text = _normalize_text(output_page)
    src_text = _normalize_text(source_page)
    if out_text != src_text:
        preview_out = out_text[:20]
        preview_src = src_text[:20]
        return f"文字内容不同（输出「{preview_out}」，源「{preview_src}」）"
    if not src_text:
        # 纯图片页没有文字可比较，退化到比较图片数量
        out_images = len(output_page.get_images(full=False))
        src_images = len(source_page.get_images(full=False))
        if out_images != src_images:
            return f"图片数量不同（输出 {out_images}，源 {src_images}）"
    return None


def verify_pages(
    path: Path | str,
    expected_pages: Sequence[int],
    source: Path | str,
) -> list[str]:
    """核对输出文件的每一页是否就是源文件的对应页。

    Args:
        path: 要检查的输出 PDF。
        expected_pages: 期望的页码顺序（从 1 开始）。
        source: 源 PDF。

    Returns:
        问题描述列表；空列表表示完全一致。
    """
    problems: list[str] = []
    output_path = Path(path)
    try:
        output_document = _load_document(output_path)
    except PdfSplitError as exc:
        return [str(exc)]
    try:
        source_document = _load_document(Path(source))
    except PdfSplitError as exc:
        output_document.close()
        return [str(exc)]

    try:
        if output_document.page_count != len(expected_pages):
            problems.append(
                f"页数不符：期望 {len(expected_pages)} 页，实际 {output_document.page_count} 页"
            )
        for position, source_number in enumerate(expected_pages):
            if position >= output_document.page_count:
                break
            if not 1 <= source_number <= source_document.page_count:
                problems.append(f"源页码超出范围：{source_number}")
                continue
            out_page = output_document.load_page(position)
            src_page = source_document.load_page(source_number - 1)
            mismatch = _page_mismatch(out_page, src_page)
            if mismatch:
                problems.append(
                    f"第 {position + 1} 个输出页应对应源第 {source_number} 页，但{mismatch}"
                )
    finally:
        output_document.close()
        source_document.close()
    return problems


def _temporary_target(target: Path) -> Path:
    return target.with_name(f"{target.name}.part.pdf")


def extract_pages(
    source: Path | str,
    target: Path | str,
    page_numbers: Iterable[int],
    *,
    title: str | None = None,
) -> Path:
    """按给定页码顺序从源 PDF 提取页面，生成新的 PDF。

    Args:
        source: 源 PDF，只会被读取。
        target: 输出 PDF 路径，父目录会自动创建。
        page_numbers: 页码顺序，从 1 开始；重复页码是允许的。
        title: 写入输出文件元数据的标题，默认用输出文件名。

    Returns:
        实际写入的输出路径。

    Raises:
        PdfSplitError: 源文件不可读、页码非法或写入失败。
    """
    numbers = list(page_numbers)
    if not numbers:
        raise PdfSplitError("没有需要提取的页码")

    source_path = Path(source)
    target_path = Path(target)
    document = _load_document(source_path)
    try:
        for number in numbers:
            if isinstance(number, bool) or not isinstance(number, int):
                raise PdfSplitError(f"页码必须是整数：{number!r}")
            if not 1 <= number <= document.page_count:
                raise PdfSplitError(
                    f"页码 {number} 超出范围（源文件共 {document.page_count} 页）"
                )

        target_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = _temporary_target(target_path)
        output = pymupdf.open()
        try:
            for number in numbers:
                output.insert_pdf(document, from_page=number - 1, to_page=number - 1)
            output.set_metadata(
                {
                    "title": title or target_path.stem,
                    "creator": "SmartPrint",
                    "producer": "SmartPrint（PyMuPDF）",
                    "subject": "SmartPrint 批量双面打印",
                }
            )
            if output.page_count != len(numbers):
                raise PdfSplitError(
                    f"提取页数异常：期望 {len(numbers)} 页，实际 {output.page_count} 页"
                )
            output.save(str(temporary), garbage=3, deflate=True)
        finally:
            output.close()

        try:
            os.replace(temporary, target_path)
        except OSError as exc:  # pragma: no cover - 磁盘/权限问题
            temporary.unlink(missing_ok=True)
            raise PdfSplitError(f"无法写入输出文件（{target_path.name}）：{exc}") from exc
    finally:
        document.close()

    return target_path


def file_sha256(path: Path | str, chunk_size: int = 1 << 20) -> str:
    """计算文件 SHA-256，用于确认源文件没有被改动。"""
    import hashlib

    digest = hashlib.sha256()
    with open(Path(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()
