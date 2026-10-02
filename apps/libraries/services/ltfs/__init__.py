"""LTFS: mounting a cartridge as a filesystem.

Modules:
    mounts.py   which volumes are mounted, read from /proc/mounts. No state
                file - the kernel already holds this, already shares it between
                processes, and is already right after a reboot
    service.py  LtfsService: status, mount, unmount, check, format_cartridge
    tape_moves.py  unloading a cartridge, refused while its filesystem is
                mounted. Here rather than in operations/ because the graph
                points this way - see that module's header

Its own package, like iscsi/, because it wraps an external tool with a lifetime
of its own. Whether LTFS will open a drive is profiles/ltfs.py; what a cartridge
says about itself is tapes/ltfs.py. This package is the part that acts.
"""
from . import mounts
from . import tape_moves
from .service import LtfsService, installed, mount_point_for
from .tape_moves import blocked_drives, unmount_tape

__all__ = ['LtfsService', 'mounts', 'tape_moves', 'installed',
           'mount_point_for', 'blocked_drives', 'unmount_tape']
