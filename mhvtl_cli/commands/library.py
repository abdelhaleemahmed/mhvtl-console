"""list, show, create, delete, orphans, next-id, slots.

Argument handling and printing only; every verb is one call into
services/libraries. Where a verb changes something it asks privileges first, so
the refusal names the group rather than arriving as a sudo error eight steps in.
"""
from .. import output, privileges, runs, sizes


def register(subparsers) -> None:
    parser = subparsers.add_parser('library', help='libraries and their drives')
    verbs = parser.add_subparsers(dest='verb', metavar='<verb>')

    listing = verbs.add_parser('list', help='every library in device.conf')
    listing.set_defaults(handler=do_list)

    show = verbs.add_parser('show', help='one library in detail')
    show.add_argument('library_id', type=int)
    show.set_defaults(handler=do_show)

    next_id = verbs.add_parser('next-id', help='the next free library id')
    next_id.set_defaults(handler=do_next_id)

    orphans = verbs.add_parser(
        'orphans', help='drives, files and units nothing declares any more')
    orphans.add_argument('--clean', action='store_true',
                         help='remove what is found')
    orphans.set_defaults(handler=do_orphans)

    slots = verbs.add_parser(
        'slots', help='how many empty slots a library has, or change it')
    slots.add_argument('library_id', type=int)
    group = slots.add_mutually_exclusive_group()
    group.add_argument('--empty', type=int,
                       help='set the number of empty slots')
    group.add_argument('--add', type=int,
                       help='add this many empty slots')
    slots.add_argument('--restart', action='store_true',
                       help='restart the library, so its robot sees the change')
    slots.set_defaults(handler=do_slots)

    create = verbs.add_parser('create', help='create a library and its drives')
    # A profile is a catalogue to choose from, a preset is a configuration
    # already chosen. One or the other, never both: a preset names its own
    # profile, so --profile beside it would be either redundant or a
    # contradiction nobody could see.
    source = create.add_mutually_exclusive_group(required=True)
    source.add_argument('--profile',
                        help='vendor catalogue to take the defaults from, '
                             "e.g. IBM, STK, SONY. 'mhvtl profile list' "
                             'names them')
    source.add_argument('--preset',
                        help='a configuration built earlier; '
                             "'mhvtl preset list' names them")
    # In the same group: it asks for the vendor, so --profile and --preset
    # answer the question it exists to ask. Opt in and never inferred, and it
    # refuses to start without a terminal rather than hanging a cron job.
    source.add_argument('--interactive', action='store_true',
                        help='ask for each choice, offering what the vendor '
                             'catalogue holds, then create it, keep it as a '
                             'preset, preview it or quit')
    create.add_argument('--id', type=int, dest='library_id',
                        help='library id; the next free one if omitted')
    create.add_argument('--drives', type=int, dest='num_drives',
                        help='how many drives, all of one model')
    create.add_argument('--media-type', help='e.g. LTO8')
    create.add_argument('--tapes', type=int, dest='media_count',
                        help='how many cartridges to put in slots')
    # A library with more than one kind of drive or tape, in slot order.
    # Registered by runs.py, which `preset set` registers from as well: the
    # same flag, spelled once, with one parser behind it.
    runs.add_options(create)
    create.add_argument('--empty-slots', type=int, dest='empty_slots',
                        help='slots to leave empty, for tapes added later '
                             "(the profile's default if omitted)")
    # `tape create` and `tape bulk` have had this from the beginning, and a
    # library's own cartridges could not be sized at the moment they were
    # made - the last place the two creation paths disagreed. The
    # specification already carried the value: libraries/workflow reads
    # spec['tape_size_mb']. This is the flag that was missing, not new
    # plumbing.
    create.add_argument('--size-mb', type=sizes.size_mb, dest='tape_size_mb',
                        help=f'every cartridge, whatever its density. '
                             f'{sizes.HELP}')
    # One size per kind, because a library holding LTO-8 and DLT-4 holds two
    # capacities and --size-mb would give the second kind the first kind's.
    # Its own flag rather than a third part of --media: the run names a kind
    # of tape and how many, and a capacity is not part of naming one. The
    # setup form posts the same pairs, under the same name.
    create.add_argument('--media-size', action='append', default=[],
                        dest='media_sizes', metavar='DENSITY:SIZE',
                        help='capacity for one kind of cartridge, repeatable: '
                             '--media-size LTO8:12TB --media-size DLT4:20GB. '
                             'A kind not named here takes its setting')
    create.add_argument('--model', dest='library_model',
                        help="library model (product string); the profile's "
                             'default if omitted')
    create.add_argument('--drive-model', dest='drive_model',
                        help="drive model; the model's default if omitted")
    create.add_argument('--serial', help='unit serial number')
    create.add_argument('--drive-revision', metavar='REV',
                        help="drive firmware revision, four characters "
                             "(default: the profile's). LTFS refuses an LTO-5 "
                             "drive below B170, LTO-8 below HB81 and TS1140 "
                             "below 3694; see 'mhvtl ltfs support'")
    create.add_argument('--no-start', action='store_true',
                        help='write the configuration but do not start the daemons')
    create.add_argument('--no-media', action='store_true',
                        help='do not create the tape files library_contents lists')
    create.add_argument('--dry-run', action='store_true',
                        help='print the device.conf text it would write, and stop')
    create.add_argument('--save-preset', metavar='NAME',
                        help='after creating it, keep this configuration '
                             'under a name, for --preset next time. The same '
                             'thing by hand is: mhvtl preset set NAME ...')
    create.set_defaults(handler=do_create)

    delete = verbs.add_parser('delete', help='remove a library and its drives')
    delete.add_argument('library_id', type=int)
    delete.add_argument('--force', action='store_true',
                        help='delete even with a tape loaded in a drive')
    delete.add_argument('--remove-media', action='store_true',
                        help='also delete the tapes, which is not reversible')
    delete.set_defaults(handler=do_delete)

    parser.set_defaults(handler=lambda args: _usage(parser))


