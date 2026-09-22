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
from . import barcodes

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

    @property
    def kind(self) -> str:
        """data, clean or WORM."""
        return barcodes.kind(self.barcode)

    @property
    def density(self) -> Optional[str]:
        return barcodes.density_for(self.barcode)

    @property
    def density_class(self) -> str:
        """Which generation colour the tape is drawn in."""
        return barcodes.generation_class(self.density)

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
            'density_class': self.density_class,
            'location': self.location,
            'used_mb': self.used_mb,
            'capacity_mb': self.capacity_mb,
            'used_percent': self.used_percent,
            'free_mb': self.free_mb,
            'summary': self.summary,
            'fullness': self.fullness,
            'media_exists': self.media_exists,
            'layout': self.layout,
        }
