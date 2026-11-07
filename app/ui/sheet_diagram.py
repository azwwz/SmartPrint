"""纸张正反面示意图与翻面方向提示（阶段八）。

readme 阶段八要求「增加纸张正反面示意图」「增加纸张翻面方向提示」。

这个控件只做一件事：把数据库里的纸张状态画成两张纸的样子，让用户一眼看出

- 这一张纸的正面是哪一页、背面是哪一页；
- 哪一面已经确认、哪一面还未打印、哪一面被作废；
- 现在轮到哪一面，以及翻面时应该怎么放纸。

翻面方向（沿长边 / 沿短边）必须按实际打印机实测，readme 6.3 已经强调过：
不同打印机的进纸方式不一样，程序不能假设。所以这里只给「按配置的提示」，
并明确写「首次使用请先试打一张确认方向」。
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from app.core.duplex import (
    FACE_CONFIRMED,
    FACE_PENDING,
    FACE_STATUS_LABELS,
    FACE_SUBMITTED,
    FACE_VOID,
    round_label,
)

#: 各面状态的配色：(填充色, 边框色, 文字色)
_STATE_COLORS: dict[str, tuple[str, str, str]] = {
    FACE_PENDING: ("#F7F7F7", "#BDBDBD", "#616161"),
    FACE_SUBMITTED: ("#FFF8E1", "#EF6C00", "#8D4E00"),
    FACE_CONFIRMED: ("#E8F5E9", "#2E7D32", "#1B5E20"),
    FACE_VOID: ("#FFEBEE", "#C62828", "#B71C1C"),
}

_EMPTY_COLORS = ("#FAFAFA", "#E0E0E0", "#9E9E9E")

_CURRENT_BORDER = "#1565C0"


def flip_instruction_text(direction: str) -> str:
    """翻面提示：说明朝哪个方向翻、放回去时注意什么。"""
    if direction == "short_edge":
        return (
            "沿短边翻面：把整叠纸上下翻（像翻台历），上边变下边。"
            "放回进纸托盘时，仍然让原来先进纸的那条边先进入打印机。"
        )
    return (
        "沿长边翻面：把整叠纸左右翻（像翻书），左边变右边。"
        "放回进纸托盘时，仍然让原来先进纸的那条边先进入打印机。"
    )


def flip_scope_text(direction: str, *, scope: str, sheet_number: int | None = None,
                    round_index: int = 0) -> str:
    """把「翻哪一叠纸、怎么翻」拼成一句完整提示。"""
    if sheet_number:
        subject = f"请只取出{round_label(round_index)}的第 {sheet_number} 张纸"
    else:
        subject = f"请取出{scope}"
    return f"{subject}翻面。{flip_instruction_text(direction)}首次使用请先试打一张确认方向。"


class SheetDiagramWidget(QWidget):
    """一张纸的正反面示意图。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._sheet_number: int | None = None
        self._front_page: int | None = None
        self._back_page: int | None = None
        self._front_state = ""
        self._back_state = ""
        self._current_side = ""
        self._round_index = 0
        self._flip_direction = "long_edge"
        self.setMinimumHeight(190)
        self.setMinimumWidth(320)

    # -- 数据 ---------------------------------------------------------------

    def set_sheet(
        self,
        *,
        sheet_number: int | None,
        front_page: int | None,
        back_page: int | None,
        front_state: str = "",
        back_state: str = "",
        current_side: str = "",
        round_index: int = 0,
        flip_direction: str = "long_edge",
    ) -> None:
        """设置要显示的纸张；传 None 表示清空。"""
        self._sheet_number = sheet_number
        self._front_page = front_page
        self._back_page = back_page
        self._front_state = front_state
        self._back_state = back_state
        self._current_side = current_side
        self._round_index = round_index
        self._flip_direction = flip_direction
        self.update()

    def clear(self) -> None:
        self.set_sheet(
            sheet_number=None, front_page=None, back_page=None, current_side=""
        )

    @property
    def sheet_number(self) -> int | None:
        return self._sheet_number

    @property
    def description(self) -> str:
        """给测试与无障碍阅读用的文字描述。"""
        if self._sheet_number is None:
            return "尚未选择纸张"
        front = f"正面第 {self._front_page} 页" if self._front_page else "正面无内容"
        back = f"背面第 {self._back_page} 页" if self._back_page else "背面无内容"
        return (
            f"{round_label(self._round_index)} 第 {self._sheet_number} 张纸："
            f"{front}（{FACE_STATUS_LABELS.get(self._front_state, '未开始')}），"
            f"{back}（{FACE_STATUS_LABELS.get(self._back_state, '未开始')}）"
        )

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt 命名风格
        return QSize(360, 200)

    # -- 绘制 ---------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名风格
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#FFFFFF"))

        if self._sheet_number is None:
            painter.setPen(QColor("#9E9E9E"))
            font = painter.font()
            font.setPointSize(10)
            painter.setFont(font)
            painter.drawText(self.rect(), Qt.AlignCenter, "开始打印后，这里显示当前纸张的正反面")
            return

        margin = 12
        title_height = 22
        footer_height = 34
        available = self.rect().adjusted(margin, margin, -margin, -margin)
        card_width = min(120.0, (available.width() - 70) / 2)
        card_height = min(
            130.0, available.height() - title_height - footer_height
        )
        top = available.top() + title_height
        left_card = QRectF(available.left() + 6, top, card_width, card_height)
        right_card = QRectF(available.right() - 6 - card_width, top, card_width, card_height)

        # 标题
        painter.setPen(QColor("#37474F"))
        title_font = QFont(self.font())
        title_font.setBold(True)
        title_font.setPointSize(max(9, title_font.pointSize()))
        painter.setFont(title_font)
        painter.drawText(
            QRectF(available.left(), available.top(), available.width(), title_height),
            Qt.AlignLeft | Qt.AlignVCenter,
            f"{round_label(self._round_index)}：第 {self._sheet_number} 张纸",
        )

        self._draw_face(
            painter, left_card, "正面", self._front_page, self._front_state, side="front"
        )
        self._draw_face(
            painter, right_card, "背面", self._back_page, self._back_state, side="back"
        )
        self._draw_flip_arrow(painter, left_card, right_card, top + card_height / 2)

        # 底部：翻面方向提示
        painter.setPen(QColor("#455A64"))
        hint_font = QFont(self.font())
        hint_font.setPointSize(max(8, hint_font.pointSize() - 1))
        painter.setFont(hint_font)
        footer = QRectF(
            available.left(),
            top + card_height + 4,
            available.width(),
            footer_height,
        )
        painter.drawText(
            footer,
            Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap,
            "翻面方向：" + ("沿短边（上下翻）" if self._flip_direction == "short_edge"
                            else "沿长边（左右翻）"),
        )

    def _draw_face(
        self,
        painter: QPainter,
        rect: QRectF,
        label: str,
        page: int | None,
        state: str,
        *,
        side: str,
    ) -> None:
        fill, border, text_color = _STATE_COLORS.get(state, _EMPTY_COLORS)
        is_current = side == self._current_side and bool(state)
        pen = QPen(QColor(_CURRENT_BORDER if is_current else border))
        pen.setWidth(3 if is_current else 1)
        painter.setPen(pen)
        painter.setBrush(QColor(fill))
        painter.drawRoundedRect(rect, 6, 6)

        # 顶部小标签
        painter.setPen(QColor(text_color))
        label_font = QFont(self.font())
        label_font.setPointSize(max(8, label_font.pointSize() - 1))
        painter.setFont(label_font)
        painter.drawText(
            rect.adjusted(6, 4, -6, 0),
            Qt.AlignLeft | Qt.AlignTop,
            label + ("　▶ 当前" if is_current else ""),
        )

        # 页码
        if page:
            page_font = QFont(self.font())
            page_font.setBold(True)
            page_font.setPointSize(max(14, page_font.pointSize() + 6))
            painter.setFont(page_font)
            painter.drawText(rect.adjusted(4, 10, -4, -14), Qt.AlignCenter, f"第 {page} 页")
        else:
            painter.drawText(rect.adjusted(4, 12, -4, -12), Qt.AlignCenter, "无内容")

        # 状态文字
        status_text = FACE_STATUS_LABELS.get(state, "未开始") if state else "未开始"
        painter.setPen(QColor(text_color))
        status_font = QFont(self.font())
        status_font.setPointSize(max(8, status_font.pointSize() - 1))
        status_font.setBold(state in (FACE_CONFIRMED, FACE_VOID))
        painter.setFont(status_font)
        painter.drawText(
            rect.adjusted(4, 0, -4, -6), Qt.AlignHCenter | Qt.AlignBottom, status_text
        )

        # 作废：画一条斜线
        if state == FACE_VOID:
            pen = QPen(QColor("#C62828"))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.drawLine(rect.topLeft() + QPointF(8, 8), rect.bottomRight() - QPointF(8, 8))

    def _draw_flip_arrow(
        self, painter: QPainter, left: QRectF, right: QRectF, y: float
    ) -> None:
        """两面之间画一个翻转箭头。"""
        start = QPointF(left.right() + 6, y)
        end = QPointF(right.left() - 6, y)
        if end.x() - start.x() < 10:
            return
        pen = QPen(QColor("#78909C"))
        pen.setWidth(1)
        pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        middle = (start.x() + end.x()) / 2
        painter.drawLine(QPointF(start.x(), y), QPointF(end.x() - 6, y))

        # 箭头
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#78909C"))
        arrow = QPolygonF(
            [QPointF(end.x() - 6, y - 4), QPointF(end.x(), y), QPointF(end.x() - 6, y + 4)]
        )
        painter.drawPolygon(arrow)

        # 中间的「翻面」两个小字
        text_font = QFont(self.font())
        text_font.setPointSize(max(7, text_font.pointSize() - 2))
        painter.setFont(text_font)
        painter.setPen(QColor("#607D8B"))
        painter.drawText(
            QRectF(middle - 16, y - 18, 32, 14), Qt.AlignCenter, "翻面"
        )
