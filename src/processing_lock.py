"""Serialize writes on the data mount shared by the Mac and Airflow."""

import errno
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread


_HEARTBEAT_SECONDS = 15
_ABANDONED_SECONDS = 3600


def _remove_claim(marker: Path):
    marker.unlink(missing_ok=True)
    try:
        marker.parent.rmdir()
    except OSError as error:
        if error.errno not in (errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST):
            raise


def _recover_abandoned(path: Path, stale_after: float) -> bool:
    try:
        marker = next(path.iterdir(), None)
        if marker is None:
            path.rmdir()
            return True
        if time.time() - marker.stat().st_mtime > stale_after:
            _remove_claim(marker)
            return True
    except FileNotFoundError:
        return True
    except OSError as error:
        if error.errno not in (errno.ENOTEMPTY, errno.EEXIST):
            raise
    return False


@contextmanager
def exclusive_claim(path: Path, *, wait=False, keepalive=True, stale_after=_ABANDONED_SECONDS):
    """Publish a nonempty claim directory atomically; yield its owner marker."""
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix='.claim-', dir=path.parent))
    marker = path / staged.name
    source = staged / staged.name
    try:
        source.touch()
        while True:
            try:
                os.utime(source, None)
                staged.rename(path)
                break
            except OSError as error:
                if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                    raise
                if _recover_abandoned(path, stale_after):
                    continue
                if not wait:
                    yield None
                    return
                time.sleep(0.05)

        stopped = Event()

        def heartbeat():
            while not stopped.wait(_HEARTBEAT_SECONDS):
                try:
                    os.utime(marker, None)
                except FileNotFoundError:
                    return

        keeper = None
        if keepalive:
            keeper = Thread(target=heartbeat, daemon=True)
            keeper.start()
        try:
            yield marker
        finally:
            stopped.set()
            if keeper is not None:
                keeper.join()
            _remove_claim(marker)
    finally:
        if staged.is_dir():
            shutil.rmtree(staged)


def check_claim(marker: Path):
    if not marker.is_file():
        raise OSError(f'Processing claim expired: {marker.parent.name}')
