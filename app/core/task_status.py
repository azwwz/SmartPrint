"""打印任务状态定义。

对应 readme「二、2 统一管理所有打印任务」中的状态表。
数据库中保存英文枚举值（稳定、便于后续扩展），界面上显示中文标签。
"""

from __future__ import annotations

from enum import Enum


class TaskStatus(str, Enum):
    """打印任务状态。"""

    PENDING = "pending"                # 待处理：PDF 已接收，尚未确认打印
    READY = "ready"                    # 已准备：已完成打印参数设置
    PRINTING_FRONT = "printing_front"  # 打印正面
    WAITING_FLIP = "waiting_flip"      # 等待翻面
    PRINTING_BACK = "printing_back"    # 打印背面
    REPRINT = "reprint"                # 待补打：存在需要重新打印的物理纸张
    COMPLETED = "completed"            # 已完成
    PAUSED = "paused"                  # 已暂停
    ERROR = "error"                    # 异常待处理
    CANCELLED = "cancelled"            # 已取消

    @property
    def label(self) -> str:
        """中文显示名。"""
        return STATUS_LABELS[self]


STATUS_LABELS: dict[TaskStatus, str] = {
    TaskStatus.PENDING: "待处理",
    TaskStatus.READY: "已准备",
    TaskStatus.PRINTING_FRONT: "打印正面",
    TaskStatus.WAITING_FLIP: "等待翻面",
    TaskStatus.PRINTING_BACK: "打印背面",
    TaskStatus.REPRINT: "待补打",
    TaskStatus.COMPLETED: "已完成",
    TaskStatus.PAUSED: "已暂停",
    TaskStatus.ERROR: "异常待处理",
    TaskStatus.CANCELLED: "已取消",
}

#: 需要继续跟进（未结束）的状态
ACTIVE_STATUSES = frozenset(
    {
        TaskStatus.PENDING,
        TaskStatus.READY,
        TaskStatus.PRINTING_FRONT,
        TaskStatus.WAITING_FLIP,
        TaskStatus.PRINTING_BACK,
        TaskStatus.REPRINT,
        TaskStatus.PAUSED,
        TaskStatus.ERROR,
    }
)

#: 已经结束、不再参与打印流程的状态
CLOSED_STATUSES = frozenset({TaskStatus.COMPLETED, TaskStatus.CANCELLED})


def parse_status(value: str | TaskStatus) -> TaskStatus:
    """把数据库中的字符串转换为枚举，未知值按「待处理」处理并保留原始值可查。"""
    if isinstance(value, TaskStatus):
        return value
    try:
        return TaskStatus(value)
    except ValueError:
        return TaskStatus.PENDING


def status_label(value: str | TaskStatus) -> str:
    """返回中文标签。"""
    return parse_status(value).label
