"""PDF 检查：读取页数、页面尺寸，并做最基本的有效性校验。

阶段二只关心「这个 PDF 能不能用」以及「页数和纸张尺寸是多少」。
页面拆分、空白页判定等留到阶段四。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

try:  # PyMuPDF 2.x 推荐使用 pymupdf 包名
    import pymupdf
except ImportError:  # pragma: no cover - 兼容旧版本
    import fitz as pymupdf  # type: ignore[no-redef]

logger = logging.getLogger("smartprint.core.pdf_inspector")

#: 常见纸张尺寸（单位 pt，纵向：宽 x 高）
PAPER_SIZES_PT: dict[str, tuple[float, float]] = {
    "A3": (841.89, 1190.55),
    "A4": (595.28, 841.89),
    "A5": (419.53, 595.28),
    "Letter": (612.0, 792.0),
    "Legal": (612.0, 1008.0),
}

#: 尺寸比对容差（pt），约 0.1mm
SIZE_TOLERANCE_PT = 3.0


class PdfInspectionError(Exception):
    """PDF 无法作为打印任务使用。"""


@dataclass
class PdfInspection:
    """PDF 检查结果。"""

    page_count: int
    width_pt: float
    height_pt: float
    paper_size: str
    orientation: str
    is_encrypted: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def size_text(self) -> str:
        """界面显示用文本，例如「A4 纵向（595x842pt）」。"""
        return f"{self.paper_size} {self.orientation}（{self.width_pt:.0f}x{self.height_pt:.0f}pt）"


def _match_paper_size(width_pt: float, height_pt: float) -> str:
    """根据页面宽高判断纸张大小，判断不出时返回「自定义」。"""
    short_side, long_side = sorted((width_pt, height_pt))
    for name, (std_width, std_height) in PAPER_SIZES_PT.items():
        if (
            abs(short_side - std_width) <= SIZE_TOLERANCE_PT
            and abs(long_side - std_height) <= SIZE_TOLERANCE_PT
        ):
            return name
    return f"自定义 {short_side:.0f}x{long_side:.0f}pt"


def inspect_pdf(path: Path) -> PdfInspection:
    """打开 PDF 并读取基本信息，失败时抛出 PdfInspectionError。"""
    pdf_path = Path(path)

    try:
        document = pymupdf.open(pdf_path)
    except Exception as exc:  # PyMuPDF 会抛出多种异常类型
        raise PdfInspectionError(f"无法打开 PDF：{exc}") from exc

    try:
        if document.needs_pass:
            raise PdfInspectionError("PDF 已加密，需要密码才能打开")

        page_count = document.page_count
        if page_count <= 0:
            raise PdfInspectionError("PDF 没有任何页面")

        first_page = document.load_page(0)
        first_rect = first_page.rect
        width_pt, height_pt = float(first_rect.width), float(first_rect.height)
        if width_pt <= 1 or height_pt <= 1:
            raise PdfInspectionError(f"PDF 首页尺寸异常：{width_pt:.1f}x{height_pt:.1f}pt")

        warnings: list[str] = []
        mixed_sizes = _count_other_sizes(document, first_rect)
        if mixed_sizes:
            warnings.append(f"{mixed_sizes} 页的尺寸与首页不同（文档包含混合页面尺寸）")

        inspection = PdfInspection(
            page_count=page_count,
            width_pt=width_pt,
            height_pt=height_pt,
            paper_size=_match_paper_size(width_pt, height_pt),
            orientation="纵向" if height_pt >= width_pt else "横向",
            is_encrypted=False,
            warnings=warnings,
        )
        return inspection

    except PdfInspectionError:
        raise
    except Exception as exc:
        raise PdfInspectionError(f"读取 PDF 页面信息失败：{exc}") from exc
    finally:
        document.close()


def _count_other_sizes(document, first_rect) -> int:
    """统计与首页尺寸不同的页数（容差 SIZE_TOLERANCE_PT）。"""
    count = 0
    for index in range(1, document.page_count):
        rect = document.load_page(index).rect
        if (
            abs(rect.width - first_rect.width) > SIZE_TOLERANCE_PT
            or abs(rect.height - first_rect.height) > SIZE_TOLERANCE_PT
        ):
            count += 1
    return count
