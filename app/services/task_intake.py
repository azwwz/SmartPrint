"""任务接收服务（阶段二的核心）。

职责：把 `Data/incoming` 里的 PDF 变成数据库中的打印任务。

完整流程对应 readme「三、6 watchdog：负责监控新文件」：

1. 发现新 PDF（watchdog 事件，或启动时/定时全目录扫描）。
2. 等待文件写入完成（file_stability）。
3. 尝试打开 PDF 并读取页数、纸张尺寸（pdf_inspector）。
4. 生成 SmartPrint 内部任务 ID。
5. 写入 SQLite（tasks + task_events）。

设计说明：

- 接收工作在独立后台线程完成，避免读取大 PDF 时卡住界面。
- 服务本身不依赖 Qt；界面通过 `drain_notifications()` 轮询获取结果，
  这样后台线程不会直接操作 Qt 控件。
"""

from __future__ import annotations

import hashlib
import logging
import queue
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from app.core.config import AppConfig, IncomingSettings, load_config
from app.core.models import IntakeNotification, Task
from app.core.pdf_inspector import PdfInspection, PdfInspectionError, inspect_pdf
from app.core.task_status import TaskStatus
from app.database.task_repository import TaskRepository
from app.services.file_stability import FileNotStableError, wait_until_stable
from app.services.incoming_watcher import IncomingDirectoryWatcher, WatcherStartError
from app.services.pdf24_filename import parse_pdf24_file_name

logger = logging.getLogger("smartprint.services.task_intake")

#: PDF 打开失败时的重试次数（PDF24 偶尔会在文件尾部还没写完时就触发事件）
INSPECT_ATTEMPTS = 3
INSPECT_RETRY_DELAY_SECONDS = 1.0

_POLL_TICK = object()  # 工作线程的定时唤醒标记


@dataclass
class IntakeStats:
    """接收统计，界面状态栏会显示。"""

    discovered: int = 0      # 入队过的 PDF 数量
    registered: int = 0      # 成功登记为任务的数量
    duplicates: int = 0      # 已存在而跳过的数量
    failed: int = 0          # 接收失败的数量
    scans: int = 0           # 全目录扫描次数


@dataclass
class IntakeState:
    """服务运行状态（供界面显示）。"""

    watcher_running: bool = False
    watcher_message: str = ""
    worker_running: bool = False
    pending: int = 0
    stats: IntakeStats = field(default_factory=IntakeStats)


