"""list, show, set, unset, delete - the configurations you built yourself.

A preset is a profile you built yourself: the same kind of thing as a vendor
profile, one shipped and one yours, and neither of them is ever *created*.
`library create` is the boundary - it is what turns either into something
lsscsi and mtx can see.

So `mhvtl preset` is `mhvtl profile`'s sibling, with one difference that is
the whole point of the pair: a profile has no write verbs, because nothing may
edit a catalogue of what exists in the world. A preset is yours, so it has
set, unset and delete.

`list` enumerates the names; `show` explains one as a concept rather than as
the file it lives in - what it fixes, and what it leaves to its profile. See
docs/sphinx/guides/plan-cli-catalogue.rst, "Two verbs, and what decides
between them".

Moved out of commands/library.py on 4 October 2026. It was `mhvtl library
preset ...` while the catalogue was `mhvtl profile ...`, which put the two
halves of one vocabulary at two different levels.
"""
from .. import output, privileges, runs

#: What `preset set` can set, in the order a file and `show` use. These are
#: the specification's own names; config.presets.KEY_TO_SPEC maps them to the
#: words typed on the command line, and test_presets.py asserts the two are
#: the same set - an option here that the file cannot hold would be accepted
#: and then lost, and a key the file holds with no option here can only be
#: written by hand.
#:
#: Spelled out rather than imported so that building the parser costs no
#: service import: every `mhvtl` invocation builds it, `--help` included.
_SETTABLE = ('profile', 'library_model', 'drive_model', 'drive_revision',
             'media_type', 'num_drives', 'media_count', 'empty_slots')


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        'preset', help='library configurations you built and named')
    verbs = parser.add_subparsers(dest='verb', metavar='<verb>')

    listing = verbs.add_parser('list', help='the preset names')
    listing.set_defaults(handler=do_list)

    show = verbs.add_parser(
        'show', help='what one fixes, and what it leaves to its profile')
    show.add_argument('name')
    show.set_defaults(handler=do_show)

    setting = verbs.add_parser(
        'set', help='add to or change one, creating it if it is new')
    setting.add_argument('name')
    setting.add_argument('--profile', help='the vendor catalogue to build from')
    setting.add_argument('--model', dest='library_model')
    setting.add_argument('--drives', type=int, dest='num_drives')
    setting.add_argument('--media-type', dest='media_type', help='e.g. LTO8')
    setting.add_argument('--tapes', type=int, dest='media_count')
    setting.add_argument('--empty-slots', type=int, dest='empty_slots')
    setting.add_argument('--drive-model', dest='drive_model')
    setting.add_argument('--drive-revision', dest='drive_revision',
                         metavar='REV')
    # Mixed drives and media, in slot order - the same flags `library create`
    # takes, registered from the same place so that a preset reads as the
    # command it replaces. --add-drive adds a kind and --drive replaces the
    # list, because a preset is built up a piece at a time and a mistake has
    # to be correctable without deleting the whole thing.
    runs.add_options(setting, appending=True)
    # No --serial. It was here and did nothing: only the keys in _SETTABLE are
    # read, so `set NAME --serial X` parsed and then said "nothing to set". A
    # serial belongs to one library and a preset is made to be used again, so
    # it stays on `library create`. test_presets.py walks these options.
    setting.set_defaults(handler=do_set)

    # Not `set`'s job: `preset set NEW` addresses a preset by name, so a name
    # it has not seen is a new preset - it would leave the old one where it
    # was and create an empty one beside it. Nothing could rename a preset
    # until this verb existed; it meant editing presets.toml by hand.
    renaming = verbs.add_parser('rename', help='give one another name')
    renaming.add_argument('name', metavar='OLD')
    renaming.add_argument('new_name', metavar='NEW')
    renaming.set_defaults(handler=do_rename)

    unset = verbs.add_parser('unset', help='remove settings from one')
    unset.add_argument('name')
    unset.add_argument('keys', nargs='+', metavar='KEY',
                       help='which settings to remove')
    unset.set_defaults(handler=do_unset)

    delete = verbs.add_parser('delete', help='remove the whole preset')
    delete.add_argument('name')
    delete.set_defaults(handler=do_delete)

    parser.set_defaults(handler=lambda args: _usage(parser))


def _usage(parser) -> int:
    parser.print_help()
    return output.EXIT_USAGE


def _presets():
    from apps.libraries.services.libraries import presets

    return presets


# -- reading ---------------------------------------------------------------

