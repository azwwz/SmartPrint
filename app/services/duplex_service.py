"""手动双面打印编排服务（阶段六 + 阶段七）。

这个模块把阶段四算出的「一张纸两个面」和阶段五的「把一页交给打印机」串成
readme 要求的手动双面流程：

    正常循环（第 0 轮）
        当前批次正面第 1 张 → 提交 → 用户确认完整出纸
        → 正面第 2 张 → 提交 → 用户确认
        ...
        → 本批正面全部确认 → 等用户整理纸张并确认翻面
        → 背面第 1 张 → 提交 → 用户确认
        ...
        → 本批完成 → 下一批正面 …
    补打循环（第 1、2、… 轮）
        正常循环里作废的纸张 → 等正常循环全部结束 → 逐张补打
        正面 → 确认 → 翻面确认 → 背面 → 确认 → 该张补打完成
        补打中再次异常 → 整张再作废 → 进入下一轮补打清单

六条硬规则（与 readme 3.2 / 7.1 / 7.4 / 10.3 / 10.4 / 10.5 一致）：

1. **最多只有一个未确认的打印面。** 一面提交后，在用户确认或报告异常之前，
   ``submit_current()`` 一律拒绝，不会自动提交下一页。
2. **命令发送不等于纸张出纸。** SumatraPDF 返回 0 只是把打印面标记为
   ``submitted``；只有 ``confirm_current()``（用户在界面上点「打印正常」）
   才会变成 ``confirmed``。
3. **永远不自动重试。** 失败、超时、预检未通过都不会重发；程序退出后恢复时，
   未确认的打印面保持 ``submitted``，等用户核对。
4. **异常以整张纸为单位。** 任意一面报告异常，正反两面一起作废。
5. **正常与补打分离。** 作废的纸张立刻退出当前循环，等当前循环全部结束后
   再统一补打（readme 7.2）。
6. **补打同样逐面提交、逐面确认**，补打中再次异常就进入下一轮（readme 7.3 / 7.4）。

所有关键操作都会写 task_events，进度写 duplex_sessions / duplex_faces /
duplex_anomalies，因此关掉程序再打开也能恢复。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from app.core.config import AppConfig, load_config
from app.core.duplex import (
    ANOMALY_ACTION_VOID_SHEET,
    FACE_BACK,
    FACE_CONFIRMED,
    FACE_SUBMITTED,
    FACE_VOID,
    PHASE_BACK,
    PHASE_CANCELLED,
    PHASE_COMPLETED,
    PHASE_CONFIRM,
    PHASE_ERROR,
    PHASE_FRONT,
    PHASE_PAUSED,
    PHASE_WAITING_FLIP,
    ROUND_NORMAL,
    DuplexAnomaly,
    DuplexFace,
    DuplexSession,
    DuplexSnapshot,
    build_faces,
    build_round_faces,
    round_label,
)
from app.core.models import Task
from app.core.page_planner import plan_pages
from app.core.print_job import SUBMIT_BLOCKED
from app.core.task_status import TaskStatus
from app.database.task_repository import TaskRepository
from app.services.print_service import PrintOutcome, PrintService

logger = logging.getLogger("smartprint.services.duplex_service")

#: 记录到 task_events 的事件类型
EVENT_START = "duplex_start"
EVENT_SUBMIT = "duplex_submit"
EVENT_BLOCKED = "duplex_blocked"
EVENT_CONFIRM = "duplex_confirm"
EVENT_FLIP = "duplex_flip"
EVENT_FRONT_DONE = "duplex_front_done"
EVENT_BATCH = "duplex_batch"
EVENT_COMPLETE = "duplex_complete"
EVENT_ANOMALY = "duplex_anomaly"
EVENT_REPRINT_ROUND = "duplex_reprint_round"
EVENT_REPRINT_DONE = "duplex_reprint_done"
EVENT_ANOMALY_RESOLVED = "duplex_anomaly_resolved"
EVENT_PAUSE = "duplex_pause"
EVENT_RESUME = "duplex_resume"
EVENT_CANCEL = "duplex_cancel"


class DuplexServiceError(Exception):
    """当前状态不允许执行这个双面打印操作。"""


#: 阶段 → 任务状态（补打轮次会覆盖成 REPRINT）
_PHASE_TASK_STATUS: dict[str, TaskStatus] = {
    PHASE_FRONT: TaskStatus.PRINTING_FRONT,
    PHASE_CONFIRM: TaskStatus.PRINTING_FRONT,   # 提交正面后按当前面再细分
    PHASE_WAITING_FLIP: TaskStatus.WAITING_FLIP,
    PHASE_BACK: TaskStatus.PRINTING_BACK,
    PHASE_PAUSED: TaskStatus.PAUSED,
    PHASE_COMPLETED: TaskStatus.COMPLETED,
    PHASE_CANCELLED: TaskStatus.CANCELLED,
    PHASE_ERROR: TaskStatus.ERROR,
}


@dataclass
class DuplexSubmitResult:
    """一次「提交当前打印面」的结果。"""

    snapshot: DuplexSnapshot | None
    outcome: PrintOutcome | None = None
    submitted: bool = False       # 是否已把命令交给打印程序（等待用户确认）
    dry_run: bool = False
    error: str = ""
    messages: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.submitted and self.outcome is not None and self.outcome.ok)

    @property
    def status_text(self) -> str:
        if self.dry_run:
            return "演练：未发送"
        if self.error:
            return self.error
        if self.outcome is not None:
            return self.outcome.status_text
        return ""

    def summary_text(self) -> str:
        snapshot = self.snapshot
        page = snapshot.current.page_number if snapshot and snapshot.current else "—"
        parts = [f"第 {page} 页", self.status_text]
        if self.outcome is not None and self.outcome.sumatra is not None:
            parts.append(f"退出码 {self.outcome.sumatra.returncode}")
        return "，".join(parts)


class DuplexPrintService:
    """逐面提交、逐面确认的手动双面打印编排。"""

    def __init__(
        self,
        repository: TaskRepository,
        print_service: PrintService,
        config: AppConfig | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.print_service = print_service
        self.config = config or load_config()
        self._clock = clock or datetime.now

    # -- 读取 ---------------------------------------------------------------

    def snapshot(self, task_uid: str, task: Task | None = None) -> DuplexSnapshot | None:
        """读取某个任务的双面打印状态；没有会话时返回 None。"""
        session = self.repository.get_duplex_session(task_uid)
        if session is None:
            return None
        faces = self.repository.list_duplex_faces(task_uid)
        anomalies = self.repository.list_duplex_anomalies(task_uid)
        return DuplexSnapshot(
            session=session,
            faces=tuple(faces),
            document_name=task.document_name if task else "",
            task_page_count=task.page_count if task else 0,
            anomalies=tuple(anomalies),
        )

    def require_snapshot(self, task: Task) -> DuplexSnapshot:
        snapshot = self.snapshot(task.task_uid, task)
        if snapshot is None:
            raise DuplexServiceError("这个任务还没有开始手动双面打印")
        return snapshot

    def has_session(self, task_uid: str) -> bool:
        return self.repository.get_duplex_session(task_uid) is not None

    def anomalies(self, task_uid: str) -> list[DuplexAnomaly]:
        """列出异常记录（readme 7.6 的完整留痕）。"""
        return self.repository.list_duplex_anomalies(task_uid)

    # -- 开始 ---------------------------------------------------------------

    def start(
        self,
        task: Task,
        *,
        sheets_per_batch: int | None = None,
        restart: bool = False,
    ) -> DuplexSnapshot:
        """开始（或继续）手动双面打印。

        - 已有未结束的会话：直接返回当前进度，不会重置，也不会自动重打任何页面。
        - 已有已完成 / 已取消的会话且 ``restart=False``：返回旧记录供查看。
        - ``restart=True``：明确要求重新开始，删除旧会话记录并重新生成打印面。
        """
        existing = self.repository.get_duplex_session(task.task_uid)
        if existing is not None and not restart:
            return self.snapshot(task.task_uid, task)  # type: ignore[return-value]

        sheets = int(sheets_per_batch or self.config.sheets_per_batch or 5)
        try:
            plan = plan_pages(
                int(task.page_count),
                sheets,
                self.config.front_page_order,
                self.config.back_page_order,
            )
        except ValueError as exc:
            raise DuplexServiceError(f"无法计算打印计划：{exc}") from exc

        faces = build_faces(task.task_uid, plan)
        now = self._clock()
        first = faces[0] if faces else None
        session = DuplexSession(
            task_uid=task.task_uid,
            phase=PHASE_FRONT,
            sheets_per_batch=sheets,
            total_sheets=plan.sheet_count,
            total_batches=plan.batch_count,
            flip_confirmed_batch=0,
            flip_confirmed_group="",
            current_round=ROUND_NORMAL,
            current_batch=1,
            current_sheet=first.sheet_number if first else None,
            current_face=first.face if first else None,
            current_page=first.page_number if first else None,
            created_at=now,
            updated_at=now,
        )
        self.repository.replace_duplex_session(session, faces)
        self.repository.add_event(
            task.task_uid,
            EVENT_START,
            f"开始手动双面打印：{plan.summary_text}，共 {len(faces)} 个打印面；"
            "每次只提交一个页面，并且必须由用户确认完整出纸后才继续",
        )
        self._set_task_status(
            task.task_uid, TaskStatus.PRINTING_FRONT, "开始手动双面打印"
        )
        logger.info(
            "任务 %s 开始手动双面打印：%s，%s 张纸，%s 批",
            task.task_uid,
            plan.summary_text,
            plan.sheet_count,
            plan.batch_count,
        )
        return self.snapshot(task.task_uid, task)  # type: ignore[return-value]

    # -- 提交 ---------------------------------------------------------------

    def submit_current(
        self,
        task: Task,
        *,
        dry_run: bool = False,
        allow_blocked_printer: bool = False,
    ) -> DuplexSubmitResult:
        """提交当前打印面。提交后必须等用户确认，不会自动继续下一页。"""
        snapshot = self.require_snapshot(task)
        if not snapshot.can_submit:
            raise DuplexServiceError(
                f"当前状态为「{snapshot.status_text}」，不能提交下一页"
            )
        face = snapshot.current
        if face is None:  # 防御：理论上 can_submit 已保证
            raise DuplexServiceError("没有找到待打印的页面")

        outcome = self.print_service.print_page(
            task,
            face.page_number,
            dry_run=dry_run,
            allow_blocked_printer=allow_blocked_printer,
        )
        if dry_run:
            return DuplexSubmitResult(
                snapshot=self.snapshot(task.task_uid, task),
                outcome=outcome,
                submitted=False,
                dry_run=True,
                messages=["演练：只生成命令并预检，没有发送任何打印数据"],
            )

        blocked = outcome.job is None or outcome.status == SUBMIT_BLOCKED
        if blocked:
            error = outcome.error or "预检未通过，没有发送"
            self.repository.add_event(
                task.task_uid,
                EVENT_BLOCKED,
                f"{face.full_label} 未提交：{error}（不会自动重试）",
            )
            return DuplexSubmitResult(
                snapshot=self.snapshot(task.task_uid, task),
                outcome=outcome,
                submitted=False,
                error=error,
            )

        # 命令已经交给 Windows（无论退出码是多少，纸上都可能已经有内容）。
        now = self._clock()
        job_id = outcome.job.id if outcome.job else None
        self.repository.update_duplex_face(
            face.id,
            status=FACE_SUBMITTED,
            job_id=job_id,
            submitted_at=now,
            note=outcome.status_text,
        )
        detail = (
            f"已提交{face.full_label}（{face.page_file_name}）→ "
            f"{outcome.job.printer_name if outcome.job else '打印机不明'}："
            f"{outcome.status_text}"
        )
        if outcome.job is not None and outcome.job.exit_code is not None:
            detail += f"（退出码 {outcome.job.exit_code}）"
        if outcome.job is not None and outcome.job.job_id:
            detail += f"，队列作业 #{outcome.job.job_id}"
        detail += "；等待用户确认实际纸张"
        self.repository.add_event(task.task_uid, EVENT_SUBMIT, detail)
        logger.info("任务 %s：%s", task.task_uid, detail)

        snapshot = self._advance(task, old_phase=snapshot.phase, reason="命令已发送，等待用户确认")
        messages = list(outcome.warnings)
        if not outcome.ok:
            messages.append(
                "打印程序返回异常：这不代表纸上没有内容，请检查打印机和实际纸张，"
                "再选择「打印正常」或「打印异常」"
            )
        return DuplexSubmitResult(
            snapshot=snapshot,
            outcome=outcome,
            submitted=True,
            messages=messages,
        )

    # -- 确认 ---------------------------------------------------------------

    def confirm_current(self, task: Task, *, note: str = "") -> DuplexSnapshot:
        """用户确认当前打印面完整出纸，然后才推进到下一个打印面。"""
        snapshot = self.require_snapshot(task)
        if not snapshot.can_confirm:
            raise DuplexServiceError(
                f"当前没有等待确认的打印面（状态：{snapshot.status_text}）"
            )
        face = snapshot.current
        if face is None:
            raise DuplexServiceError("没有找到等待确认的打印面")
        now = self._clock()
        self.repository.update_duplex_face(
            face.id, status=FACE_CONFIRMED, confirmed_at=now
        )
        detail = f"用户确认{face.full_label}完整出纸"
        if note:
            detail += f"（{note}）"
        self.repository.add_event(task.task_uid, EVENT_CONFIRM, detail)
        logger.info("任务 %s：%s", task.task_uid, detail)
        if face.round_index > ROUND_NORMAL:
            self._resolve_sheet_if_done(task, face, now)
        return self._advance(task, old_phase=snapshot.phase, reason="用户确认完整出纸")

    def confirm_flip(self, task: Task) -> DuplexSnapshot:
        """用户确认已经整理纸张并完成翻面；确认后才能打印背面。"""
        snapshot = self.require_snapshot(task)
        if not snapshot.can_flip:
            raise DuplexServiceError(
                f"当前状态为「{snapshot.status_text}」，还不能确认翻面"
            )
        current = snapshot.current
        batch = current.batch_index if current else snapshot.current_batch
        round_index = current.round_index if current else ROUND_NORMAL
        group = current.flip_group if current else f"0:{batch}"
        if round_index > ROUND_NORMAL:
            what = f"第 {current.sheet_number} 张纸已取出、整理并翻面"
            fields = {
                "flip_confirmed_group": group,
                "current_round": int(round_index),
                "phase": PHASE_BACK,
                "updated_at": self._clock(),
            }
        else:
            what = f"第 {batch} 批已整理纸张并翻面"
            fields = {
                "flip_confirmed_batch": int(batch),
                "flip_confirmed_group": group,
                "current_batch": int(batch),
                "phase": PHASE_BACK,
                "updated_at": self._clock(),
            }
        self.repository.update_duplex_session(task.task_uid, **fields)
        self.repository.add_event(
            task.task_uid,
            EVENT_FLIP,
            f"用户确认{what}（{round_label(round_index)}）；开始逐页打印背面",
        )
        self._set_task_status(
            task.task_uid,
            TaskStatus.REPRINT if round_index > ROUND_NORMAL else TaskStatus.PRINTING_BACK,
            f"翻面确认（{round_label(round_index)}）",
        )
        logger.info("任务 %s：%s 翻面确认", task.task_uid, round_label(round_index))
        return self.snapshot(task.task_uid, task)  # type: ignore[return-value]

    # -- 异常 / 暂停 / 取消 --------------------------------------------------

    def report_anomaly(self, task: Task, reason: str = "") -> DuplexSnapshot:
        """报告当前打印面异常：整张纸作废并进入待补打清单，当前循环继续。

        readme 7.1 / 7.2 / 7.6：
        - 任意一面异常，整张纸作废，正反两面都进待补打清单；
        - 作废的纸张立刻退出当前循环，等当前循环结束再统一补打；
        - 每次异常都要留下完整记录（哪一轮、哪张纸、正反面页码、异常面、原因）。
        """
        snapshot = self.require_snapshot(task)
        if not snapshot.can_confirm:
            raise DuplexServiceError(
                f"当前没有待核对的打印面（状态：{snapshot.status_text}）"
            )
        face = snapshot.current
        if face is None:
            raise DuplexServiceError("没有找到待核对的打印面")

        sheet = next(
            (
                item
                for item in snapshot.sheets
                if item.number == face.sheet_number
                and item.round_index == face.round_index
            ),
            None,
        )
        front_page = sheet.front_page if sheet else None
        back_page = sheet.back_page if sheet else None
        detail_reason = reason.strip() or "用户报告打印异常"
        now = self._clock()

        self.repository.void_duplex_sheet(
            task.task_uid,
            face.sheet_number,
            detail_reason,
            round_index=face.round_index,
        )
        anomaly = DuplexAnomaly(
            task_uid=task.task_uid,
            round_index=face.round_index,
            sheet_number=face.sheet_number,
            face=face.face,
            front_page=front_page,
            back_page=back_page,
            reason=detail_reason,
            action=ANOMALY_ACTION_VOID_SHEET,
            created_at=now,
        )
        self.repository.add_duplex_anomaly(anomaly)

        front_text = front_page if front_page is not None else "无"
        back_text = back_page if back_page is not None else "无"
        self.repository.add_event(
            task.task_uid,
            EVENT_ANOMALY,
            f"整张纸作废：{round_label(face.round_index)} 第 {face.sheet_number} 张"
            f"（正面 {front_text}，背面 {back_text}），异常面：{face.face_text}，"
            f"原因：{detail_reason}；正反两面一起进入待补打清单，"
            "当前循环继续处理其它纸张",
        )
        logger.warning(
            "任务 %s %s 第 %s 张整张作废：%s",
            task.task_uid,
            round_label(face.round_index),
            face.sheet_number,
            detail_reason,
        )
        return self._advance(
            task,
            old_phase=snapshot.phase,
            reason=f"第 {face.sheet_number} 张作废（{round_label(face.round_index)}）",
        )

    def pause(self, task: Task) -> DuplexSnapshot:
        snapshot = self.require_snapshot(task)
        if not snapshot.can_pause:
            raise DuplexServiceError(f"当前状态不能暂停：{snapshot.status_text}")
        self.repository.update_duplex_session(
            task.task_uid, phase=PHASE_PAUSED, updated_at=self._clock()
        )
        self.repository.add_event(
            task.task_uid,
            EVENT_PAUSE,
            f"用户暂停任务（当前：{snapshot.status_text}）；未确认的打印面保持待核对",
        )
        self._set_task_status(task.task_uid, TaskStatus.PAUSED, "用户暂停")
        logger.info("任务 %s 已暂停", task.task_uid)
        return self.snapshot(task.task_uid, task)  # type: ignore[return-value]

    def resume(self, task: Task) -> DuplexSnapshot:
        snapshot = self.require_snapshot(task)
        if not snapshot.can_resume:
            raise DuplexServiceError(f"任务当前不是暂停状态：{snapshot.status_text}")
        self.repository.add_event(task.task_uid, EVENT_RESUME, "用户恢复任务")
        logger.info("任务 %s 已恢复", task.task_uid)
        return self._advance(task, old_phase=PHASE_PAUSED, reason="用户恢复任务")

    def cancel(self, task: Task) -> DuplexSnapshot:
        snapshot = self.require_snapshot(task)
        if not snapshot.can_cancel:
            raise DuplexServiceError(f"当前状态不能取消：{snapshot.status_text}")
        now = self._clock()
        self.repository.update_duplex_session(
            task.task_uid,
            phase=PHASE_CANCELLED,
            note="用户取消任务",
            finished_at=now,
            updated_at=now,
        )
        pending = snapshot.reprint_pending_count
        tail = f"，{pending} 张纸仍在待补打清单" if pending else ""
        self.repository.add_event(
            task.task_uid,
            EVENT_CANCEL,
            f"用户取消任务（{snapshot.sheet_progress_text}"
            f"，已确认 {snapshot.confirmed_count}/{snapshot.total_faces} 面）{tail}",
        )
        self._set_task_status(task.task_uid, TaskStatus.CANCELLED, "用户取消")
        logger.info("任务 %s 已取消", task.task_uid)
        return self.snapshot(task.task_uid, task)  # type: ignore[return-value]

    # -- 内部 ---------------------------------------------------------------

    def _advance(self, task: Task, *, old_phase: str, reason: str) -> DuplexSnapshot:
        """按打印面的真实状态重新推导阶段，并同步任务状态与事件。"""
        session = self.repository.get_duplex_session(task.task_uid)
        if session is None:
            raise DuplexServiceError("双面打印会话不存在")
        faces = self.repository.list_duplex_faces(task.task_uid)
        now = self._clock()
        current = next((face for face in faces if face.is_open), None)

        if current is None:
            current, faces, session = self._open_reprint_round(
                task, faces, session, now
            )

        if current is None:
            # 没有任何待打印的面：所有纸张（含补打轮次）都已确认
            latest = max((face.round_index for face in faces), default=ROUND_NORMAL)
            leftover = [
                face
                for face in faces
                if face.round_index == latest and face.is_void
            ]
            if leftover:
                phase = PHASE_ERROR
                note = "存在无法自动排入补打的作废纸张"
            else:
                phase = PHASE_COMPLETED
                note = ""
            self.repository.update_duplex_session(
                task.task_uid,
                phase=phase,
                current_sheet=None,
                current_face=None,
                current_page=None,
                finished_at=now,
                updated_at=now,
                note=note,
            )
            if phase == PHASE_COMPLETED:
                extra = (
                    f"（含 {latest} 轮补打）" if latest > ROUND_NORMAL else ""
                )
                self.repository.add_event(
                    task.task_uid,
                    EVENT_COMPLETE,
                    f"全部完成：{len(faces)} 个打印面均已由用户确认完整出纸{extra}",
                )
                self._set_task_status(
                    task.task_uid, TaskStatus.COMPLETED, "所有纸张正反两面均已确认"
                )
                logger.info("任务 %s 手动双面打印完成", task.task_uid)
            else:
                self._set_task_status(
                    task.task_uid, TaskStatus.ERROR, "存在无法补打的作废纸张"
                )
            return self.snapshot(task.task_uid, task)  # type: ignore[return-value]

        if current.status == FACE_SUBMITTED:
            phase = PHASE_CONFIRM
        elif current.face == FACE_BACK and not self._flip_confirmed(session, current):
            phase = PHASE_WAITING_FLIP
        elif current.face == FACE_BACK:
            phase = PHASE_BACK
        else:
            phase = PHASE_FRONT

        self.repository.update_duplex_session(
            task.task_uid,
            phase=phase,
            current_round=int(current.round_index),
            current_batch=int(current.batch_index),
            current_sheet=int(current.sheet_number),
            current_face=current.face,
            current_page=int(current.page_number),
            updated_at=now,
        )

        if phase == PHASE_WAITING_FLIP and old_phase not in (PHASE_WAITING_FLIP, PHASE_PAUSED):
            if current.round_index > ROUND_NORMAL:
                message = (
                    f"{round_label(current.round_index)}：第 {current.sheet_number} 张"
                    "正面已确认；请取出这张纸、翻面后放回进纸托盘再确认，"
                    "未确认翻面之前不会打印背面"
                )
            else:
                message = (
                    f"第 {current.batch_index} 批正面全部完成；请整理纸张、翻面后确认，"
                    "未确认翻面之前不会打印背面"
                )
            self.repository.add_event(task.task_uid, EVENT_FRONT_DONE, message)
        if (
            phase == PHASE_FRONT
            and current.round_index == ROUND_NORMAL
            and current.batch_index > 1
            and old_phase not in (PHASE_FRONT, PHASE_PAUSED)
        ):
            self.repository.add_event(
                task.task_uid,
                EVENT_BATCH,
                f"第 {current.batch_index - 1} 批已完成，进入第 {current.batch_index} 批；"
                f"下一面：{current.label}",
            )

        self._set_task_status(
            task.task_uid, self._status_for_phase(phase, current), reason
        )
        return self.snapshot(task.task_uid, task)  # type: ignore[return-value]

    def _open_reprint_round(
        self,
        task: Task,
        faces: list[DuplexFace],
        session: DuplexSession,
        now: datetime,
    ):
        """当前轮次已经没有待打印的面时，把作废纸张排进下一轮补打。

        readme 7.2 / 7.4：正常循环里作废的纸张不参与当前循环，等这一轮全部结束
        后统一补打；补打中再次异常的纸张进入再下一轮。
        """
        latest = max((face.round_index for face in faces), default=ROUND_NORMAL)
        voided = sorted(
            {
                face.sheet_number
                for face in faces
                if face.round_index == latest and face.is_void
            }
        )
        if not voided:
            return None, faces, session

        if latest > ROUND_NORMAL:
            self.repository.add_event(
                task.task_uid,
                EVENT_REPRINT_DONE,
                f"{round_label(latest)}结束：其中 {len(voided)} 张纸再次异常，"
                "进入下一轮补打清单",
            )

        new_round = latest + 1
        new_faces = build_round_faces(task.task_uid, faces, new_round, voided)
        if not new_faces:
            logger.error("任务 %s 无法为第 %s 轮补打创建打印面", task.task_uid, new_round)
            return None, faces, session

        self.repository.add_duplex_faces(new_faces)
        self.repository.update_duplex_session(
            task.task_uid,
            current_round=int(new_round),
            phase=PHASE_FRONT,
            flip_confirmed_group="",
            updated_at=now,
        )
        sheets_text = "、".join(f"第 {number} 张" for number in voided)
        self.repository.add_event(
            task.task_uid,
            EVENT_REPRINT_ROUND,
            f"开始{round_label(new_round)}：{len(voided)} 张纸（{sheets_text}）；"
            "补打同样逐面提交、逐面确认，整张纸两面都确认才算完成",
        )
        logger.info(
            "任务 %s 开始%s：%s 张纸", task.task_uid, round_label(new_round), len(voided)
        )
        session = self.repository.get_duplex_session(task.task_uid) or session
        faces = self.repository.list_duplex_faces(task.task_uid)
        current = next((face for face in faces if face.is_open), None)
        return current, faces, session

    @staticmethod
    def _flip_confirmed(session: DuplexSession, face: DuplexFace) -> bool:
        """这一面的翻面是否已经确认（兼容阶段六只记录批次号的旧数据）。"""
        if session.flip_confirmed_group:
            return session.flip_confirmed_group == face.flip_group
        if face.round_index > ROUND_NORMAL:
            return False
        return session.flip_confirmed_batch == face.batch_index

    def _resolve_sheet_if_done(
        self, task: Task, face: DuplexFace, now: datetime
    ) -> None:
        """补打轮里一张纸两面都确认后，回填异常记录的补打结果。"""
        faces = self.repository.list_duplex_faces(
            task.task_uid, round_index=face.round_index
        )
        sheet_faces = [item for item in faces if item.sheet_number == face.sheet_number]
        if not sheet_faces or not all(item.is_confirmed for item in sheet_faces):
            return
        resolved = self.repository.resolve_duplex_anomalies(
            task.task_uid,
            face.sheet_number,
            face.round_index,
            result=f"{round_label(face.round_index)}完成",
        )
        if resolved:
            self.repository.add_event(
                task.task_uid,
                EVENT_ANOMALY_RESOLVED,
                f"第 {face.sheet_number} 张纸{round_label(face.round_index)}完成："
                "正反两面均已确认完整出纸",
            )
            logger.info(
                "任务 %s 第 %s 张纸补打完成（%s）",
                task.task_uid,
                face.sheet_number,
                round_label(face.round_index),
            )

    @staticmethod
    def _status_for_phase(phase: str, current: DuplexFace | None) -> TaskStatus:
        """补打轮次里的任务状态统一显示「待补打」。"""
        if current is not None and current.round_index > ROUND_NORMAL:
            if phase == PHASE_COMPLETED:
                return TaskStatus.COMPLETED
            if phase == PHASE_CANCELLED:
                return TaskStatus.CANCELLED
            if phase == PHASE_PAUSED:
                return TaskStatus.PAUSED
            if phase == PHASE_ERROR:
                return TaskStatus.ERROR
            return TaskStatus.REPRINT
        if phase == PHASE_CONFIRM and current is not None:
            return (
                TaskStatus.PRINTING_FRONT
                if current.face != FACE_BACK
                else TaskStatus.PRINTING_BACK
            )
        return _PHASE_TASK_STATUS.get(phase, TaskStatus.PRINTING_FRONT)

    def _set_task_status(self, task_uid: str, status: TaskStatus, reason: str) -> None:
        task = self.repository.get_task(task_uid)
        if task is None or task.status == status:
            return
        self.repository.update_status(task_uid, status)
        self.repository.add_event(
            task_uid,
            "status",
            f"状态由「{task.status.label}」变更为「{status.label}」（{reason}）",
        )


__all__ = [
    "DuplexPrintService",
    "DuplexServiceError",
    "DuplexSubmitResult",
    "EVENT_ANOMALY",
    "EVENT_ANOMALY_RESOLVED",
    "EVENT_BATCH",
    "EVENT_BLOCKED",
    "EVENT_CANCEL",
    "EVENT_COMPLETE",
    "EVENT_CONFIRM",
    "EVENT_FLIP",
    "EVENT_FRONT_DONE",
    "EVENT_PAUSE",
    "EVENT_REPRINT_DONE",
    "EVENT_REPRINT_ROUND",
    "EVENT_RESUME",
    "EVENT_START",
    "EVENT_SUBMIT",
]
