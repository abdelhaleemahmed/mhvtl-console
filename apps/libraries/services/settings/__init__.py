"""The console's own preferences, as an operation rather than a file.

Modules:
    service.py    SettingsService: list, get, set, reset

A leaf, the shape about/ and dashboard/ use: one module, in its own package
because it is its own role. It imports config (which owns the file), core (for
the result shape) and profiles (to say what a cartridge really holds beside
what one is made at).

WHY IT IS NOT PART OF ConfigService
-----------------------------------
`mhvtl config` is about *MHVTL's* files - device.conf, library_contents,
backups, sync. A preference of the console's is not one of those, and putting
it there would repeat the confusion that splitting `profile` from `preset`
fixed: two different things wearing one noun.

The file itself, its format and the chain that resolves a value are in
config/settings.py, because `tapes` has to read a tape size and `tapes` may
import `config` and not this package. This layer adds what a front end needs
and a reader does not: a ServiceResult, a sentence for a person, and the
provenance of every value.
"""
from .service import SettingsService

__all__ = ['SettingsService']
