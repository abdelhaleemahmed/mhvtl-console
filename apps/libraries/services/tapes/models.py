"""Tape dataclasses.

Replaces TapeInfo from tape_operations_service.py:1897. The difference worth
noting is that capacity and used are optional: a tape whose size cannot be read
reports None rather than 0, because 0 reads as "blank tape" and sends an
operator looking for a problem that does not exist.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
from dataclasses import dataclass
from typing import Dict, Optional

from ..core.units import fullness as fullness_of
from ..core.units import human_size
from . import barcodes, palette

MEGABYTE = 1024 * 1024


@dataclass
class TapeInfo:
    """One tape, as the library configuration and the disk describe it."""
    barcode: str
    library_id: Optional[int] = None
    slot: Optional[int] = None
    drive: Optional[int] = None
    used_mb: Optional[int] = None
    capacity_mb: Optional[int] = None
    media_exists: bool = False
    layout: str = ''
    #: Partitions counted from the cartridge's data.N files. 0 means "not
    #: measured", which is why it is not defaulted to 1.
    partitions: int = 0
    #: 'ltfs' | 'plain' | 'unknown'. Empty when nothing asked.
    ltfs_state: str = ''
    #: The MAM carries LTFS traces but the cartridge is no longer partitioned.
    ltfs_was: bool = False

    @property
    def kind(self) -> str:
        """data, clean or WORM."""
        return barcodes.kind(self.barcode)

    @property
    def density(self) -> Optional[str]:
        return barcodes.density_for(self.barcode)

    @property
    def generation_token(self) -> str:
        """Which generation this tape is drawn as - `lto-8`.

        A TOKEN, not a class: the stylesheet is named after it and the terminal
        renders it through its own table. See services/tapes/palette.py.
        """
        return palette.token_for_tape(self.barcode, self.density)

    @property
    def location(self) -> str:
        if self.drive is not None:
            return 'drive'
        return 'slot' if self.slot is not None else 'unknown'

    @property
    def used_percent(self) -> Optional[float]:
        if not self.capacity_mb or self.used_mb is None:
            return None
        return round(self.used_mb / self.capacity_mb * 100, 1)

    @property
    def free_mb(self) -> Optional[int]:
        if not self.capacity_mb or self.used_mb is None:
            return None
        return max(self.capacity_mb - self.used_mb, 0)

    @property
    def summary(self) -> str:
        """What a tape has left, the way a drive is described in a file
        manager: "117 MB free of 480 MB".

        Worded here rather than in a template or a page script, so the
        library page, the tape inventory and `mhvtl tape list` say the same
        thing about the same tape.
        """
        if self.capacity_mb is None or self.used_mb is None:
            return 'size not known'
        return (f'{human_size(self.free_mb * MEGABYTE)} free of '
                f'{human_size(self.capacity_mb * MEGABYTE)}')

    @property
    def ltfs(self) -> bool:
        """Is this an LTFS volume? Both conditions, never one.

        A cartridge needs two partitions AND the LTFS traces in its MAM.
        `mkltfs --wipe` unpartitions a cartridge and leaves every attribute
        behind, so the MAM alone would report a cartridge that cannot be
        mounted - see ltfs_summary.
        """
        return self.ltfs_state == 'ltfs'

    @property
    def ltfs_summary(self) -> str:
        """How the LTFS state is worded, in one place.

        Here rather than in a template or a page script, for the reason the
        `summary` docstring already gives: the library page, the tape inventory
        and `mhvtl ltfs tapes` must say the same thing about the same cartridge.

        Empty when nothing was asked or the cartridge was skipped by a gate.
        "not known" is what an attempted read that could not say deserves; a
        cartridge nobody looked at gets no words at all, so a page cannot imply
        knowledge it does not have.
        """
        if not self.ltfs_state:
            return ''
        if self.ltfs_state == 'unknown':
            return 'not known'
        if self.ltfs_state == 'ltfs':
            return f'LTFS, {self.partitions} partitions'
        if self.ltfs_was:
            return 'not LTFS - formatted once, since unpartitioned'
        return 'not LTFS'

    @property
    def ltfs_mark(self) -> str:
        """'ltfs', 'ltfs-was' or '' - a mark, never a colour.

        Empty for a cartridge that was never read, which `ltfs` cannot say:
        that property is `ltfs_state == 'ltfs'`, so NOT_ASKED and PLAIN both
        come out False. The difference matters to anything drawing a map.
        """
        if not self.ltfs_state:
            return ''
        return palette.mark_for_cartridge(self.ltfs, self.ltfs_was)

    @property
    def partition_summary(self) -> str:
        """The partition count, worded for a human.

        A cartridge that was never measured says so rather than claiming one.
        """
        if not self.partitions:
            return 'not known'
        return '1 partition' if self.partitions == 1 else f'{self.partitions} partitions'

    @property
    def fullness(self) -> str:
        """How alarming the bar should look. The thresholds are in core.units,
        shared with the live drive panel: the same tape must not be described
        one way in a slot and another in a drive."""
        return fullness_of(self.used_percent)

    def to_dict(self) -> Dict:
        return {
            'barcode': self.barcode,
            'library_id': self.library_id,
            'slot': self.slot,
            'drive': self.drive,
            'kind': self.kind,
            'density': self.density,
            'generation_token': self.generation_token,
            'location': self.location,
            'used_mb': self.used_mb,
            'capacity_mb': self.capacity_mb,
            'used_percent': self.used_percent,
            'free_mb': self.free_mb,
            'summary': self.summary,
            'fullness': self.fullness,
            'media_exists': self.media_exists,
            'layout': self.layout,
            'partitions': self.partitions,
            'partition_summary': self.partition_summary,
            'ltfs': self.ltfs,
            'ltfs_state': self.ltfs_state,
            'ltfs_was': self.ltfs_was,
            'ltfs_summary': self.ltfs_summary,
            'ltfs_mark': self.ltfs_mark,
        }