def _usage(parser) -> int:
    parser.print_help()
    return output.EXIT_USAGE


def _service(args):
    from apps.libraries.services.libraries import LibraryService
    return LibraryService(args.config_dir)


# -- reading ---------------------------------------------------------------

def do_list(args) -> int:
    service = _service(args)
    result = service.list()
    if args.json or not result.success:
        return output.result(result, as_json=args.json)

    libraries = result.data['libraries']
    if not libraries:
        print('No libraries are configured.')
        return output.EXIT_OK

    output.table(libraries,
                 columns=['library_id', 'model', 'serial', 'drives',
                          'slot_count', 'tape_count'],
                 headers=['id', 'model', 'serial', 'drives', 'slots', 'tapes'])
    # What the table adds up to. The same sentence the console shows above
    # its libraries, from the same call, so the two cannot disagree about
    # what this host holds.
    if not args.quiet:
        summary = service.summary()
        if summary.success:
            print(summary.data['says'])
    return output.EXIT_OK


def do_show(args) -> int:
    result = _service(args).get(args.library_id)
    if args.json or not result.success:
        return output.result(result, as_json=args.json)

    output.pairs(result.data['library'],
                 keys=['library_id', 'vendor', 'product', 'serial', 'channel',
                       'target', 'lun', 'naa', 'home_directory', 'drives',
                       'drive_ids', 'slot_count', 'tape_count'],
                 labels={'library_id': 'id', 'naa': 'NAA',
                         'slot_count': 'slots', 'tape_count': 'tapes'})
    return output.EXIT_OK


def do_next_id(args) -> int:
    result = _service(args).next_id()
    if args.json or not result.success:
        return output.result(result, as_json=args.json)
    print(result.data['library_id'])
    return output.EXIT_OK


