"""Tape media: barcodes, densities, and the files under the media directory.

Modules:
    models.py        TapeInfo
    barcodes.py      validation, densities, prefixes, series
    compatibility.py which cartridges a drive can read and write
    media.py         the files on disk, in both the 1.7 and 1.8 layouts
    ltfs_state.py    what mhvtl_data and the MAM say about LTFS - a second file
                     with a second id namespace, so not more of media.py. Named
                     for the question: profiles/ has ltfs_support.py for whether
                     a DRIVE can use LTFS, and services/ltfs/ does the mounting
    service.py       TapeService: list, create, create_bulk, delete, next_barcode

Barcode validation lives in barcodes.py and is not optional: a barcode reaches
`sudo mktape` and `sudo rm -rf`, and the check before this refactor was
`len(barcode) >= 4`.
"""
from . import compatibility, ltfs_state
from .barcodes import InvalidBarcode
from .ltfs_state import LtfsState
from .media import MediaUsage
from .models import TapeInfo
from .service import TapeService

__all__ = ['TapeService', 'TapeInfo', 'MediaUsage', 'LtfsState',
           'InvalidBarcode', 'compatibility', 'ltfs_state']
