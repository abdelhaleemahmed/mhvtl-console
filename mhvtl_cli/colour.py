"""The only file in the CLI that contains an escape sequence.

A generation token - `lto-8` - is decided once, in
apps/libraries/services/tapes/palette.py, and rendered twice: the browser does
it through a stylesheet, and this module does it through the table below. The
service never learns what either one looks like.

When colour is used, and when it is not
---------------------------------------
Off unless all four hold:

    stdout is a terminal          a pipe, a log and `| jq` get plain text
    --no-colour was not passed    an operator's explicit no
    NO_COLOR is unset             the convention (https://no-color.org)
    the terminal offers 256       see below

256 is not a preference. The palette separates LTO-6 from LTO-8 and LTO-5 from
LTO-9 by lightness within one hue, because the real cartridge shell colours give
those pairs the same colour. Sixteen colours cannot hold that separation, so
where TERM offers only sixteen the colour is dropped ENTIRELY rather than
collapsed into ties - two generations sharing one colour is worse than no colour
at all, and the generation is printed as text either way.

That last point is the rule the whole palette rests on: COLOUR IS A HINT, NEVER
THE IDENTIFIER. Nothing here may be the only way to read a value.
"""
import os
import sys

#: Drawn where a colour is wanted but the terminal cannot have one.
BAR = '▌'          # LEFT HALF BLOCK - the slot and drive marker
BLOCK = '█' * 6    # FULL BLOCK - the legend swatch

_RESET = '\033[0m'
_DIM = '\033[2m'
_BOLD = '\033[1m'


#: What to write ON a generation's colour. A fill and its ink are one
#: decision, so they are chosen together: the five light fills take dark ink
#: and every other fill takes white. This is the terminal's half of what the
#: stylesheet does with `color:` beside `background:`.
INK = {'lto-2': 232, 'lto-7': 232, 'lto-9': 232, 'lto-10': 232}
DEFAULT_INK = 231


def block(text: str, ansi: int = None, token: str = '', *, on: bool = True) -> str:
    """`text` written on the generation's colour - the slot tile.

    The barcode stays legible at every generation because the ink is chosen
    against the fill, which is the whole reason the map draws a block rather
    than colouring the text: LTO-6 as foreground on a black terminal is too
    dark to read.
    """
    if not on or ansi is None:
        return text
    ink = INK.get(token, DEFAULT_INK)
    return f'\033[48;5;{int(ansi)}m\033[38;5;{ink}m{text}{_RESET}'


#: LTFS is a MARK, never a fill - it is something a cartridge is as well as
#: its generation, so it never takes a colour of its own. The web draws an
#: inset outline; a terminal has no box-shadow, so it gets a bar down each
#: side of the tile instead. Same idea, same accent colour the page uses
#: (--accent-strong), and the muted one for a cartridge that was a volume and
#: is no longer.
LTFS_ACCENT = 45
LTFS_MUTED = 244
LEFT_BAR = '\u258c'
RIGHT_BAR = '\u2590'


def ltfs_outline(mark: str, *, on: bool = True):
    """The pair that goes either side of a tile, as (left, right).

    Both are one column wide whatever the mark is, so a grid stays a grid.

    Without colour the bars would be two identical blocks and an LTFS volume
    would look exactly like one that merely was - the distinction the whole
    LTFS state model exists to keep. So the plain fallback is the two
    characters that can still be told apart: '*' for a volume, '~' for one
    that was.
    """
    if not mark:
        return ' ', ' '
    if not on:
        return ' ', '*' if mark == 'ltfs' else '~'
    ansi = LTFS_ACCENT if mark == 'ltfs' else LTFS_MUTED
    return (f'\033[38;5;{ansi}m{LEFT_BAR}{_RESET}',
            f'\033[38;5;{ansi}m{RIGHT_BAR}{_RESET}')


def enabled(no_colour: bool = False, stream=None) -> bool:
    """Whether to colour at all. Every caller asks this, and nothing else."""
    stream = stream or sys.stdout
    if no_colour or os.environ.get('NO_COLOR') is not None:
        return False
    if not hasattr(stream, 'isatty') or not stream.isatty():
        return False
    return _depth() >= 256


def paint(text: str, ansi: int = None, *, on: bool = True, dim: bool = False,
          bold: bool = False) -> str:
    """`text` in the 256-colour index `ansi`, or unchanged when off.

    `ansi` of None means the palette had no colour for it - a cartridge that is
    not LTO at all - which is drawn dim rather than in a wrong colour.
    """
    if not on:
        return text
    codes = []
    if bold:
        codes.append(_BOLD)
    if dim or ansi is None:
        codes.append(_DIM)
    if ansi is not None:
        codes.append(f'\033[38;5;{int(ansi)}m')
    return f'{"".join(codes)}{text}{_RESET}' if codes else text


def swatch(ansi: int = None, *, on: bool = True, width: int = 6) -> str:
    """A block of colour for a legend, or its token's width in spaces."""
    block = '█' * width
    return paint(block, ansi, on=on) if on else ' ' * width


def _depth() -> int:
    """How many colours TERM claims. No curses: this must work without a tty
    database, and `tput` is a subprocess for a question that does not need one.
    """
    term = os.environ.get('TERM', '')
    if os.environ.get('COLORTERM') in ('truecolor', '24bit'):
        return 16777216
    if '256' in term or term.endswith('-direct'):
        return 256
    return 8 if term and term != 'dumb' else 0