def do_orphans(args) -> int:
    from apps.libraries.services.libraries import orphans

    if args.clean:
        privileges.require_write_access('cleaning up orphans')
        return output.result(orphans.cleanup(config_directory=args.config_dir),
                             as_json=args.json, quiet=args.quiet)

    result = orphans.find_result(args.config_dir)
    if args.json or not result.success:
        return output.result(result, as_json=args.json)

    found = result.data
    for label, key, columns in (
            ('Orphaned drives', 'orphaned_drives', ['drive_id', 'reason']),
            ('Orphaned files', 'orphaned_files', ['path', 'reason']),
            ('Orphaned units', 'orphaned_services', ['service', 'reason'])):
        if found[key]:
            print(f'\n{label}:')
            output.table(found[key], columns=columns)

    # Media is listed apart from the rest and never cleaned: these are tapes
    # with their data, and `tape adopt` puts one back into a library.
    if found.get('orphaned_media'):
        print('\nTapes on disk no library lists (data kept):')
        output.table(found['orphaned_media'], columns=['barcode', 'path'])
        print('\nPut one back with: mhvtl tape adopt <library> <barcode>')

    if not any(found.get(key) for key in
               ('orphaned_drives', 'orphaned_files', 'orphaned_services',
                'orphaned_media')):
        print('Nothing is orphaned.')
    return output.EXIT_OK


# -- changing --------------------------------------------------------------

def do_create(args) -> int:
    """The same sequence the web form runs: validate, write, start, verify,
    make the tape files (services/libraries/workflow). It used to stop after
    writing, so a library created here had barcodes and no tapes."""
    from apps.libraries.services.libraries import presets

    if args.interactive:
        refused = _only_interactive(args)
        if refused is not None:
            return refused
        asked = _ask_for_a_library(args)
        if asked is None:                  # quit, or Ctrl-C: nothing written
            return output.EXIT_OK
        spec, args.save_preset = asked
        return _create(args, spec, presets)

    refused = runs.one_answer_per_question(args)
    if refused is not None:
        return refused

    if args.preset:
        resolved = presets.resolve(args.preset)
        if not resolved.success:
            return output.result(resolved, as_json=args.json)
        spec = dict(resolved.data['spec'])
    else:
        spec = {'profile': args.profile}

    # A library with more than one kind of drive or tape. A list replaces a
    # preset's own list, which is what every other argument here does, and it
    # takes out what the list implies - the count, and the single model or
    # density - so that a preset's `drives = 4` cannot be left beside a list
    # of two. Which keys those are is the preset format's rule, asked rather
    # than repeated.
    from apps.libraries.services.config.presets import IMPLIED_BY_LIST

    try:
        lists, _replace = runs.collect(args)
    except runs.Refused as problem:
        return output.fail(problem.message, *problem.fixes)
    for key, value in lists.items():
        spec[key] = value
        for implied in IMPLIED_BY_LIST[key]:
            spec.pop(implied, None)

    # One size per kind of cartridge, through the parser `mhvtl settings set`
    # and the Settings page use. This comment claimed all three agreed while
    # --size-mb above was type=int and rejected "12TB"; they agree now because
    # that flag goes through cli/sizes.py, which is the same parser.
    if getattr(args, 'media_sizes', None):
        from apps.libraries.services.config import settings as sizes

        asked = {}
        for pair in args.media_sizes:
            density, _, typed = str(pair).partition(':')
            if not density.strip() or not typed.strip():
                return output.fail(
                    f'{pair!r} is not a density and a size',
                    'the shape is DENSITY:SIZE, as in LTO8:12TB')
            try:
                asked[density.strip().upper()] = sizes.parse_size_mb(typed)
            except ValueError as problem:
                return output.fail(f'{density.strip()}: {problem}')
        spec['tape_sizes'] = asked

    # An explicit argument beats the preset, which is what this existing loop
    # already does: only values that were given are applied over what is
    # there. Nothing new was needed for the override rule.
    for key, value in (('library_id', args.library_id),
                       ('num_drives', args.num_drives),
                       ('media_type', args.media_type),
                       ('media_count', args.media_count),
                       ('empty_slots', args.empty_slots),
                       ('tape_size_mb', args.tape_size_mb),
                       ('library_model', args.library_model),
                       ('product', args.library_model),
                       ('drive_model', args.drive_model),
                       ('drive_product', args.drive_model),
                       ('drive_revision', args.drive_revision),
                       ('serial', args.serial)):
        if value is not None:
            spec[key] = value

    if args.dry_run:
        if args.save_preset:
            # Saving is for a configuration that has been proved, and a dry
            # run proves nothing. Refused rather than quietly skipped: a
            # --save-preset that printed a preview and saved nothing would
            # look like it had worked.
            return output.fail(
                '--save-preset needs a real creation, not a dry run',
                'a preset is kept because the configuration worked',
                'drop --dry-run, or save it with: '
                f'mhvtl preset set {args.save_preset} ...')
        return _preview(args, spec)

    return _create(args, spec, presets)


