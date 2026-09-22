"""Tape media: barcodes, densities, and the files under the media directory.

Modules:
    models.py        TapeInfo
    barcodes.py      validation, densities, prefixes, series
    compatibility.py which cartridges a drive can read and write
    media.py         the files on disk, in both the 1.7 and 1.8 layouts
    service.py       TapeService: list, create, create_bulk, delete, next_barcode

Barcode validation lives in barcodes.py and is not optional: a barcode reaches
`sudo mktape` and `sudo rm -rf`, and the check before this refactor was
`len(barcode) >= 4`.
"""
from . import compatibility
from .barcodes import InvalidBarcode
from .media import MediaUsage
from .models import TapeInfo
from .service import TapeService

__all__ = ['TapeService', 'TapeInfo', 'MediaUsage', 'InvalidBarcode',
           'compatibility']
