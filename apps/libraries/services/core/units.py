"""Sizes written the way a person reads them.

This lives in the services layer because the answer has to be the same
wherever it is shown: `mhvtl status activity 40` and the library page print
one string, produced once, rather than each formatting bytes its own way.

Rules for this layer:
    - returns plain values; no ServiceResult, because nothing here can fail
    - never imports django.contrib.messages and never sees a request
"""
from typing import Optional

UNITS = ('B', 'KB', 'MB', 'GB', 'TB', 'PB')

#: When a tape stops being roomy and starts being a problem. One rule, because
#: a tape in a drive and the same tape in a slot must not be described
#: differently by the panel and the tile.
FILLING_AT = 75.0
FULL_AT = 90.0


def human_size(size: Optional[int]) -> str:
    """41943040 -> '40.0 MB'. Bytes are whole; everything above has a decimal."""
    if not size:
        return '0 B'
    value = float(size)
    index = 0
    while value >= 1024 and index < len(UNITS) - 1:
        value /= 1024
        index += 1
    return f'{value:.1f} {UNITS[index]}' if index else f'{int(value)} B'


def fullness(percent: Optional[float]) -> str:
    """How alarming a bar should look: normal, filling, full, or unknown.

    A tape at 95% is nearly out of room, and that is worth seeing before a
    backup finds out.
    """
    if percent is None:
        return 'unknown'
    if percent >= FULL_AT:
        return 'full'
    if percent >= FILLING_AT:
        return 'filling'
    return 'normal'