def _create(args, spec, presets) -> int:
    """Write it, start it, verify it, make its tapes - then keep the preset.

    Shared by the flag-driven path and the interactive one, so that a library
    created by answering questions goes through exactly the same workflow as
    one created by typing arguments. The interactive loop is a way of filling
    in `spec`, not a second way of creating a library.
    """
    privileges.require_write_access('creating a library')
    from apps.libraries.services.libraries import create_library_workflow

    if 'library_id' not in spec:
        allocated = _service(args).next_id()
        if not allocated.success:
            return output.result(allocated, as_json=args.json, quiet=args.quiet)
        spec['library_id'] = allocated.data['library_id']

    result = create_library_workflow(spec, restart=not args.no_start,
                                     create_media=not args.no_media,
                                     config_directory=args.config_dir)
    if not args.json and not args.quiet:
        for step in (result.data or {}).get('steps', []):
            mark = 'ok  ' if step['ok'] else ('FAIL' if step['fatal'] else 'warn')
            print(f'{mark} {step["step"]:9s} {step["message"]}')

    # Only keep a configuration that worked. Saving one that failed would hand
    # somebody a preset that recreates the failure.
    if args.save_preset and result.success:
        kept = presets.save(args.save_preset, _savable(spec))
        if not args.json and not args.quiet:
            print(kept.message if kept.success
                  else f'preset not saved: {kept.message}')
    return output.result(result, as_json=args.json, quiet=args.quiet)


def _savable(spec) -> dict:
    """What `--save-preset` keeps, decided by the service.

    This used to be a comprehension over a tuple in this file. The setup
    wizard saves a preset too and cannot import the CLI, so the rule moved to
    config.presets.savable and every caller asks it - this one, the wizard,
    and `mhvtl preset set`.
    """
    from apps.libraries.services.config.presets import savable

    return savable(spec)


# -- interactive -----------------------------------------------------------
#
# A form, not a brain. The loop asks questions and calls six things, every one
# of which existed before it:
#
#     the options at each step   profiles.catalogue
#     the default at each step   libraries.spec.apply_defaults
#     the check on the result    libraries.lifecycle.preview
#     keeping it afterwards      libraries.presets.save
#     the library itself         libraries.create_library_workflow
#     the id to offer            LibraryService.next_id
#
# If it ever needs a seventh thing that does not exist yet, that thing belongs
# in a service and not in a prompt. See docs/sphinx/guides/plan-cli-catalogue,
# "the six calls".

#: Flags that answer a question the loop exists to ask. Refused rather than
#: allowed to overrule a typed answer from outside the conversation.
_NOT_WITH_INTERACTIVE = (
    ('--id', 'library_id'), ('--drives', 'num_drives'),
    ('--media-type', 'media_type'), ('--tapes', 'media_count'),
    ('--empty-slots', 'empty_slots'), ('--model', 'library_model'),
    ('--drive-model', 'drive_model'), ('--serial', 'serial'),
    ('--drive-revision', 'drive_revision'), ('--save-preset', 'save_preset'),
)


