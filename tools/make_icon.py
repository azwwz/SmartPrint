"""生成 SmartPrint 的程序图标（阶段八：打包完善）。

用法：

    python tools\\make_icon.py

产物：

    resources/smartprint.ico     多尺寸图标（16/24/32/48/64/128/256），给 exe 与窗口用
    resources/smartprint-256.png 同一张图的 PNG，方便预览或在别处使用

为什么自己画：

- 项目里没有现成的图标文件，打包出来的 exe 默认是 PyInstaller 的「蛇＋箱子」；
- 开发用的 py313 环境没装 Pillow（也不该为了一个图标多装一个依赖），
  所以这里用 PySide6 的 QPainter 绘制，再把像素手动拼成 ICO 容器。

ICO 容器本身很简单：ICONDIR 头 + 每张图一个 ICONDIRENTRY + 每张图的数据。
32 位图的数据是「BITMAPINFOHEADER + 自下而上的 BGRA 行 + AND 掩码」，
Alpha 通道由 BGRA 自己带，AND 掩码全 0 即可（表示「不透明，别再挖洞」）。
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QColor,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPen,
)

ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)

#: 图标配色：和界面里的蓝色系一致
_BG_TOP = "#42A5F5"
_BG_BOTTOM = "#0D47A1"
_PAGE_FILL = "#FFFFFF"
_PAGE_EDGE = "#BBDEFB"
_LINE_COLOR = "#1565C0"
_ACCENT = "#FFB300"


def _draw_icon(size: int) -> QImage:
    """按给定像素尺寸画一张图标（抗锯齿只在足够大时才开，小图更锐利）。"""
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, size >= 32)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

    scale = float(size)
    # 背景：圆角方块 + 竖向渐变
    gradient = QLinearGradient(0.0, 0.0, 0.0, scale)
    gradient.setColorAt(0.0, QColor(_BG_TOP))
    gradient.setColorAt(1.0, QColor(_BG_BOTTOM))
    painter.setPen(Qt.NoPen)
    painter.setBrush(gradient)
    radius = scale * 0.20
    painter.drawRoundedRect(QRectF(0.0, 0.0, scale, scale), radius, radius)

    # 两张纸：后面一张浅色、前面一张白色，错开表示「正反两面」
    page_w = scale * 0.40
    page_h = scale * 0.52
    back = QRectF(scale * 0.42, scale * 0.20, page_w, page_h)
    front = QRectF(scale * 0.16, scale * 0.28, page_w, page_h)

    painter.setPen(QPen(QColor(_PAGE_EDGE), max(1.0, scale * 0.012)))
    painter.setBrush(QColor(228, 240, 252))
    painter.drawRoundedRect(back, scale * 0.035, scale * 0.035)

    painter.setPen(QPen(QColor(_PAGE_EDGE), max(1.0, scale * 0.012)))
    painter.setBrush(QColor(_PAGE_FILL))
    painter.drawRoundedRect(front, scale * 0.035, scale * 0.035)

    # 纸上的「文字行」：小尺寸画不下就不画，避免糊成一团
    if size >= 24:
        line_height = max(1.4, scale * 0.045)
        painter.setPen(Qt.NoPen)
        margin = scale * 0.055
        for index in range(3):
            width_ratio = (0.86, 0.70, 0.78)[index]
            line_rect = QRectF(
                front.left() + margin,
                front.top() + margin + index * line_height * 1.9,
                (front.width() - margin * 2) * width_ratio,
                line_height,
            )
            color = QColor(_LINE_COLOR)
            color.setAlpha(200 if index == 0 else 140)
            painter.setBrush(color)
            painter.drawRoundedRect(line_rect, line_height / 2, line_height / 2)

    # 右下角一个翻面箭头（小尺寸用实心圆点替代，保证还看得出方向）
    if size >= 32:
        pen = QPen(QColor(_ACCENT), max(1.6, scale * 0.055))
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        arc = QRectF(scale * 0.46, scale * 0.52, scale * 0.34, scale * 0.34)
        painter.drawArc(arc, 210 * 16, -215 * 16)  # 左下开口的圆弧
        # 箭头尖
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(_ACCENT))
        tip = QPointF(arc.center().x() + arc.width() * 0.34, arc.center().y() - arc.height() * 0.42)
        painter.save()
        painter.translate(tip)
        painter.rotate(-25)
        arrow_size = scale * 0.10
        painter.drawPolygon(
            [
                QPointF(-arrow_size * 0.5, -arrow_size * 0.9),
                QPointF(arrow_size * 0.75, 0.0),
                QPointF(-arrow_size * 0.5, arrow_size * 0.9),
            ]
        )
        painter.restore()
    elif size >= 24:
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(_ACCENT))
        dot = scale * 0.13
        painter.drawEllipse(
            QRectF(scale * 0.70 - dot / 2, scale * 0.70 - dot / 2, dot, dot)
        )

    painter.end()
    return image


def _dib_bytes(image: QImage) -> bytes:
    """把 QImage 转成 ICO 里用的 32 位 DIB（BITMAPINFOHEADER + BGRA + AND 掩码）。"""
    image = image.convertToFormat(QImage.Format_ARGB32)
    width = image.width()
    height = image.height()

    header = struct.pack(
        "<IiiHHIIiiII",
        40,            # biSize
        width,         # biWidth
        height * 2,    # biHeight：颜色数据 + 掩码，所以是两倍
        1,             # biPlanes
        32,            # biBitCount
        0,             # biCompression = BI_RGB
        width * height * 4,  # biSizeImage
        0, 0, 0, 0,    # 分辨率与调色板
    )

    rows = []
    stride = image.bytesPerLine()
    raw = image.constBits().tobytes()
    for y in range(height - 1, -1, -1):  # DIB 是自下而上
        start = y * stride
        rows.append(raw[start : start + width * 4])  # ARGB32 在小端下即 BGRA
    pixels = b"".join(rows)

    mask_stride = ((width + 31) // 32) * 4
    mask = b"\x00" * (mask_stride * height)
    return header + pixels + mask


def write_ico(images: list[QImage], path: Path) -> Path:
    """把若干张图写成一个多尺寸 ICO 文件。"""
    blobs = [_dib_bytes(image) for image in images]
    count = len(images)
    offset = 6 + 16 * count

    entries = []
    for image, blob in zip(images, blobs):
        width = 0 if image.width() >= 256 else image.width()
        height = 0 if image.height() >= 256 else image.height()
        entries.append(
            struct.pack(
                "<BBBBHHII",
                width,
                height,
                0,      # 调色板颜色数：真彩色写 0
                0,      # 保留
                1,      # 色彩平面
                32,     # 位深
                len(blob),
                offset,
            )
        )
        offset += len(blob)

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(struct.pack("<HHH", 0, 1, count))
        for entry in entries:
            handle.write(entry)
        for blob in blobs:
            handle.write(blob)
    return path


def main() -> int:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)
    images = [_draw_icon(size) for size in ICON_SIZES]

    resources = ROOT / "resources"
    ico_path = write_ico(images, resources / "smartprint.ico")
    png_path = resources / "smartprint-256.png"
    images[-1].save(str(png_path), "PNG")

    app.setWindowIcon  # 占位：避免 linter 误报未使用的 app
    print(f"已生成：{ico_path}（{ico_path.stat().st_size} 字节，{len(images)} 个尺寸）")
    print(f"已生成：{png_path}（{png_path.stat().st_size} 字节）")
    for size in ICON_SIZES:
        print(f"  - {size}×{size}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
