"""`--drive MODEL[:COUNT]` and `--media DENSITY[:COUNT]`: more than one kind.

A library may hold two generations of drive, and cartridges for both. The
command line says so by repeating one flag, in slot order:

    mhvtl library create --profile IBM --drive ULT3580-TD8:2 \\
        --drive ULT3580-TD6:2 --media LTO8:20 --media LTO6:10

Slot order is SCSI target order, so the first --drive fills slot 1 and a
backup application that addresses a drive by its position notices when that
position changes. The order given is therefore kept exactly.

WHY THIS IS NOT IN commands/library.py
--------------------------------------
Two commands ask the same question. `library create` builds a library and
`preset set` writes the same choice down under a name, and a preset exists to
read as the command it replaces - so `--drive` has to mean one thing, spelled
one way, with one parser behind it. It was in commands/library.py while only
one command had it; the second caller is what moved it.

A COUNT AND A LIST ARE TWO ANSWERS TO ONE QUESTION
--------------------------------------------------
`--drives 4 --drive ULT3580-TD8:2` could mean four drives or two, and nothing
can choose between them: a library created from the wrong reading is the wrong
size. So the pair is refused here, the preset file refuses it in
config/presets.py, and validation refuses it once more for a preset edited in
two steps that passed neither edge.

Nothing in this module knows what a library is. It parses text into the lists
the services already take, and the services decide what they mean.
"""
from typing import NamedTuple, Tuple

from . import output


class Kind(NamedTuple):
    """One repeatable flag, and everything said about it in one place.

    A NamedTuple rather than six parallel tables: the metavar in the help, the
    word in the refusal and the key in the specification are the same fact
    about `--drive`, and they drifted - the help said MODEL while the refusal
    said NAME, for the same flag.
    """

    flag: str                      #: what an operator types
    metavar: str                   #: the word in the help and the refusal
    dest: str                      #: where argparse puts it
    key: str                       #: the key in a specification
    what: str                      #: the key inside each entry
    singles: Tuple[Tuple[str, str], ...]   #: the single forms it replaces
    example: str                   #: a correct one, for the refusal


#: The single forms are named here rather than looked up, because a refusal
#: has to print the flag an operator typed - `--drives`, not `num_drives`.
KINDS = (
    Kind('--drive', 'MODEL', 'drive_runs', 'drive', 'model',
         (('--drives', 'num_drives'), ('--drive-model', 'drive_model')),
         'ULT3580-TD8:2'),
    Kind('--media', 'DENSITY', 'media_runs', 'media', 'density',
         (('--tapes', 'media_count'), ('--media-type', 'media_type')),
         'LTO8:20'),
)


class Refused(Exception):
    """The arguments cannot be read, and what to tell the operator.

    Carries the message and the fixes rather than printing them: this module
    is given an argparse namespace, and whether the answer is text or JSON is
    the command's to decide.
    """

    def __init__(self, message: str, *fixes: str):
        super().__init__(message)
        self.message = message
        self.fixes = fixes


def add_options(parser, *, appending: bool = False) -> None:
    """Register --drive and --media, and optionally the --add- pair.

    One registration for both commands, so the two cannot drift into
    different metavars or different help for the same flag.

    `appending` is for `preset set`, where a preset is built up a piece at a
    time: --add-drive adds a kind to the list and --drive replaces the whole
    list, which is how a mistake gets corrected without deleting the preset.
    """
    drive, media = KINDS
    parser.add_argument(drive.flag, metavar=f'{drive.metavar}[:COUNT]',
                        action='append', dest=drive.dest,
                        help='a drive model and how many of it, repeatable: '
                             '--drive ULT3580-TD8:2 --drive ULT3580-TD6:2. '
                             'Not with --drives or --drive-model')
    parser.add_argument(media.flag, metavar=f'{media.metavar}[:COUNT]',
                        action='append', dest=media.dest,
                        help='a density and how many cartridges, repeatable: '
                             '--media LTO8:20 --media LTO6:10. Not with '
                             '--tapes or --media-type')
    if not appending:
        return
    parser.add_argument('--add-drive', metavar=f'{drive.metavar}[:COUNT]',
                        action='append', dest=f'add_{drive.dest}',
                        help='add a kind of drive to the list, keeping what '
                             'is there. --drive replaces the list instead')
    parser.add_argument('--add-media', metavar=f'{media.metavar}[:COUNT]',
                        action='append', dest=f'add_{media.dest}',
                        help='add a kind of cartridge to the list, keeping '
                             'what is there. --media replaces the list')


def parse(given, what: str):
    """``['ULT3580-TD8:2', 'ULT3580-TD6']`` -> the list the services take.

    The parsing is ``libraries.spec.parse_runs``, because the setup form's
    rows send the same ``MODEL:COUNT`` back to the server and the web cannot
    import this module - the query string would otherwise be a second
    encoding of one syntax. What stays here is what is genuinely the command
    line's: the flags, the metavars and the wording of the refusals.

    Raises ValueError, which collect() turns into a refusal naming the form.
    """
    from apps.libraries.services.libraries.spec import parse_runs

    return parse_runs(given, what)


def collect(args):
    """(values, replace) - the lists given, and which of them replace.

    values is {'drive': [...], 'media': [...]} for whichever were given, ready
    to go into a specification or into `preset set`. replace names the lists
    that were given as --drive/--media rather than --add-drive/--add-media, so
    presets.save knows which to overwrite and which to add to.

    Raises Refused.
    """
    values, replace = {}, []
    for kind in KINDS:
        replacing = getattr(args, kind.dest, None)
        adding = getattr(args, f'add_{kind.dest}', None)
        if replacing and adding:
            raise Refused(
                f'{kind.flag} and --add-{kind.key} cannot both be given',
                f'{kind.flag} replaces the list, '
                f'--add-{kind.key} adds to it',
                'use one or the other')
        given = replacing or adding
        if not given:
            continue
        try:
            values[kind.key] = parse(given, kind.what)
        except ValueError as problem:
            raise Refused(
                f'{kind.flag}: {problem}',
                f'the form is {kind.flag} {kind.metavar} or '
                f'{kind.flag} {kind.metavar}:COUNT',
                f'for example: {kind.flag} {kind.example}') from None
        if replacing:
            replace.append(kind.key)
    return values, tuple(replace)


def one_answer_per_question(args):
    """None when the arguments are consistent, or the exit code to return.

    A list says which models and how many of each, so the single forms beside
    it are a second answer to the question it has already answered.
    """
    for kind in KINDS:
        if not (getattr(args, kind.dest, None)
                or getattr(args, f'add_{kind.dest}', None)):
            continue
        clashing = [single for single, single_dest in kind.singles
                    if getattr(args, single_dest, None) is not None]
        if clashing:
            return output.fail(
                f"{kind.flag} cannot be combined with {', '.join(clashing)}",
                f'{kind.flag} already says which models and how many of each',
                f'use {kind.flag} alone, repeated once per kind')
    return None