def _only_interactive(args):
    """None when --interactive may go ahead, or the exit code to return.

    Two refusals. A terminal, because a cron entry that runs this by mistake
    has to fail at once rather than wait for an answer that will never come -
    the discipline colour.enabled() applies to stdout, applied to stdin. And
    nothing else on the command line but --no-start and --no-media, which are
    about what happens after the specification is settled.
    """
    from .. import prompt

    if not prompt.require_terminal():
        return output.fail(
            '--interactive needs a terminal',
            'stdin is not a tty, so there is nobody to answer the questions',
            'in a script, give the values instead: mhvtl library create '
            '--profile IBM --drives 2 --tapes 20')

    given = [flag for flag, dest in _NOT_WITH_INTERACTIVE
             if getattr(args, dest, None) is not None]
    if args.dry_run:
        given.append('--dry-run')
    if args.json:
        given.append('--json')
    if not given:
        return None
    return output.fail(
        f"--interactive cannot be combined with {', '.join(given)}",
        'it asks for each value, so a flag would quietly overrule an answer',
        'the menu at the end offers both the preview and keeping it as a '
        'preset',
        'only --no-start and --no-media go with it')


def _ask_for_a_library(args, asker=None):
    """(spec, preset name or None), or None when the operator quit.

    Nothing is written before the final choice - including by the preview -
    so quitting at any question, or a Ctrl-C, leaves the host untouched.
    """
    from .. import prompt

    ask = asker or prompt.Prompt()
    try:
        return _questions(ask, args)
    except prompt.Interrupted as stopped:
        ask.say(f'Nothing was written. ({stopped})' if str(stopped)
                else 'Nothing was written.')
        return None


def _questions(ask, args):
    """The narrowing order, which is a property of the data rather than a
    decision here: a vendor, then one of its models, then a drive that model
    takes, then a density that drive writes, then the counts its layout
    allows."""
    from apps.libraries.services.profiles import catalogue

    ask.say('Creating a library. An empty answer takes the [default];')
    ask.say('Ctrl-C quits and writes nothing.')
    ask.say()

    # No default vendor. There are nine real catalogues and no function in
    # this project that prefers one, so a default here would be this file
    # choosing a favourite. Every answer after it has one, and each comes
    # from the profile.
    profile = ask.choose('Vendor profile', catalogue.names())
    spec = {'profile': profile}
    described = catalogue.describe(profile)

    ask.say()
    model = ask.choose('Library model',
                       [row['model'] for row in described['models']],
                       _would_be(spec, 'product'))
    spec['library_model'] = model
    for_model = next(row for row in described['models']
                     if row['model'] == model)

    # How many drives and slots this library may really have: the model's
    # element layout, the host's free SCSI targets and its free device ids,
    # whichever is smallest. The same answer the web form shows, from the
    # same service - this loop capped by the layout alone, so on a host with
    # 71 targets free it would offer an IBM 3584's 511 drives and the
    # creation would then be refused.
    room = _room_for(args, spec)

    # Drives, one kind at a time. A library can hold more than one model -
    # two LTO-8 drives and two LTO-6 - and the order asked is the order
    # written, because slot order is SCSI target order.
    ask.say()
    if room.get('bound_by') and room.get('bound_by') != 'the model':
        ask.say(f"  {room['max_drives']} drive(s) at most here, limited by "
                f"{room['bound_by']}.")
    drives = _one_kind_at_a_time(
        ask, spec, 'Drive model', 'How many drives', 'drive type',
        options=lambda: for_model['drives'],
        default_name=lambda: _would_be(spec, 'drive_product'),
        default_count=lambda: _would_be(spec, 'num_drives'),
        maximum=room.get('max_drives', for_model['max_drives']), what='model',
        after=lambda model: _say_read_only(ask, described, model))
    if len(drives) == 1:
        # One kind stays the simple pair, so a preset saved from this reads
        # as the command somebody would have typed.
        spec['drive_model'] = drives[0]['model']
        spec['num_drives'] = drives[0]['count']
    else:
        spec['drive'] = drives

    ask.say()
    media = _one_kind_at_a_time(
        ask, spec, 'Cartridge density', 'How many cartridges', 'density',
        # Only the densities a drive in this library can WRITE: one it merely
        # loads would give a library that restores and never backs up, and
        # `tape bulk` can still add those deliberately afterwards.
        options=lambda: _writable_in(catalogue, drives),
        default_name=lambda: _would_be(spec, 'media_type'),
        default_count=lambda: _would_be(spec, 'media_count'),
        maximum=room.get('max_slots', for_model['max_slots']), what='density')
    if len(media) == 1:
        spec['media_type'] = media[0]['density']
        spec['media_count'] = media[0]['count']
    else:
        spec['media'] = media

    ask.say()
    cartridges = sum(run['count'] for run in media)
    max_slots = room.get('max_slots', for_model['max_slots'])
    slots_left = None if max_slots is None else max_slots - cartridges
    spec['empty_slots'] = ask.ask_int(
        'Empty slots', _would_be(spec, 'empty_slots'),
        minimum=0, maximum=slots_left)

    allocated = _service(args).next_id()
    spec['library_id'] = ask.ask_int(
        'Library id',
        allocated.data['library_id'] if allocated.success else None,
        minimum=1)

    return _then_what(ask, args, spec, catalogue.names())


