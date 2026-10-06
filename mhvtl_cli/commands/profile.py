"""list, show - the vendor catalogues.

A profile is what a vendor makes: which library models, which drives each of
those takes, which densities each drive writes. It ships with the console and
**nothing edits it**, which is why this noun has no write verbs at all - the
rule at the top of services/profiles/data.py. A preset is the same kind of
thing built by an operator, and `mhvtl preset` is its sibling.

`list` enumerates, `show` explains one as a concept. See
docs/sphinx/guides/plan-cli-catalogue.rst, "Two verbs, and what decides
between them".

Argument handling and printing only: every answer here is one call into
services/profiles/catalogue, which is also what the web's vendor page
composes its cards from.
"""
from .. import output


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        'profile', help='vendor catalogues: what can be built from each')
    verbs = parser.add_subparsers(dest='verb', metavar='<verb>')

    listing = verbs.add_parser('list', help='the profile names')
    listing.add_argument('--media', metavar='DENSITY',
                         help='only profiles whose drives can WRITE this '
                              'density, e.g. LTO9')
    listing.add_argument('--long', action='store_true',
                         help='all of them side by side, with the model, '
                              'drive and density each would choose')
    listing.set_defaults(handler=do_list)

    show = verbs.add_parser(
        'show', help='one catalogue: its models, drives and densities')
    show.add_argument('profile', metavar='PROFILE', help='e.g. IBM, STK')
    show.add_argument('--library-model', dest='library_model', metavar='MODEL',
                      help='only the drives this library model takes')
    show.add_argument('--media', metavar='DENSITY',
                      help='only the models and drives that WRITE this density')
    show.add_argument('--like', metavar='TEXT',
                      help='only drive models whose name contains TEXT')
    sections = show.add_mutually_exclusive_group()
    sections.add_argument('--models', action='store_true',
                          help='the library models only')
    sections.add_argument('--drives', action='store_true',
                          help='the drive models only')
    sections.add_argument('--densities', action='store_true',
                          help='the densities only')
    show.set_defaults(handler=do_show)

    parser.set_defaults(handler=lambda args: _usage(parser))


def _usage(parser) -> int:
    parser.print_help()
    return output.EXIT_USAGE


def _catalogue():
    from apps.libraries.services.profiles import catalogue

    return catalogue


# -- list ------------------------------------------------------------------

def do_list(args) -> int:
    """The names, because the names are what the next command needs.

    --media answers the one question names cannot: six of the nine profiles
    are LTO families, and STK is T10000, SONY is AIT, QUANTUM is SDLT.
    """
    catalogue = _catalogue()

    if args.long:
        return _long(args, catalogue)

    try:
        names = catalogue.names(media=args.media)
    except catalogue.UnknownProfile as refusal:      # pragma: no cover - n/a
        return output.fail(str(refusal), *refusal.fixes)

    if args.json:
        output.emit_json({'profiles': names, 'media': args.media})
        return output.EXIT_OK

    if not names:
        return output.fail(
            f'no vendor profile has a drive that writes {args.media}',
            'a drive that only reads it is not an answer to "I want a '
            f'{args.media} library"',
            'mhvtl profile list --long   shows what each one writes by default')

    print(' '.join(names))
    if not args.quiet:
        output.note(f'{len(names)} profile(s). '
                    f'For one of them:  mhvtl profile show {names[0]}')
    return output.EXIT_OK


def _long(args, catalogue) -> int:
    """All nine side by side. The default density is the column that matters:
    it is what stops somebody choosing SONY for an LTFS test and getting AIT4
    cartridges LTFS cannot open."""
    rows = catalogue.summaries()
    if args.media:
        wanted = set(catalogue.names(media=args.media))
        rows = [row for row in rows if row['profile'] in wanted]
    if args.json:
        output.emit_json({'profiles': rows})
        return output.EXIT_OK
    if not rows:
        return output.fail(f'no vendor profile writes {args.media}')

    output.table([{'profile': row['profile'],
                   'models': row['model_count'],
                   'drives': row['drive_count'],
                   'default_model': row['default_model'],
                   'default_drive': row['default_drive'],
                   'default_media': row['default_media']} for row in rows],
                 columns=['profile', 'models', 'drives', 'default_model',
                          'default_drive', 'default_media'],
                 headers=['profile', 'models', 'drives', 'default model',
                          'default drive', 'default media'])
    if not args.quiet:
        print('\nThe defaults are what `--profile <name>` alone would choose.')
    return output.EXIT_OK


# -- show ------------------------------------------------------------------

def do_show(args) -> int:
    """One catalogue, in three sections, or one section when asked.

    Narrowing beats refusing: --library-model, --media and --like compose, and
    a library model with no drive left after the filter is dropped rather than
    listed with nothing under it.
    """
    catalogue = _catalogue()
    try:
        described = catalogue.describe(args.profile,
                                       library_model=args.library_model,
                                       media=args.media, like=args.like)
    except catalogue.UnknownProfile as refusal:
        return output.fail(str(refusal), *refusal.fixes)

    if args.json:
        output.emit_json(described)
        return output.EXIT_OK

    wanted = (args.models, args.drives, args.densities)
    everything = not any(wanted)

    if everything:
        print(f"{described['profile']} - "
              f"{len(described['models'])} library model(s), "
              f"{len(described['drives'])} drive model(s)"
              f"{_narrowed(described)}")

    if everything or args.models:
        _models(described, blank=everything)
    if everything or args.drives:
        _drives(described, blank=everything)
    if everything or args.densities:
        _densities(described, blank=everything)

    if everything and not args.quiet and not args.library_model:
        print(f"\nNarrow it:  mhvtl profile show {described['profile']} "
              f"--library-model {described['models'][0]['model']}")
    return output.EXIT_OK


def _narrowed(described) -> str:
    """What was filtered, said in the header rather than left to be guessed."""
    parts = []
    if described['library_model']:
        parts.append(f"for a {described['library_model']}")
    if described['media']:
        parts.append(f"that write {described['media']}")
    if described['like']:
        parts.append(f"named like {described['like']!r}")
    return f" {', '.join(parts)}" if parts else ''


def _models(described, *, blank: bool) -> None:
    if blank:
        print()
    output.table([{'model': row['model'],
                   'drives': row['drive_count'],
                   'max_drives': row['max_drives'],
                   'max_slots': row['max_slots'],
                   'default_drive': row['default_drive'],
                   'default': row['default']} for row in described['models']],
                 columns=['model', 'drives', 'max_drives', 'max_slots',
                          'default_drive', 'default'],
                 headers=['library model', 'drives', 'max drives', 'max slots',
                          'default drive', 'default'])


def _drives(described, *, blank: bool) -> None:
    if blank:
        print()
    output.table([{'model': row['model'],
                   'lto': row['generation'],
                   'writes': row['writes'],
                   'reads_only': row['reads_only'],
                   'default': row['default']} for row in described['drives']],
                 columns=['model', 'lto', 'writes', 'reads_only', 'default'],
                 headers=['drive model', 'lto', 'writes', 'reads only',
                          'default'])


def _densities(described, *, blank: bool) -> None:
    if blank:
        print()
    densities = described['densities']
    if not densities:
        print('No densities.')
        return
    print(f"densities  {', '.join(densities)}")
