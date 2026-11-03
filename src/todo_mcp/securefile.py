"""Private file storage for the token cache and other state in the data volume.

Every file is written atomically (temp file in the same directory, fsync, rename) with
mode 0600 inside a 0700 directory, so a crash mid-write can never leave a half-written
refresh token behind and nothing else on the host can read it.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from typing import Any


def ensure_private_dir(path: str) -> None:
    os.makedirs(path, mode=0o700, exist_ok=True)
    st = os.stat(path)
    if st.st_uid == os.getuid() and stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(path, 0o700)
    # Tighten anything that was created with looser permissions (e.g. by hand).
    for name in os.listdir(path):
        full = os.path.join(path, name)
        try:
            fst = os.lstat(full)
        except FileNotFoundError:
            continue
        if stat.S_ISREG(fst.st_mode) and fst.st_uid == os.getuid() and stat.S_IMODE(fst.st_mode) & 0o077:
            os.chmod(full, 0o600)


def write_private(path: str, data: bytes) -> None:
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    dfd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def write_private_json(path: str, obj: Any) -> None:
    write_private(path, json.dumps(obj, indent=2, sort_keys=True).encode("utf-8"))


def read_json(path: str) -> Any | None:
    try:
        with open(path, "rb") as fh:
            return json.loads(fh.read().decode("utf-8"))
    except FileNotFoundError:
        return None


def remove(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
