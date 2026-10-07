"""list, get, set, reset - the console's own preferences.

Today that is how big a new cartridge is made. An LTO-8 holds 12 TB and is
created at 1 GB, because the media files are sparse so the size costs no disk
but it costs time: filling a native LTO-8 takes about 63 hours, which puts end
of tape and multi-volume spanning - the things a virtual library exists to
exercise - out of reach. Anyone who wants a realistic cartridge sets one here.

A NOUN, THEN A VERB, THEN A KEY

    mhvtl settings set tape.size.LTO8 12TB

and not `mhvtl settings tape-size set LTO8`, which is a noun, a noun and then
a verb. `mhvtl library preset ...` had that shape until 4 October 2026 and was
promoted to `mhvtl preset` for the same reason: it put the two halves of one
vocabulary at two different levels.

The key is the path through the file, so what is typed is what is found in
/etc/mhvtl-gui/settings.toml - the rule presets already follows for its own
keys. A setting added later is a new key under a new section, not a new verb
and not a new sub-noun, so these four verbs are all there will ever be.

This paragraph used to illustrate that with an invented key for a default
drive model - the one command in this file that could not run, because `set`
refuses a key nothing reads and nothing read that one. A documented command
that fails is what SuggestedCommandTests exists to stop, and it missed this
because it checks the noun and the verb rather than the argument.

There is no example of a future setting here now. An example of something
that does not exist is indistinguishable from a bug, and
DocumentedKeysExistTests now fails on one - including on a mention inside an
explanation like this, which is why this paragraph describes the key in words
rather than writing it out.

NARROWING
---------
`list` takes a section, a family or one key, and case never matters:

    mhvtl settings list                 all of them
    mhvtl settings list tape.size       the section
    mhvtl settings list tape.size.lto   every LTO generation
    mhvtl settings list tape.size.3592  J1A, E05, E06, E07
    mhvtl settings list tape.size.LTO8  one

Thirty-three rows is not a list anybody reads. A family is not always a
prefix - the 3592 cartridges share none with each other - so the service asks
what family a density belongs to rather than matching the spelling.

`mhvtl config` is deliberately not where this lives. That noun is MHVTL's
files - device.conf, library_contents, backups - and a preference of ours is
not one of them.
"""
from .. import output, privileges


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        'settings', help="the console's own preferences")
    verbs = parser.add_subparsers(dest='verb', metavar='<verb>')

    listing = verbs.add_parser(
        'list', help='every setting, and where each value came from')
    listing.add_argument('section', nargs='?',
                         help='narrow it: a section (tape.size), a family '
                              '(tape.size.lto, tape.size.3592) or one key '
                              '(tape.size.LTO8). Case does not matter')
    listing.set_defaults(handler=do_list)

    get = verbs.add_parser('get', help='one setting')
    get.add_argument('key', help='e.g. tape.size.LTO8')
    get.set_defaults(handler=do_get)

    setting = verbs.add_parser('set', help='change one setting')
    setting.add_argument('key', help='e.g. tape.size.LTO8')
    setting.add_argument('value',
                         help='a size: 1000, 2000GB or 12TB. Decimal, as tape '
                              'capacity is quoted; MB is stored')
    setting.set_defaults(handler=do_set)

    reset = verbs.add_parser(
        'reset', help='remove one setting, so it takes the default again')
    reset.add_argument('key', help='e.g. tape.size.LTO8')
    reset.set_defaults(handler=do_reset)

    parser.set_defaults(handler=lambda args: _usage(parser))


def _usage(parser) -> int:
    parser.print_help()
    return output.EXIT_USAGE


def _service(args):
    from apps.libraries.services.settings import SettingsService
    return SettingsService()


# -- reading ---------------------------------------------------------------

def do_list(args) -> int:
    result = _service(args).list(getattr(args, 'section', None))
    if args.json or not result.success:
        return output.result(result, as_json=args.json)

    rows = [{'key': row['key'],
             'value': row['shown'],
             'source': row['source'],
             'native': row['native_shown'] or '-'}
            for row in result.data['settings']]
    output.table(rows, columns=['key', 'value', 'source', 'native'],
                 headers=['setting', 'value', 'from', 'the cartridge holds'])

    print(f'\n{result.message}')
    if not result.data['exists']:
        # Saying the file is absent matters more than saying where it is: a
        # reader who has just seen a table of values will otherwise go looking
        # for the file those values came out of, and there is not one.
        print(f'No settings file yet; it is written when you set something: '
              f'{result.data["file"]}')
    return output.EXIT_OK


def do_get(args) -> int:
    result = _service(args).get(args.key)
    if args.json or not result.success:
        return output.result(result, as_json=args.json)

    row = result.data
    pairs = {'value': row['shown'], 'from': row['source']}
    if row['native_shown']:
        pairs['the cartridge holds'] = row['native_shown']
    output.pairs(pairs)
    return output.EXIT_OK


# -- changing --------------------------------------------------------------

def do_set(args) -> int:
    privileges.require_write_access('changing a setting')
    result = _service(args).set(args.key, args.value)
    code = output.result(result, as_json=args.json, quiet=args.quiet)
    if result.success and not args.json and not args.quiet:
        print(_note(result.data))
    return code


def do_reset(args) -> int:
    privileges.require_write_access('changing a setting')
    result = _service(args).reset(args.key)
    code = output.result(result, as_json=args.json, quiet=args.quiet)
    if result.success and not args.json and not args.quiet:
        print(_note(result.data))
    return code


def _note(row) -> str:
    """What a changed setting does and does not do.

    It applies to cartridges made from now on. Saying so here is cheaper than
    the support question from somebody who changed it and looked at a tape
    they made last week.
    """
    return ('Tapes created from now on use this; the ones you already have '
            'keep the size they were made with.')