class TaskIntakeService:
    """PDF 自动接收服务。"""

    def __init__(
        self,
        config: AppConfig | None = None,
        repository: TaskRepository | None = None,
        incoming_dir: Path | None = None,
    ) -> None:
        self.config = config or load_config()
        self.settings: IncomingSettings = self.config.incoming
        self.repository = repository or TaskRepository()
        self.incoming_dir = Path(incoming_dir) if incoming_dir else Path(self.config.incoming_dir)

        self._queue: queue.Queue[object] = queue.Queue()
        self._pending: set[str] = set()
        self._pending_lock = threading.Lock()
        self._notifications: deque[IntakeNotification] = deque(maxlen=500)
        self._notification_lock = threading.Lock()

        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._watcher: IncomingDirectoryWatcher | None = None

        self._stats = IntakeStats()
        self._watcher_message = ""

    # -- 生命周期 -----------------------------------------------------------

    def start(self) -> None:
        """启动后台接收线程、扫描一次目录并开始监控。"""
        if self._worker is not None and self._worker.is_alive():
            logger.debug("接收服务已在运行，忽略重复启动")
            return

        self._stop_event.clear()
        self._worker = threading.Thread(
            target=self._worker_loop, name="smartprint-intake", daemon=True
        )
        self._worker.start()
        logger.info("任务接收服务已启动，监控目录：%s", self.incoming_dir)

        # 程序启动时先扫描一遍，避免 SmartPrint 关闭期间生成的 PDF 漏掉
        self.scan_now(reason="启动扫描")

        self._watcher = IncomingDirectoryWatcher(self.incoming_dir, self._on_file_event)
        try:
            self._watcher.start()
            self._watcher_message = f"正在监控 {self.incoming_dir}"
            # 监控启动完成后再扫一次，补上「启动扫描」与「监控生效」之间出现的文件
            self.scan_now(reason="监控启动后补扫")
        except WatcherStartError as exc:
            self._watcher_message = f"目录监控启动失败（{exc}），仅依赖定时扫描"
            logger.error("目录监控启动失败：%s", exc)
            self._notify("watcher", self._watcher_message)

    def stop(self) -> None:
        """停止监控与后台线程。"""
        if self._worker is None:
            return

        logger.info("正在停止任务接收服务…")
        self._stop_event.set()

        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None

        self._queue.put(None)  # 唤醒工作线程退出
        self._worker.join(timeout=10.0)
        if self._worker.is_alive():  # pragma: no cover
            logger.warning("接收线程未能在超时时间内退出")
        self._worker = None
        logger.info("任务接收服务已停止（%s）", self._stats_text())

    # -- 对外接口 -----------------------------------------------------------

    def scan_now(self, reason: str = "手动扫描") -> int:
        """扫描整个 incoming 目录，把尚未登记的 PDF 放入处理队列。"""
        self.incoming_dir.mkdir(parents=True, exist_ok=True)
        try:
            candidates = sorted(
                (p for p in self.incoming_dir.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"),
                key=lambda p: p.stat().st_mtime,
            )
        except OSError as exc:
            logger.error("扫描 %s 失败：%s", self.incoming_dir, exc)
            self._notify("failed", f"扫描待处理目录失败：{exc}")
            return 0

        self._stats.scans += 1
        queued = 0
        for path in candidates:
            if self._enqueue(path):
                queued += 1

        logger.info(
            "%s：目录内 %s 个 PDF，新入队 %s 个（%s）",
            reason,
            len(candidates),
            queued,
            self._stats_text(),
        )
        self._notify(
            "info", f"{reason}完成：发现 {len(candidates)} 个 PDF，新入队 {queued} 个"
        )
        return queued

    def drain_notifications(self) -> list[IntakeNotification]:
        """取出并清空累积的通知（界面定时调用）。"""
        with self._notification_lock:
            items = list(self._notifications)
            self._notifications.clear()
        return items

    def state(self) -> IntakeState:
        """当前运行状态快照。"""
        with self._pending_lock:
            pending = len(self._pending)
        return IntakeState(
            watcher_running=bool(self._watcher and self._watcher.is_running),
            watcher_message=self._watcher_message,
            worker_running=bool(self._worker and self._worker.is_alive()),
            pending=pending,
            stats=IntakeStats(**vars(self._stats)),
        )

    # -- 内部：目录事件与队列 -----------------------------------------------

    def _on_file_event(self, path: Path) -> None:
        """watchdog 回调（运行在 watchdog 线程）。"""
        self._enqueue(path)

    def _enqueue(self, path: Path) -> bool:
        """把文件放入处理队列；已登记或已在队列中则跳过。"""
        key = str(path.resolve()).lower()
        with self._pending_lock:
            if key in self._pending:
                return False
            self._pending.add(key)

        try:
            if self.repository.exists_source_path(path):
                with self._pending_lock:
                    self._pending.discard(key)
                return False
        except Exception:  # 数据库暂时不可用时仍尝试处理，由后续去重兜底
            logger.exception("检查任务是否已存在时出错：%s", path)

        self._stats.discovered += 1
        logger.info("发现待接收 PDF：%s", path.name)
        self._queue.put(path)
        return True

    # -- 内部：工作线程 -----------------------------------------------------

    def _worker_loop(self) -> None:
        rescan_interval = self.settings.rescan_interval_seconds
        next_rescan = time.monotonic() + rescan_interval if rescan_interval > 0 else None

        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                item = _POLL_TICK

            if item is None:
                break

            if item is _POLL_TICK:
                if next_rescan is not None and time.monotonic() >= next_rescan:
                    try:
                        self.scan_now(reason="定时扫描")
                    except Exception:
                        logger.exception("定时扫描出现异常")
                    next_rescan = time.monotonic() + rescan_interval
                continue

            path = Path(item)  # type: ignore[arg-type]
            try:
                self._process(path)
            except Exception:
                logger.exception("处理文件 %s 时出现未预期的异常", path)
            finally:
                with self._pending_lock:
                    self._pending.discard(str(path.resolve()).lower())

    def _process(self, path: Path) -> None:
        """单个文件的接收流程：稳定性检查 → PDF 校验 → 写库。"""
        if self._stop_event.is_set():
            return
        if path.suffix.lower() != ".pdf" or not path.exists():
            return

        parsed = parse_pdf24_file_name(path.name)

        # 1) 去重：路径已登记，或 PDF24 任务 ID 已登记
        if self.repository.exists_source_path(path):
            logger.debug("已登记过，跳过：%s", path.name)
            return
        if self.repository.exists_source_job_id(parsed.job_id):
            self._stats.duplicates += 1
            message = f"跳过重复文件：{path.name}（PDF24 任务 {parsed.job_id} 已存在）"
            logger.warning(message)
            self._notify("duplicate", message)
            return

        # 2) 等待写入完成
        try:
            stable = wait_until_stable(
                path,
                stability_seconds=self.settings.stability_seconds,
                poll_interval_seconds=self.settings.poll_interval_seconds,
                max_wait_seconds=self.settings.max_wait_seconds,
                min_size_bytes=self.settings.min_size_bytes,
                stop_event=self._stop_event,
            )
        except FileNotStableError as exc:
            self._record_failure(path, f"文件未就绪：{exc}")
            return

        # 3) PDF 有效性检查（带重试）
        inspection = self._inspect_with_retry(path)
        if inspection is None:
            return

        # 4) 生成任务并入库
        task = self._build_task(path, parsed, inspection, stable.size)
        try:
            self.repository.add_task(task)
        except Exception as exc:
            if self.repository.exists_source_path(path):
                logger.info("任务已被其他流程登记，跳过：%s", path.name)
                return
            self._record_failure(path, f"写入数据库失败：{exc}")
            return

        self.repository.add_event(
            task.task_uid,
            "received",
            f"PDF 已接收：{task.file_name}，{task.page_count} 页，{task.size_text}，"
            f"{task.file_size} 字节，文件写入等待 {stable.waited_seconds:.1f} 秒",
        )
        if task.note:
            self.repository.add_event(task.task_uid, "warning", task.note)

        self._stats.registered += 1
        message = (
            f"新任务 {task.task_uid}：{task.document_name}，"
            f"{task.page_count} 页，{task.size_text}"
        )
        logger.info(message)
        self._notify("registered", message, task)

    def _inspect_with_retry(self, path: Path) -> PdfInspection | None:
        """读取 PDF 信息，失败时短暂重试。"""
        last_error = ""
        for attempt in range(1, INSPECT_ATTEMPTS + 1):
            try:
                inspection = inspect_pdf(path)
            except PdfInspectionError as exc:
                last_error = str(exc)
                logger.warning(
                    "PDF 检查失败（第 %s/%s 次）：%s - %s",
                    attempt,
                    INSPECT_ATTEMPTS,
                    path.name,
                    last_error,
                )
                if attempt < INSPECT_ATTEMPTS and not self._stop_event.wait(
                    INSPECT_RETRY_DELAY_SECONDS
                ):
                    continue
                break
            else:
                if inspection.warnings:
                    logger.warning("%s：%s", path.name, "；".join(inspection.warnings))
                return inspection

        self._record_failure(path, f"PDF 无效：{last_error}")
        return None

    def _build_task(
        self,
        path: Path,
        parsed,
        inspection: PdfInspection,
        file_size: int,
    ) -> Task:
        """组装 Task 对象。"""
        now = datetime.now()
        return Task(
            task_uid=self._generate_task_uid(now),
            file_name=path.name,
            document_name=parsed.document_name,
            source_path=path,
            source_job_id=parsed.job_id,
            file_size=file_size,
            file_hash=self._sha256(path),
            page_count=inspection.page_count,
            paper_size=inspection.paper_size,
            orientation=inspection.orientation,
            width_pt=inspection.width_pt,
            height_pt=inspection.height_pt,
            status=TaskStatus.PENDING,
            note="；".join(inspection.warnings) or None,
            created_at=now,
            updated_at=now,
        )

    def _generate_task_uid(self, moment: datetime) -> str:
        """生成 SmartPrint 内部任务号，例如 SP-20261105-195741-A1B2。"""
        for _ in range(10):
            candidate = (
                f"SP-{moment:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4].upper()}"
            )
            if not self.repository.exists_task_uid(candidate):
                return candidate
        raise RuntimeError("无法生成唯一的任务号")

    def _sha256(self, path: Path) -> str | None:
        """计算文件哈希，用于后续校验；失败时返回 None。"""
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            logger.warning("计算文件哈希失败：%s（%s）", path.name, exc)
            return None
        return digest.hexdigest()

    # -- 内部：状态与通知 ---------------------------------------------------

    def _record_failure(self, path: Path, message: str) -> None:
        """记录接收失败（文件保留在原处，等待人工处理）。"""
        self._stats.failed += 1
        logger.error("接收失败：%s - %s", path.name, message)
        self._notify("failed", f"接收失败：{path.name} - {message}")

    def _notify(self, kind: str, message: str, task: Task | None = None) -> None:
        with self._notification_lock:
            self._notifications.append(
                IntakeNotification(kind=kind, message=message, task=task)
            )

    def _stats_text(self) -> str:
        stats = self._stats
        return (
            f"发现 {stats.discovered}，已登记 {stats.registered}，"
            f"重复 {stats.duplicates}，失败 {stats.failed}"
        )
