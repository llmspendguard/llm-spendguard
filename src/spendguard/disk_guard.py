"""Prevent bulk writes when the destination filesystem is below its configured reserve."""
import shutil
from pathlib import Path

from . import config
from .gate import SpendGateRefused


def require_disk_space(target):
    """Refuse before a write if free bytes are below the reserve; never suppress probe errors.

    For a not-yet-created destination, inspect its nearest existing ancestor. This is an
    admission check, not a reservation: callers must repeat it before each bulk write.
    """
    destination = Path(target).expanduser().absolute()
    try:
        floor = config.disk_guard_floor_bytes()
        probe = destination
        while not probe.exists():
            if probe == probe.parent:
                raise OSError(f"no existing ancestor for {destination}")
            probe = probe.parent
        free = shutil.disk_usage(probe).free
    except (OSError, ValueError, TypeError) as exc:
        raise SpendGateRefused(f"Disk guard refused {destination}: cannot establish safe free space: {exc}") from exc
    if free < floor:
        raise SpendGateRefused(
            f"Disk guard refused {destination}: free {free} bytes < floor {floor} bytes "
            f"(shortfall {floor - free} bytes)")