def do_list(args) -> int:
    """The names, and a word about any that cannot be used yet.

    A preset with no profile is legal - building one a piece at a time is the
    point - but `--preset` will refuse it, so the name alone would be a trap.
    """
    presets = _presets()
    result = presets.names()
    if args.json or not result.success:
        return output.result(result, as_json=args.json)

    rows = result.data['presets']
    if not rows:
        print('No presets are defined.')
        output.note(f"file: {result.data['path']}")
        if result.data.get('example'):
            output.note(f"every key, with an example of each: "
                        f"{result.data['example']}")
        print('\nBuild one with:  mhvtl preset set NAME --profile IBM')
        print('or after a create:  mhvtl library create ... --save-preset NAME')
        return output.EXIT_OK

    if args.quiet:
        print(' '.join(row['name'] for row in rows))
        return output.EXIT_OK

    # What each one builds, beside its name. The names alone meant running
    # `preset show` once per preset to find out which was which, and the
    # sentence is the service's - the vendor page's card shows the same words
    # as the folded summary of each saved configuration.
    output.table(
        [{'name': row['name'],
          'builds': row['brief'] if row['complete'] else
                    f"not usable yet - "
                    f"{'it names no profile' if not row['profile'] else '; '.join(row['errors'])}"}
         for row in rows],
        ['name', 'builds'])
    usable = [row['name'] for row in rows if row['complete']]
    if usable:
        print(f'{len(rows)} preset(s). For one of them:  '
              f'mhvtl preset show {usable[0]}')
    return output.EXIT_OK


def do_show(args) -> int:
    """One preset as a concept. The service decides both halves."""
    presets = _presets()
    result = presets.describe(args.name)
    if args.json or not result.success:
        return output.result(result, as_json=args.json)

    # The spelling is the service's: the console prints the same words under
    # each saved configuration, and it could not reach this while it lived
    # here - so the page wrote its own summary from the raw specification and
    # said "vendor default drive(s)" for a preset holding four drives.
    from apps.libraries.services.config.presets import as_words

    described = result.data
    profile = described['profile']

    # "builds from the IBM catalogue" rather than "builds an IBM library":
    # the article would need a rule, and "a IBM" is what it printed first.
    print(f"preset {described['name']} - "
          + (f'builds from the {profile} catalogue' if described['complete']
             else 'not finished'))
    print()
    output.pairs({'valid': described['valid'], 'usable': described['complete']})

    if not described['complete']:
        for problem in (described['errors']
                        or ['it names no profile, so it cannot create '
                            'a library']):
            output.note(f'  {problem}')
        for fix in described['fixes']:
            output.note(f'  {fix}')

    print()
    # One width for both labels, and a space after it: 'left to the profile'
    # is longer than the field, so ljust alone ran the label into its text.
    print(f'{"fixed here":<12} '
          + (as_words(described['fixed']) or 'nothing yet'))
    # The total above the breakdown, for a preset of more than one kind:
    # "2 x TD8 + 2 x TD6, 20 x LTO8 + 10 x LTO6" never said four and thirty.
    # The service counts it - validation uses the same two functions.
    if described['holds']:
        print(f'{"which is":<12} {described["holds"]}')
    left = as_words(described['from_profile'])
    if profile:
        print(f'{f"left to {profile}":<12} '
              + (left or 'nothing - every value is fixed here'))
    else:
        print(f'{"left open":<12} '
              'everything else, until it names a profile')
    for warning in described['warnings']:
        output.note(f'warning: {warning}')

    # What to type, in the same shape `profile show` ends with ("Narrow
    # it:"). The service composes the lines: the console prints these same
    # ones under each saved configuration, and two places that knew how a
    # preset is spelled would be two places to get it wrong.
    if not args.quiet:
        print()
        for line in described['commands']:
            print(f"{line['label'] + ':':<12} {line['command']}")
    return output.EXIT_OK


# -- changing --------------------------------------------------------------

def do_set(args) -> int:
    presets = _presets()
    values = {key: getattr(args, key) for key in _SETTABLE
              if getattr(args, key, None) is not None}

    # The lists, and which of them replace rather than add. Both go to the
    # same save(): scalars replace, a list adds unless it was named with
    # --drive/--media, and the service does the merging - the CLI never reads
    # the file to work out what is already there.
    refused = runs.one_answer_per_question(args)
    if refused is not None:
        return refused
    try:
        lists, replace = runs.collect(args)
    except runs.Refused as problem:
        return output.fail(problem.message, *problem.fixes)
    values.update(lists)

    if not values:
        return output.fail('nothing to set',
                           'name at least one of --profile --model --drives '
                           '--tapes --media-type --empty-slots --drive-model '
                           '--drive-revision --drive --media')

    privileges.require_write_access('saving a preset')
    result = presets.save(args.name, values, replace_lists=replace)
    if args.json or not result.success:
        return output.result(result, as_json=args.json)

    print(result.message)
    if not result.data['complete']:
        output.note('not usable yet: it names no profile, so it cannot '
                    'create a library')
    for warning in result.data.get('warnings', []):
        output.note(f'warning: {warning}')
    return output.EXIT_OK


def do_rename(args) -> int:
    """Move a preset to another name. The service does all of it."""
    presets = _presets()
    privileges.require_write_access('renaming a preset')
    return output.result(presets.rename(args.name, args.new_name),
                         as_json=args.json)


def do_unset(args) -> int:
    """Remove settings. The whole preset is `preset delete`, which is the
    other half of a split made deliberately: one verb doing both jobs had the
    destructive one needing less typing than the careful one."""
    presets = _presets()
    privileges.require_write_access('changing a preset')
    return output.result(presets.forget(args.name, args.keys),
                         as_json=args.json)


def do_delete(args) -> int:
    presets = _presets()
    privileges.require_write_access('deleting a preset')
    return output.result(presets.forget(args.name, []), as_json=args.json)
