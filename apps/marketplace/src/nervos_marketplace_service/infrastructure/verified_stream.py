"""Close handles before removing private files, including on Windows/disconnect."""

from collections.abc import Callable, Iterator
from pathlib import Path
from threading import Lock
from typing import Protocol


class BinaryReader(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...
    def close(self) -> None: ...


class StagedArtifact:
    def __init__(
        self, file: BinaryReader, path: Path, size: int, digest: str, release: Callable[[], None]
    ) -> None:
        self.file = file
        self.path = path
        self.size_bytes = size
        self.archive_sha256 = digest
        self.release = release
        self.lock = Lock()
        self.closed = False

    def chunks(self) -> Iterator[bytes]:
        try:
            while True:
                with self.lock:
                    chunk = b"" if self.closed else self.file.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            self.close()

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
            try:
                self.file.close()
                self.path.unlink(missing_ok=True)
            finally:
                self.release()