def _room_for(args, spec) -> dict:
    """How many drives and slots this library may have, here and now.

    The web form's answer, from the service both now ask:
    libraries.setup_form composes the model's element layout with the host's
    free SCSI targets and its free device ids, and names the one that binds.

    An empty answer when the profile cannot be resolved, which cannot happen
    by this point - the vendor was chosen from catalogue.names() - so the
    caller falls back to the model's layout rather than carrying a refusal
    through the questions.
    """
    from apps.libraries.services.libraries import setup_form

    answer = setup_form.state(spec['profile'],
                              library_model=spec.get('library_model'),
                              config_dir=args.config_dir)
    return (answer.data or {}).get('limits') or {}


def _one_kind_at_a_time(ask, spec, name_question, count_question, another, *,
                        options, default_name, default_count, maximum, what,
                        after=None):
    """Ask for a model (or density) and a count, then whether there is another.

    Returns ``[{what: name, 'count': n}, ...]`` in the order asked, which is
    the order they are written - the same shape the file format and
    ``--drive MODEL:COUNT`` produce, so the loop composes a specification
    rather than a special case of one.

    Only the first of each kind is offered a default. A second drive type has
    no sensible default: the profile's answer is already in the first, and
    repeating it would suggest asking twice was pointless.
    """
    runs = []
    remaining = maximum
    while True:
        first = not runs
        choices = [choice for choice in options()
                   if choice not in [run[what] for run in runs]]
        if not choices:
            ask.say(f'  No other {another} is left to add.')
            break
        name = ask.choose(name_question, choices,
                          default_name() if first else None)
        count = ask.ask_int(count_question, default_count() if first else None,
                            minimum=1 if what == 'model' else 0,
                            maximum=remaining)
        runs.append({what: name, 'count': count})
        if after is not None:
            after(name)
        if remaining is not None:
            remaining -= count
            if remaining < 1:
                break
        if not ask.confirm(f'Add another {another}?'):
            break
    return runs


def _writable_in(catalogue, drives) -> list:
    """Every density some drive in the library can write, in slot order.

    The same order and the same rule the services use, so the loop offers
    what creation will accept - a mixed library's LTO-6 is offered because
    its TD6 writes it, and nothing offers LTO-1 because nothing writes that.
    """
    found = []
    for run in drives:
        for density in catalogue.writes(run['model']):
            if density not in found:
                found.append(density)
    return found


