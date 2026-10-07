"""``--size-mb 12TB``: one spelling of a size, everywhere one is given.

Three flags and one setting take a cartridge's capacity, and they have to
accept the same words:

    mhvtl settings set tape.size.LTO8 12TB
    mhvtl library create --profile IBM --media-size LTO8:12TB --id 90
    mhvtl tape create 50 K50001L8 --size-mb 12TB

``--size-mb`` was ``type=int`` while the other two went through
``config.settings.parse_size_mb``, so `12TB` was a setting, a media size, and
an argparse error, depending on which flag it was next to. The comment beside
``--media-size`` said all three agreed, which is how it was found.

WHY A WRAPPER AND NOT THE PARSER ITSELF
---------------------------------------
``parse_size_mb`` raises ValueError, which argparse reports as
``invalid size_mb value: '12TiB'`` - swallowing the sentence the service
wrote. ArgumentTypeError is printed as-is, so the refusal an operator reads
from the flag is the one they read from ``settings set``:

    12TiB: sizes here are decimal - MB, GB or TB. A tape is quoted decimal,
    so an LTO-8 holds 12 TB and not 12 TiB

That is the whole of this module. It decides nothing: the service owns which
spellings are legal, and this hands its complaint to argparse unchanged.
"""
import argparse


def size_mb(text: str) -> int:
    """``1000``, ``2000GB``, ``12TB`` -> a size in MB. MB when no unit."""
    from apps.libraries.services.config import settings

    try:
        return settings.parse_size_mb(text)
    except ValueError as problem:
        raise argparse.ArgumentTypeError(str(problem)) from problem


#: The same sentence under every flag that takes one, so the three do not
#: describe the same argument three ways. The default is deliberately not
#: named here: it is a chain, `mhvtl settings list` prints which level
#: answered, and a help string repeating "1000 MB" would be a fourth place
#: holding the number.
HELP = ('1000, 2000GB or 12TB - decimal, as tape capacity is quoted. '
        'The size set for the density if omitted: mhvtl settings list')
