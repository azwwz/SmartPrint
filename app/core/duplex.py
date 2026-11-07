"""手动双面打印的纸张 / 页面状态模型（阶段六 + 阶段七，纯逻辑）。

核心是 readme「三、核心设计原则」里的三条：

1. **任何时候最多只有一个已经提交、但尚未经过用户确认的页面。**
2. **队列状态不能推进流程，只有用户确认「这一面完整出纸」才能推进。**
3. **异常以整张物理纸为单位**：任何一面异常，正反两面一起作废、一起补打。

因此这里把一次双面打印拆成「一张物理纸 × 两个打印面」：

    第 1 张：正面第 1 页，背面第 2 页
    第 2 张：正面第 3 页，背面第 4 页
    ...

打印分轮次进行（round_index）：

    0        正常循环：按批次先打完一批的正面，用户翻面确认后再打背面
    1, 2, …  第 N 轮补打：正常循环里作废的纸张，等正常循环全部结束后逐张补打

状态机（每个打印面）：

    pending ──提交命令──> submitted ──用户确认完整出纸──> confirmed
       │                      │
       │                      └──用户报告异常──> void（整张纸两面一起作废，进下一轮）
       └──预检未通过，不改变状态（不会自动重试）

任务阶段（session.phase）：

    front        可以提交当前轮次的下一个正面
    confirm      已经提交了一面，等待用户确认（此时不允许提交下一页）
    waiting_flip 当前翻面组（一批纸或补打的一张纸）正面完成，等待用户翻面确认
    back         可以提交当前轮次的下一个背面
    paused       用户暂停
    completed    全部纸张正反两面均已确认（补打轮次也全部完成）
    cancelled    用户取消
    error        需要人工介入的异常（正常作废流程不会走到这里）

本模块只描述数据与规则，不访问数据库、不调用打印程序；真正的编排在
app/services/duplex_service.py，界面在 app/ui/duplex_dialog.py。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.core.page_planner import (
    FACE_BACK,
    FACE_FRONT,
    face_label,
    page_file_name,
)

# ---------------------------------------------------------------------------
# 打印面状态
# ---------------------------------------------------------------------------

FACE_PENDING = "pending"        # 还没提交
FACE_SUBMITTED = "submitted"    # 命令已交给 Windows，等待用户确认纸张
FACE_CONFIRMED = "confirmed"    # 用户确认这一面完整出纸
FACE_VOID = "void"              # 整张纸作废（正反两面一起标记）

FACE_STATUS_LABELS: dict[str, str] = {
    FACE_PENDING: "待打印",
    FACE_SUBMITTED: "待确认",
    FACE_CONFIRMED: "已确认",
    FACE_VOID: "已作废",
}

#: 还没结束的打印面状态（current 取第一个）
OPEN_FACE_STATUSES = (FACE_PENDING, FACE_SUBMITTED)

# ---------------------------------------------------------------------------
# 轮次
# ---------------------------------------------------------------------------

ROUND_NORMAL = 0


def round_label(round_index: int) -> str:
    """轮次文案：0 是正常打印，1 以上是第 N 轮补打。"""
    if round_index <= ROUND_NORMAL:
        return "正常打印"
    return f"第 {round_index} 轮补打"


def round_short_label(round_index: int) -> str:
    """表格里用的短文案。"""
    if round_index <= ROUND_NORMAL:
        return "正常"
    return f"补打 {round_index}"


def flip_group_key(round_index: int, batch_index: int, sheet_number: int) -> str:
    """翻面确认的分组键。

    正常循环按批次翻面（一批纸一起翻），补打循环按单张纸翻面。
    """
    if round_index <= ROUND_NORMAL:
        return f"0:{batch_index}"
    return f"{round_index}:{sheet_number}"


# ---------------------------------------------------------------------------
# 任务阶段
# ---------------------------------------------------------------------------

PHASE_FRONT = "front"
PHASE_CONFIRM = "confirm"
PHASE_WAITING_FLIP = "waiting_flip"
PHASE_BACK = "back"
PHASE_PAUSED = "paused"
PHASE_COMPLETED = "completed"
PHASE_CANCELLED = "cancelled"
PHASE_ERROR = "error"

PHASE_LABELS: dict[str, str] = {
    PHASE_FRONT: "打印正面",
    PHASE_CONFIRM: "等待用户确认",
    PHASE_WAITING_FLIP: "等待翻面",
    PHASE_BACK: "打印背面",
    PHASE_PAUSED: "已暂停",
    PHASE_COMPLETED: "已完成",
    PHASE_CANCELLED: "已取消",
    PHASE_ERROR: "异常待处理",
}

#: 仍在进行中的阶段（可以暂停 / 取消）
ACTIVE_PHASES = frozenset(
    {PHASE_FRONT, PHASE_CONFIRM, PHASE_WAITING_FLIP, PHASE_BACK}
)

#: 已经结束的阶段
FINISHED_PHASES = frozenset({PHASE_COMPLETED, PHASE_CANCELLED, PHASE_ERROR})

#: 允许开始 / 恢复的阶段
RESUMABLE_PHASES = ACTIVE_PHASES | {PHASE_PAUSED}

#: 异常记录里「用户选择的处理方式」：第一版只有这一种
ANOMALY_ACTION_VOID_SHEET = "整张纸作废，正反两面一起进入补打清单"


def face_status_label(status: str) -> str:
    return FACE_STATUS_LABELS.get(status, status)


def phase_label(phase: str) -> str:
    return PHASE_LABELS.get(phase, phase)


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class DuplexFace:
    """一个物理纸张的其中一个打印面（每一轮各有一份记录）。"""

    task_uid: str
    step_index: int          # 打印顺序（0 开始），跨轮次连续递增
    batch_index: int         # 所属批次，从 1 开始
    sheet_number: int        # 物理纸张全局序号，从 1 开始
    face: str                # front / back
    page_number: int         # 对应 PDF 页码，从 1 开始
    round_index: int = ROUND_NORMAL   # 0 = 正常打印，N = 第 N 轮补打
    status: str = FACE_PENDING
    job_id: int | None = None        # print_jobs.id（最近一次提交）
    submitted_at: datetime | None = None
    confirmed_at: datetime | None = None
    note: str = ""
    id: int | None = None

    @property
    def face_text(self) -> str:
        return face_label(self.face)

    @property
    def page_file_name(self) -> str:
        return page_file_name(self.page_number)

    @property
    def status_text(self) -> str:
        return face_status_label(self.status)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_FACE_STATUSES

    @property
    def is_confirmed(self) -> bool:
        return self.status == FACE_CONFIRMED

    @property
    def is_void(self) -> bool:
        return self.status == FACE_VOID

    @property
    def is_reprint(self) -> bool:
        return self.round_index > ROUND_NORMAL

    @property
    def round_text(self) -> str:
        return round_label(self.round_index)

    @property
    def flip_group(self) -> str:
        return flip_group_key(self.round_index, self.batch_index, self.sheet_number)

    @property
    def label(self) -> str:
        return f"第 {self.sheet_number} 张{self.face_text}（第 {self.page_number} 页）"

    @property
    def full_label(self) -> str:
        if self.is_reprint:
            return f"{self.round_text} · {self.label}"
        return self.label


@dataclass
class DuplexSheet:
    """界面展示用的一张物理纸（把两个打印面拼在一起）。"""

    number: int
    batch_index: int
    front_page: int | None
    back_page: int | None
    front_status: str = FACE_PENDING
    back_status: str = FACE_PENDING
    round_index: int = ROUND_NORMAL

    @property
    def has_back(self) -> bool:
        return self.back_page is not None

    @property
    def voided(self) -> bool:
        return self.front_status == FACE_VOID or self.back_status == FACE_VOID

    @property
    def confirmed(self) -> bool:
        if self.voided:
            return False
        front_ok = self.front_page is None or self.front_status == FACE_CONFIRMED
        back_ok = self.back_page is None or self.back_status == FACE_CONFIRMED
        return front_ok and back_ok

    @property
    def round_text(self) -> str:
        return round_short_label(self.round_index)

    @property
    def front_text(self) -> str:
        return str(self.front_page) if self.front_page is not None else "无"

    @property
    def back_text(self) -> str:
        return str(self.back_page) if self.back_page is not None else "无（空白背面）"

    @property
    def pages_text(self) -> str:
        if self.has_back:
            return f"正面 {self.front_page} / 背面 {self.back_page}"
        return f"正面 {self.front_page} / 无背面"

    @property
    def status_text(self) -> str:
        if self.voided:
            return "已作废（整张纸补打）"
        if self.confirmed:
            return "已完成"
        if FACE_SUBMITTED in (self.front_status, self.back_status):
            return "等待确认"
        if FACE_CONFIRMED in (self.front_status, self.back_status):
            return "部分完成"
        return "待打印"


@dataclass
class DuplexAnomaly:
    """一次异常记录（readme 7.6：异常必须以整张纸为单位完整留痕）。"""

    task_uid: str
    round_index: int = ROUND_NORMAL
    sheet_number: int = 0
    face: str = FACE_FRONT
    front_page: int | None = None
    back_page: int | None = None
    reason: str = ""
    action: str = ANOMALY_ACTION_VOID_SHEET
    created_at: datetime = field(default_factory=datetime.now)
    resolved_round: int | None = None
    resolved_at: datetime | None = None
    result: str = ""
    id: int | None = None

    @property
    def round_text(self) -> str:
        return round_label(self.round_index)

    @property
    def face_text(self) -> str:
        return face_label(self.face)

    @property
    def pages_text(self) -> str:
        front = self.front_page if self.front_page is not None else "无"
        back = self.back_page if self.back_page is not None else "无"
        return f"正面 {front} / 背面 {back}"

    @property
    def resolved(self) -> bool:
        return self.resolved_at is not None

    @property
    def result_text(self) -> str:
        if self.resolved:
            return self.result or "补打完成"
        return "等待补打"

    def to_text(self) -> str:
        parts = [
            self.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            self.round_text,
            f"第 {self.sheet_number} 张",
            self.pages_text,
            f"异常面：{self.face_text}",
        ]
        if self.reason:
            parts.append(f"原因：{self.reason}")
        parts.append(f"结果：{self.result_text}")
        return " · ".join(parts)


@dataclass
class DuplexSession:
    """一次手动双面打印的持久化状态（一个任务最多一条）。"""

    task_uid: str
    phase: str = PHASE_FRONT
    sheets_per_batch: int = 5
    total_sheets: int = 0
    total_batches: int = 0
    flip_confirmed_batch: int = 0   # 已经确认翻面的是哪一批（0 表示还没有）
    flip_confirmed_group: str = ""  # 已经确认翻面的分组键（批次或单张纸）
    current_round: int = ROUND_NORMAL
    current_batch: int = 1
    current_sheet: int | None = None
    current_face: str | None = None
    current_page: int | None = None
    note: str = ""
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    finished_at: datetime | None = None
    id: int | None = None

    @property
    def phase_text(self) -> str:
        return phase_label(self.phase)

    @property
    def is_active(self) -> bool:
        return self.phase in ACTIVE_PHASES

    @property
    def is_paused(self) -> bool:
        return self.phase == PHASE_PAUSED

    @property
    def is_finished(self) -> bool:
        return self.phase in FINISHED_PHASES

    @property
    def is_completed(self) -> bool:
        return self.phase == PHASE_COMPLETED

    @property
    def flip_confirmed(self) -> bool:
        return bool(self.flip_confirmed_batch) or bool(self.flip_confirmed_group)

    @property
    def round_text(self) -> str:
        return round_label(self.current_round)


@dataclass
class DuplexSnapshot:
    """一份双面打印的全部状态（session + 每一轮的打印面 + 异常记录）。"""

    session: DuplexSession
    faces: tuple[DuplexFace, ...] = ()
    document_name: str = ""
    task_page_count: int = 0
    anomalies: tuple[DuplexAnomaly, ...] = ()

    # -- 轮次 ---------------------------------------------------------------

    @property
    def latest_round(self) -> int:
        """已经创建出来的最高轮次（0 表示只有正常循环）。"""
        return max((face.round_index for face in self.faces), default=ROUND_NORMAL)

    @property
    def rounds(self) -> tuple[int, ...]:
        return tuple(sorted({face.round_index for face in self.faces}))

    @property
    def has_reprint(self) -> bool:
        return self.latest_round > ROUND_NORMAL

    @property
    def live_faces(self) -> tuple[DuplexFace, ...]:
        """每张纸的有效打印面：取该纸轮次最大的那一份（旧的作废行只是记录）。"""
        latest: dict[int, int] = {}
        for face in self.faces:
            if face.round_index > latest.get(face.sheet_number, -1):
                latest[face.sheet_number] = face.round_index
        return tuple(
            face
            for face in self.faces
            if face.round_index == latest.get(face.sheet_number, ROUND_NORMAL)
        )

    def faces_of_sheet(self, sheet_number: int, round_index: int | None = None) -> tuple[DuplexFace, ...]:
        items = [face for face in self.faces if face.sheet_number == sheet_number]
        if round_index is not None:
            items = [face for face in items if face.round_index == round_index]
        return tuple(sorted(items, key=lambda face: face.step_index))

    # -- 基础统计 -----------------------------------------------------------

    @property
    def task_uid(self) -> str:
        return self.session.task_uid

    @property
    def phase(self) -> str:
        return self.session.phase

    @property
    def phase_text(self) -> str:
        return phase_label(self.session.phase)

    @property
    def total_faces(self) -> int:
        """有效打印面总数（不含作废的面）。"""
        return len([face for face in self.live_faces if not face.is_void])

    @property
    def confirmed_count(self) -> int:
        return sum(1 for face in self.live_faces if face.is_confirmed)

    @property
    def submitted_count(self) -> int:
        return sum(1 for face in self.live_faces if face.status == FACE_SUBMITTED)

    @property
    def void_count(self) -> int:
        return sum(1 for face in self.live_faces if face.is_void)

    @property
    def percent(self) -> int:
        total = self.total_faces
        if total <= 0:
            return 0
        return int(self.confirmed_count * 100 / total)

    @property
    def progress_text(self) -> str:
        return f"已确认 {self.confirmed_count} / {self.total_faces} 面（{self.percent}%）"

    # -- 物理纸统计（readme 7.1：完成与否按整张纸判断） ----------------------

    @property
    def live_sheets(self) -> tuple[DuplexSheet, ...]:
        return self._sheets_from(self.live_faces)

    @property
    def total_sheets(self) -> int:
        return len(self.live_sheets) or self.session.total_sheets

    @property
    def completed_sheets(self) -> int:
        return sum(1 for sheet in self.live_sheets if sheet.confirmed)

    @property
    def sheet_percent(self) -> int:
        total = self.total_sheets
        if total <= 0:
            return 0
        return int(self.completed_sheets * 100 / total)

    @property
    def sheet_progress_text(self) -> str:
        return (
            f"已完成 {self.completed_sheets} / {self.total_sheets} 张纸"
            f"（{self.sheet_percent}%）"
        )

    # -- 当前打印面 ---------------------------------------------------------

    @property
    def current(self) -> DuplexFace | None:
        """第一个还没有结束的打印面（pending / submitted）。"""
        for face in self.faces:
            if face.is_open:
                return face
        return None

    @property
    def current_face(self) -> DuplexFace | None:
        return self.current

    @property
    def current_round(self) -> int:
        if self.current is not None:
            return self.current.round_index
        return self.session.current_round

    @property
    def round_text(self) -> str:
        return round_label(self.current_round)

    @property
    def is_reprint_round(self) -> bool:
        return self.current_round > ROUND_NORMAL

    @property
    def flip_confirmed_for_current(self) -> bool:
        """当前这一面的翻面是否已经确认。"""
        current = self.current
        if current is None:
            return False
        if self.session.flip_confirmed_group:
            return self.session.flip_confirmed_group == current.flip_group
        # 兼容阶段六写入的旧数据：只记录批次号
        if current.round_index > ROUND_NORMAL:
            return False
        return self.session.flip_confirmed_batch == current.batch_index

    @property
    def awaiting_confirmation(self) -> bool:
        return (
            self.session.phase == PHASE_CONFIRM
            and self.current is not None
            and self.current.status == FACE_SUBMITTED
        )

    @property
    def can_submit(self) -> bool:
        return (
            self.session.phase in (PHASE_FRONT, PHASE_BACK)
            and self.current is not None
            and self.current.status == FACE_PENDING
        )

    @property
    def can_confirm(self) -> bool:
        return self.awaiting_confirmation

    @property
    def can_flip(self) -> bool:
        return self.session.phase == PHASE_WAITING_FLIP and self.current is not None

    @property
    def can_pause(self) -> bool:
        return self.session.phase in ACTIVE_PHASES

    @property
    def can_resume(self) -> bool:
        return self.session.phase == PHASE_PAUSED

    @property
    def can_cancel(self) -> bool:
        return self.session.phase in ACTIVE_PHASES or self.session.phase == PHASE_PAUSED

    @property
    def is_finished(self) -> bool:
        return self.session.phase in FINISHED_PHASES

    @property
    def is_completed(self) -> bool:
        return self.session.phase == PHASE_COMPLETED

    @property
    def has_void(self) -> bool:
        return self.void_count > 0

    # -- 补打清单 -----------------------------------------------------------

    @property
    def reprint_pending_sheets(self) -> tuple[DuplexSheet, ...]:
        """当前轮次里作废、等待下一轮补打的纸张（按纸张号排序）。"""
        return tuple(sheet for sheet in self.live_sheets if sheet.voided)

    @property
    def reprint_pending_count(self) -> int:
        return len(self.reprint_pending_sheets)

    @property
    def reprint_pending_text(self) -> str:
        sheets = self.reprint_pending_sheets
        if not sheets:
            return "无"
        items = [f"第 {sheet.number} 张（{sheet.pages_text}）" for sheet in sheets]
        return "、".join(items)

    @property
    def reprint_round_sheets(self) -> tuple[DuplexSheet, ...]:
        """当前补打轮次要处理的纸张。"""
        if not self.is_reprint_round:
            return ()
        round_index = self.current_round
        return tuple(
            sheet for sheet in self.live_sheets if sheet.round_index == round_index
        )

    @property
    def reprint_round_text(self) -> str:
        if not self.is_reprint_round:
            return "无"
        count = len(self.reprint_round_sheets)
        return f"{round_label(self.current_round)}（共 {count} 张纸）"

    @property
    def anomaly_count(self) -> int:
        return len(self.anomalies)

    @property
    def open_anomaly_count(self) -> int:
        return sum(1 for item in self.anomalies if not item.resolved)

    @property
    def anomaly_text(self) -> str:
        if not self.anomalies:
            return "无"
        return f"共 {self.anomaly_count} 次，待补打 {self.open_anomaly_count} 次"

    # -- 位置 ---------------------------------------------------------------

    @property
    def current_batch(self) -> int:
        if self.current is not None and not self.is_reprint_round:
            return self.current.batch_index
        return self.session.current_batch or 1

    @property
    def current_sheet_number(self) -> int | None:
        return self.current.sheet_number if self.current else None

    @property
    def sheet_in_batch(self) -> int:
        """当前纸张是本批次的第几张（1 开始）。"""
        number = self.current_sheet_number
        if number is None:
            return 0
        return number - (self.current_batch - 1) * self.session.sheets_per_batch

    @property
    def batch_sheet_count(self) -> int:
        """当前批次实际有多少张纸（最后一批可能不足）。"""
        batch = self.current_batch
        count = sum(1 for sheet in self.sheets if sheet.batch_index == batch)
        return count or self.session.sheets_per_batch

    @property
    def batch_position_text(self) -> str:
        if self.is_reprint_round:
            sheets = self.reprint_round_sheets
            index = 0
            number = self.current_sheet_number
            for position, sheet in enumerate(sheets, start=1):
                if sheet.number == number:
                    index = position
                    break
            return f"{round_label(self.current_round)}：第 {index} 张 / 共 {len(sheets)} 张"
        return f"第 {self.current_batch} 批 / 共 {self.session.total_batches} 批"

    @property
    def sheet_position_text(self) -> str:
        if self.current_sheet_number is None:
            return "—"
        if self.is_reprint_round:
            return f"第 {self.current_sheet_number} 张 / {self.round_text}"
        return f"第 {self.sheet_in_batch} 张 / 本批 {self.batch_sheet_count} 张"

    @property
    def face_text(self) -> str:
        return self.current.face_text if self.current else "—"

    @property
    def page_text(self) -> str:
        return f"第 {self.current.page_number} 页" if self.current else "—"

    # -- 纸张视图 -----------------------------------------------------------

    def _sheets_from(self, faces) -> tuple[DuplexSheet, ...]:
        grouped: dict[tuple[int, int], dict[str, object]] = {}
        for face in faces:
            key = (face.round_index, face.sheet_number)
            data = grouped.setdefault(
                key,
                {
                    "number": face.sheet_number,
                    "batch_index": face.batch_index,
                    "round_index": face.round_index,
                    "front_page": None,
                    "back_page": None,
                    "front_status": FACE_PENDING,
                    "back_status": FACE_PENDING,
                },
            )
            if face.face == FACE_FRONT:
                data["front_page"] = face.page_number
                data["front_status"] = face.status
            else:
                data["back_page"] = face.page_number
                data["back_status"] = face.status
        sheets = [
            DuplexSheet(
                number=int(item["number"]),
                batch_index=int(item["batch_index"]),
                front_page=item["front_page"],  # type: ignore[arg-type]
                back_page=item["back_page"],    # type: ignore[arg-type]
                front_status=str(item["front_status"]),
                back_status=str(item["back_status"]),
                round_index=int(item["round_index"]),
            )
            for item in grouped.values()
        ]
        return tuple(
            sorted(sheets, key=lambda sheet: (sheet.round_index, sheet.number))
        )

    @property
    def sheets(self) -> tuple[DuplexSheet, ...]:
        """把打印面按「轮次 × 物理纸」合并，供界面表格显示（含历史轮次）。"""
        return self._sheets_from(self.faces)

    def sheets_of_batch(self, batch_index: int) -> tuple[DuplexSheet, ...]:
        return tuple(
            sheet
            for sheet in self.sheets
            if sheet.batch_index == batch_index and sheet.round_index == ROUND_NORMAL
        )

    def face_for(self, sheet_number: int, face: str) -> DuplexFace | None:
        """取某张纸某一面的有效记录（轮次最大的那一份）。"""
        items = [
            item
            for item in self.live_faces
            if item.sheet_number == sheet_number and item.face == face
        ]
        return items[0] if items else None

    # -- 文案 ---------------------------------------------------------------

    @property
    def status_text(self) -> str:
        """当前状态（readme 要求界面始终能看清「状态」）。"""
        if self.session.phase == PHASE_CONFIRM and self.current is not None:
            return f"等待用户确认：{self.current.full_label} 是否完整出纸"
        if self.session.phase == PHASE_WAITING_FLIP:
            if self.is_reprint_round:
                return (
                    f"{self.round_text}：第 {self.current_sheet_number} 张正面已确认，"
                    "等待你取出这张纸、翻面并确认"
                )
            return (
                f"第 {self.current_batch} 批正面已全部完成，"
                "等待你整理纸张、翻面并确认"
            )
        if self.session.phase in (PHASE_FRONT, PHASE_BACK):
            if self.current is None:
                return "等待提交"
            return f"{self.current.full_label}：等待提交"
        if self.session.phase == PHASE_PAUSED:
            return "任务已暂停，进度已保存"
        if self.session.phase == PHASE_COMPLETED:
            if self.has_reprint:
                return (
                    f"全部完成：所有纸张正反两面均已确认完整出纸"
                    f"（含 {self.latest_round} 轮补打）"
                )
            return "全部完成：所有纸张正反两面均已确认完整出纸"
        if self.session.phase == PHASE_CANCELLED:
            return "任务已取消（已确认的进度仍保留在记录里）"
        if self.session.phase == PHASE_ERROR:
            return "异常待处理：有整张纸被作废，需要人工核对后继续"
        return self.session.phase_text

    @property
    def next_action_text(self) -> str:
        """下一步该做什么（readme 8.3 要求恢复后显示明确状态）。"""
        if self.session.phase in (PHASE_FRONT, PHASE_BACK) and self.current is not None:
            prefix = f"{self.round_text}：" if self.is_reprint_round else ""
            return (
                f"下一步：{prefix}提交{self.current.label}，打印后检查实际纸张并确认。"
                "未确认之前不会提交下一页。"
            )
        if self.session.phase == PHASE_CONFIRM and self.current is not None:
            return (
                f"下一步：检查打印机与{self.current.full_label}是否完整出纸。"
                "正常点「打印正常」；异常点「打印异常」，整张纸会一起作废并进入补打清单。"
            )
        if self.session.phase == PHASE_WAITING_FLIP:
            if self.is_reprint_round:
                return (
                    f"下一步：取出第 {self.current_sheet_number} 张纸，"
                    "按软件提示的方向整理并放回进纸托盘，然后点「确认已翻面」。"
                    "未确认翻面之前不会打印背面。"
                )
            return (
                "下一步：取出本批纸张，按软件提示的方向整理并放回进纸托盘，"
                "然后点「确认已翻面」。未确认翻面之前不会打印背面。"
            )
        if self.session.phase == PHASE_PAUSED:
            return "下一步：点「恢复任务」；恢复后仍要先处理当前未确认的这一面。"
        if self.session.phase == PHASE_COMPLETED:
            return "全部纸张均已确认完成，可以从任务列表归档或删除该任务。"
        if self.session.phase == PHASE_CANCELLED:
            return "任务已取消；如需重打，请在任务列表重新开始一次手动双面打印。"
        if self.session.phase == PHASE_ERROR:
            return "下一步：检查打印机与纸张，点「打印异常」把整张纸加入补打清单，或重新开始打印。"
        return ""

    def summary_text(self) -> str:
        """一句话摘要（主窗口任务详情用）。"""
        if self.session.phase == PHASE_COMPLETED:
            if self.has_reprint:
                return (
                    f"已完成（{self.completed_sheets}/{self.total_sheets} 张纸，"
                    f"含 {self.latest_round} 轮补打）"
                )
            return f"已完成（{self.confirmed_count}/{self.total_faces} 面，{self.percent}%）"
        if self.session.phase == PHASE_CANCELLED:
            return f"已取消（已确认 {self.confirmed_count}/{self.total_faces} 面）"
        if self.session.phase == PHASE_ERROR:
            return f"异常待处理（{self.void_count} 个打印面作废）"
        parts: list[str] = []
        if self.is_reprint_round:
            parts.append(self.round_text)
        else:
            parts.append(f"第 {self.current_batch}/{self.session.total_batches} 批")
        parts.append(self.phase_text)
        if self.current is not None:
            parts.append(self.current.label)
        parts.append(self.progress_text)
        if self.reprint_pending_count:
            parts.append(f"待补打 {self.reprint_pending_count} 张纸")
        return " · ".join(parts)

    def to_text(self) -> str:
        """多行状态文本（验收脚本与日志共用）。"""
        lines = [
            f"任务：{self.document_name or self.task_uid}",
            f"当前轮次：{self.round_text}",
            f"当前批次：{self.batch_position_text}",
            f"当前纸张：{self.sheet_position_text}",
            f"当前打印面：{self.face_text}",
            f"当前页码：{self.page_text}",
            f"状态：{self.status_text}",
            f"进度：{self.progress_text}",
            f"纸张进度：{self.sheet_progress_text}",
        ]
        if self.reprint_pending_count or self.has_reprint:
            lines.append(f"待补打清单：{self.reprint_pending_text}")
        if self.anomalies:
            lines.append(f"异常记录：{self.anomaly_text}")
        if self.next_action_text:
            lines.append(self.next_action_text)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 从打印计划构造打印面
# ---------------------------------------------------------------------------


def build_faces(task_uid: str, plan) -> list[DuplexFace]:
    """按 page_planner 的 print_steps 展开出每个打印面（顺序即提交顺序）。"""
    faces: list[DuplexFace] = []
    for index, step in enumerate(plan.print_steps):
        faces.append(
            DuplexFace(
                task_uid=task_uid,
                step_index=index,
                batch_index=step.batch_index,
                sheet_number=step.sheet_number,
                face=step.face,
                page_number=step.page_number,
                round_index=ROUND_NORMAL,
            )
        )
    return faces


def build_round_faces(
    task_uid: str,
    faces,
    round_index: int,
    sheet_numbers,
) -> list[DuplexFace]:
    """为指定纸张创建新一轮的打印面（先正面再背面，按纸张号升序）。

    readme 7.4：补打中的纸张再次异常时整张再作废，进入下一轮补打清单。
    """
    latest: dict[tuple[int, str], DuplexFace] = {}
    for face in faces:
        key = (face.sheet_number, face.face)
        current = latest.get(key)
        if current is None or face.round_index > current.round_index:
            latest[key] = face
    next_step = max((face.step_index for face in faces), default=-1) + 1
    result: list[DuplexFace] = []
    for number in sorted(sheet_numbers):
        front = latest.get((number, FACE_FRONT))
        if front is None:
            continue
        result.append(
            DuplexFace(
                task_uid=task_uid,
                step_index=next_step,
                batch_index=front.batch_index,
                sheet_number=number,
                face=FACE_FRONT,
                page_number=front.page_number,
                round_index=round_index,
            )
        )
        next_step += 1
        back = latest.get((number, FACE_BACK))
        if back is not None:
            result.append(
                DuplexFace(
                    task_uid=task_uid,
                    step_index=next_step,
                    batch_index=back.batch_index,
                    sheet_number=number,
                    face=FACE_BACK,
                    page_number=back.page_number,
                    round_index=round_index,
                )
            )
            next_step += 1
    return result


__all__ = [
    "ACTIVE_PHASES",
    "ANOMALY_ACTION_VOID_SHEET",
    "DuplexAnomaly",
    "DuplexFace",
    "DuplexSession",
    "DuplexSheet",
    "DuplexSnapshot",
    "FACE_BACK",
    "FACE_CONFIRMED",
    "FACE_FRONT",
    "FACE_PENDING",
    "FACE_STATUS_LABELS",
    "FACE_SUBMITTED",
    "FACE_VOID",
    "FINISHED_PHASES",
    "PHASE_BACK",
    "PHASE_CANCELLED",
    "PHASE_COMPLETED",
    "PHASE_CONFIRM",
    "PHASE_ERROR",
    "PHASE_FRONT",
    "PHASE_LABELS",
    "PHASE_PAUSED",
    "PHASE_WAITING_FLIP",
    "RESUMABLE_PHASES",
    "ROUND_NORMAL",
    "build_faces",
    "build_round_faces",
    "face_status_label",
    "flip_group_key",
    "phase_label",
    "round_label",
    "round_short_label",
]