def _say_read_only(ask, described, model) -> None:
    """Name what this drive loads and cannot write, having chosen it."""
    for row in described['drives']:
        if row['model'] == model and row['reads_only']:
            ask.say(f"  {model} also loads "
                    f"{', '.join(row['reads_only'])}, which it cannot write.")


def _would_be(spec, key):
    """What apply_defaults would fill in, given the answers so far.

    The same function creation fills a specification with, so a default
    offered here is the value the library would really get rather than a
    second guess at it. The id is a placeholder: nothing read here depends on
    it, and the real one is asked for at the end.
    """
    from apps.libraries.services.libraries import spec as spec_rules

    try:
        return spec_rules.apply_defaults({**spec, 'library_id': 0}).get(key)
    except spec_rules.UnknownProfile:
        return None


def _then_what(ask, args, spec, profiles):
    """The summary, the check, and the four ways out."""
    from apps.libraries.services.libraries import lifecycle

    while True:
        checked = lifecycle.preview(dict(spec), args.config_dir)
        _summarise(ask, spec, checked)
        choice = ask.menu('Now what', (
            ('c', 'create it'),
            ('s', 'create it and keep this configuration as a preset'),
            ('p', 'print the device.conf it would write'),
            ('q', 'quit, writing nothing')))

        if choice == 'q':
            ask.say('Nothing was written.')
            return None
        if choice == 'p':
            text = (checked.data or {}).get('text')
            if text:
                # The configuration is data, so it goes to stdout even here:
                # `--interactive > my.conf` keeps the questions on the
                # terminal and the preview in the file.
                print(text, end='')
            else:
                ask.say(f'There is nothing to preview: {checked.message}')
            continue
        if choice == 's':
            return spec, _a_name_of_its_own(ask, profiles)
        return spec, None


def _a_name_of_its_own(ask, profiles):
    """A name for the preset, which may not be a vendor's.

    The service refuses that as well - it is the rule and it stays there -
    but asking again is better than creating the library and then reporting
    that the preset was not kept.
    """
    reserved = {name.upper() for name in profiles}
    while True:
        name = ask.ask('Keep it as')
        if name.upper() not in reserved:
            return name
        ask.say(f"  '{name}' is a vendor catalogue, not a configuration of "
                f"your own - try {name.lower()}-small.")


def _summarise(ask, spec, checked) -> None:
    """The library as it stands, and what validation makes of it.

    Read from the specification the way the writers read it, so a mixed
    library is summarised as the two kinds it is rather than as its first.
    The loop writes the single pair when there is one kind and the list when
    there are several - a preset saved from the first should read as the
    command somebody would have typed - and the service answers for both
    shapes, so printing does not have to know which arrived.
    """
    from apps.libraries.services.libraries import spec as spec_rules

    drives = spec_rules.asked_drive_runs(spec)
    media = spec_rules.asked_media_runs(spec)
    cartridges = sum(count for _, count in media)
    total = cartridges + spec['empty_slots']

    ask.say()
    ask.say(f"{spec['profile']} {spec['library_model']}, "
            f"id {spec['library_id']}")
    ask.say('  drives      '
            + ', '.join(f'{count} x {model}' for model, count in drives))
    ask.say('  cartridges  '
            + ', '.join(f'{count} x {density}' for density, count in media))
    ask.say(f"  slots       {total} ({cartridges} full, "
            f"{spec['empty_slots']} empty)")

    if not checked.success:
        ask.say(f'  checked: {checked.message}')
        return
    errors = checked.data['errors']
    if errors:
        ask.say(f'  checked: {len(errors)} problem(s)')
        for problem in errors:
            ask.say(f'    error: {problem}')
        for fix in checked.data['fixes']:
            ask.say(f'      {fix}')
    else:
        ask.say('  checked: valid')
    for warning in checked.data['warnings']:
        ask.say(f'    warning: {warning}')

    # Nothing about mixed libraries to apologise for any more: creation writes
    # one block per slot, so what was asked for is what gets built. Only the
    # one thing this loop still does not ask about is named.
    if spec.get('empty_slots'):
        ask.say()
        ask.say(f"{spec['empty_slots']} slot(s) are left empty for cartridges "
                f"added later: mhvtl tape bulk {spec['library_id']} <count>")


