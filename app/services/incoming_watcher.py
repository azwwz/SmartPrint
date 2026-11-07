"""监控 Data/incoming 目录。

只负责「发现文件」这一件事：把新出现或刚被写入的 PDF 路径交给回调函数，
文件是否写入完成、是否是有效 PDF，都交给上层（task_intake）判断。

使用 watchdog 的目录事件；即使事件丢失，上层还有定时全目录扫描兜底。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger("smartprint.services.incoming_watcher")


class WatcherStartError(Exception):
    """目录监控启动失败。"""


class _PdfEventHandler(FileSystemEventHandler):
    """把目录事件过滤成 PDF 路径回调。"""

    def __init__(self, callback: Callable[[Path], None]) -> None:
        super().__init__()
        self._callback = callback

    def _handle(self, raw_path: str | bytes) -> None:
        path = Path(raw_path if isinstance(raw_path, str) else raw_path.decode(errors="ignore"))
        if path.suffix.lower() != ".pdf":
            return
        self._callback(path)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle(getattr(event, "dest_path", event.src_path))


class IncomingDirectoryWatcher:
    """Data/incoming 目录监控器。"""

    def __init__(self, directory: Path, on_pdf_found: Callable[[Path], None]) -> None:
        self.directory = Path(directory)
        self._callback = on_pdf_found
        self._observer: Observer | None = None

    @property
    def is_running(self) -> bool:
        return self._observer is not None and self._observer.is_alive()

    def start(self) -> None:
        """启动监控，失败时抛出 WatcherStartError。"""
        self.directory.mkdir(parents=True, exist_ok=True)
        observer = Observer()
        observer.schedule(_PdfEventHandler(self._callback), str(self.directory), recursive=False)
        try:
            observer.start()
        except Exception as exc:  # OSError / 权限不足等
            try:
                observer.stop()
            except Exception:  # pragma: no cover
                pass
            raise WatcherStartError(f"无法监控目录 {self.directory}：{exc}") from exc

        self._observer = observer
        logger.info("开始监控目录：%s", self.directory)

    def stop(self) -> None:
        """停止监控。"""
        observer, self._observer = self._observer, None
        if observer is None:
            return
        try:
            observer.stop()
            observer.join(timeout=5.0)
        except Exception:  # pragma: no cover - 退出阶段不抛异常
            logger.exception("停止目录监控时出现异常")
        else:
            logger.info("已停止监控目录：%s", self.directory)