def _preview(args, spec) -> int:
    from apps.libraries.services.libraries import lifecycle

    if 'library_id' not in spec:
        allocated = _service(args).next_id()
        if not allocated.success:
            return output.result(allocated, as_json=args.json)
        spec['library_id'] = allocated.data['library_id']
    result = lifecycle.preview(spec, args.config_dir)
    if not result.success:
        return output.result(result, as_json=args.json)

    # An error is what create() would refuse, a warning is what it would
    # accept and grumble about: they are printed apart and only an error is a
    # failure. The preview used to call both "warning" and exit 1 for either,
    # so a perfectly creatable library - LTO3 in a drive that only reads it -
    # looked like a refusal.
    errors, warnings = result.data['errors'], result.data['warnings']
    fixes = result.data.get('fixes', [])
    code = output.EXIT_FAILED if errors else output.EXIT_OK
    if args.json:
        output.result(result, as_json=True)
        return code

    # The device.conf text is the data, on stdout; the problems are
    # diagnostics, on stderr, so a redirected preview stays a config file.
    print(result.data['text'], end='')
    for error in errors:
        output.note(f'error: {error}')
    for warning in warnings:
        output.note(f'warning: {warning}')
    # Only when something was refused: on a clean preview the valid values are
    # not news, and they are long.
    if errors:
        for fix in fixes:
            output.note(f'  {fix}')
    return code


def do_slots(args) -> int:
    """Read the slot counts, or change how many are empty.

    The daemon reads library_contents once at start, so a change only reaches
    the robot after a restart; --restart does it, and without it the command
    says so rather than leaving the operator with a file the library ignores.
    """
    from apps.libraries.services.config.service import ConfigService
    from apps.libraries.services.libraries import lifecycle

    if args.empty is None and args.add is None:
        contents = ConfigService(args.config_dir).library_contents(args.library_id)
        if contents is None:
            return output.fail(f'cannot read library_contents.{args.library_id}',
                               'is the library configured, and readable?')
        summary = contents.summary()
        if args.json:
            output.emit_json({'library_id': args.library_id, **summary})
            return output.EXIT_OK
        output.pairs(summary)
        return output.EXIT_OK

    privileges.require_write_access('changing a library\'s slots')

    wanted = args.empty
    if wanted is None:
        contents = ConfigService(args.config_dir).library_contents(args.library_id)
        if contents is None:
            return output.fail(f'cannot read library_contents.{args.library_id}',
                               'is the library configured, and readable?')
        wanted = contents.summary()['empty_slots'] + args.add

    result = lifecycle.set_empty_slots(args.library_id, wanted, args.config_dir)
    if not result.success or args.json:
        return output.result(result, as_json=args.json, quiet=args.quiet)

    if not args.quiet:
        print(result.message)
    if args.restart:
        from apps.libraries.services.console import units
        restarted = units.restart_library(args.library_id)
        if not restarted.get('ok'):
            return output.fail(
                f'The slots were written but library {args.library_id} was not restarted',
                'restart it with: mhvtl service restart --library '
                f'{args.library_id}')
        if not args.quiet:
            print(f'Restarted vtllibrary@{args.library_id}.service')
    elif not args.quiet:
        print(f'Restart the library for its robot to see this: '
              f'mhvtl service restart --library {args.library_id}')
    return output.EXIT_OK


def do_delete(args) -> int:
    privileges.require_write_access('deleting a library')
    result = _service(args).delete(args.library_id, force=args.force,
                                   remove_media=args.remove_media)
    return output.result(result, as_json=args.json, quiet=args.quiet)
