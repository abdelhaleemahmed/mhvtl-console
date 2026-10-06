"""The catalogue, composed once for every caller.

A profile is what a vendor makes: which library models, which drives each of
those models accepts, which densities each drive loads and which of those it
can write. ``data.py`` holds those tables and answers one question at a time.
This module composes them into the answers a *caller* wants - the vendors, or
one vendor explained - so that no caller composes them for itself.

WHAT THIS REPLACES
------------------
Three callers had each written that composition out, for the same tables::

    DriveService.models_for_profile   the `drive models` verb that
                                      `profile show` replaced
    views._brand_catalogue            the web's vendor page
    brand_config.html's script        the setup form's dropdowns

The first two walked ``library_models``, collected the drives of each,
collected the media of those drives and picked the defaults. The third did it
again in JavaScript, and had drifted: it filtered on what a drive *loads*
where the services filter on what it *writes*, and it chose the last writer
listed where the services choose by generation. ``form_lookups`` ends that by
handing the page the answers instead of the tables.

A fourth copy was about to be written for ``library create --interactive``,
which is what made this worth doing rather than worth noting.

WRITES IS NOT LOADS
-------------------
``creatable_media()`` deliberately includes the generation a drive can only
read, because a restore library is a real thing to build. "I want an LTO-9
library" means a drive that *writes* LTO9, so the two are separated here
rather than in each caller - see ``writes()``.

ON ORDER
--------
Lists of models and drives keep the profile's own order, which runs oldest
generation to newest. They are not sorted: ``get_default_drive_for_library``
relies on that order, the forms present it that way, and STK's L700 list ends
with a DLT7000 precisely because the order is the data rather than a
presentation choice. Densities are the exception and are grouped by family -
see ``densities_of``.

Rules for this layer:
    - pure. No I/O, no Django, no subprocess: profiles/ imports nothing
      outside itself (test_service_layers.py has ``'profiles': set()``)
    - returns plain data, never a ServiceResult. The services that have one
      wrap these answers, and the CLI prints them
"""
from typing import Dict, List, Optional

from . import data


class UnknownProfile(ValueError):
    """A vendor nobody makes, carrying the ones that exist.

    ``.fixes`` for the same reason validation has it: the moment somebody
    names a profile wrong is the moment they want the list.
    """

    def __init__(self, message: str, fixes=None):
        super().__init__(message)
        self.fixes = list(fixes or [])


def writes(drive_model: str) -> List[str]:
    """The densities this drive can write, native first.

    What ``--media LTO9`` and the web's tape filter both mean. A drive that
    loads LTO9 and cannot write it is not an answer to "I want an LTO-9
    library", and offering it as one is how somebody ends up with fifty
    cartridges they can restore from and not back up to.
    """
    read_only = set(data.read_only_media(drive_model))
    return [density for density in data.creatable_media(drive_model)
            if density not in read_only]


def default_density_for(drive_models) -> Optional[str]:
    """The density a new cartridge gets in a library with these drives.

    The densities in drive order, each once, and the first of them some drive
    can **write**. A drive lists its native density first, so for a uniform
    library this is simply that drive's own tape; for a mixed one it is the
    first slot's.

    That is the rule ``TapeService.media_for_library`` has always reported as
    a library's default - "media are in the order the drives list them,
    native first, so the first is the natural default". It is here so that
    there is one copy of it and so that **creation** can apply it before the
    library exists: a mixed library is specified before device.conf has a
    word about it, and media_for_library can only answer for a library that
    is already there.
    """
    in_order: List[str] = []
    for drive in drive_models:
        for density in data.creatable_media(drive):
            if density not in in_order:
                in_order.append(density)
    for density in in_order:
        if any(density in writes(drive) for drive in drive_models):
            return density
    return None


def default_drive_for(profile_key: str, library_model: str, *,
                      media: str = None) -> Optional[str]:
    """The drive a library model gets when none is chosen, for a wanted tape.

    Without ``media`` this is ``data.get_default_drive_for_library``: the
    profile's own default when the model takes it, else the newest LTO
    generation it takes. With one, the same rule over the drives that *write*
    that density.

    Which is **not** "the last one listed". IBM lists its half-height drives
    after the full-height ones, so last-listed answers ULT3580-HH9 where the
    rule answers ULT3580-TD9 - same generation, and not the drive anybody
    means. The setup form's script picked last-listed until this function
    existed, so the terminal and the browser disagreed about the default for
    every filtered choice.
    """
    key = resolve(profile_key)
    takes = data.get_valid_drives_for_library(key, library_model)
    if media:
        wanted = media.strip().upper()
        takes = [drive for drive in takes
                 if wanted in [density.upper() for density in writes(drive)]]
    if not takes:
        return None

    profile = data.get_profile(key)
    if profile.drive_product_default in takes:
        return profile.drive_product_default
    newest = max(takes, key=data.lto_generation)
    return newest if data.lto_generation(newest) else takes[-1]


def form_lookups(profile_key: str) -> Dict:
    """The answers a browser needs, because it cannot call this module.

    The setup form embeds its profile as JSON and narrows its dropdowns in
    the browser, so it has no way to ask a question per keystroke. These two
    maps are the answers precomputed, and **a lookup is not a rule** - which
    is the whole point, because the rules it replaces had both drifted::

        writes_by_drive          {drive: the densities it can write}
        default_drive_by_media   {library model: {density: the drive}}

    3.1 KB for IBM, the largest catalogue, on a page that is already 46 KB.
    The alternative was leaving two rules written twice, one of which
    answered a different drive from the terminal's.
    """
    key = resolve(profile_key)
    options = data.get_profile_options(key)
    densities = densities_of(options['drive_models'])

    by_media = {}
    for model in options['library_models']:
        for_model = {}
        for density in densities:
            drive = default_drive_for(key, model, media=density)
            if drive:
                for_model[density] = drive
        by_media[model] = for_model

    return {
        'writes_by_drive': {drive: writes(drive)
                            for drive in options['drive_models']},
        'default_drive_by_media': by_media,
    }


def resolve(profile_key: str) -> str:
    """The canonical profile key, or UnknownProfile with the valid ones.

    Case-insensitive, because the web reaches profiles through a URL segment
    (``/setup/brand/ibm/``) and the command line is typed by hand.
    """
    key = (profile_key or '').strip().upper()
    try:
        data.get_profile(key)
    except KeyError:
        raise UnknownProfile(
            f"'{profile_key}' is not a vendor profile",
            [f"profiles: {', '.join(data.list_profiles())}"]) from None
    return key


def names(*, media: str = None) -> List[str]:
    """Every profile, or only those whose drives can write ``media``.

    The question `mhvtl profile list --media LTO9` asks, and the one the
    vendor page's filter asks. Six of the nine profiles are LTO families;
    STK is T10000, SONY is AIT and QUANTUM is SDLT, and nothing used to say
    so until a library had already been built.
    """
    if not media:
        return list(data.list_profiles())
    wanted = media.strip().upper()
    return [key for key in data.list_profiles()
            if any(wanted == density.upper()
                   for drive in data.get_profile(key).drive_models
                   for density in writes(drive))]


def summaries() -> List[Dict]:
    """One row per vendor: how big each catalogue is, and what it would pick.

    ``default_media`` is the column that does the work. The counts are the
    weak ones - "which catalogue is biggest" is barely a question - but they
    cost nothing once the row is being composed and they do say where there
    is room to vary a library later.

    Every drive a profile declares is reachable from at least one of its
    library models (checked for all nine on 4 October 2026), so the counts
    here and the drives in ``describe`` cannot disagree.
    """
    rows = []
    for key in data.list_profiles():
        profile = data.get_profile(key)
        defaults = data.get_profile_options(key)['defaults']
        rows.append({
            'profile': key,
            'vendor': profile.library_vendor,
            'library_models': list(profile.library_models),
            'drive_models': list(profile.drive_models),
            'densities': densities_of(profile.drive_models),
            'model_count': len(profile.library_models),
            'drive_count': len(profile.drive_models),
            'default_model': defaults['library_model'],
            'default_drive': defaults['drive_model'],
            'default_media': defaults['media_type'],
            'default_revision': defaults['library_revision'],
        })
    return rows


def describe(profile_key: str, *, library_model: str = None,
             media: str = None, like: str = None) -> Dict:
    """One profile as a concept: its models, its drives, its densities.

    The three narrowings are the three ways the question is really asked:

        library_model   which drives will this model take?
        media           which models and drives write this tape?
        like            the one whose name I half remember

    They compose, and each one narrows both sections: asking for ``--media
    LTO9`` leaves only the models that can hold a drive which writes it, so a
    model listed here always has at least one drive listed under it.
    """
    key = resolve(profile_key)
    profile = data.get_profile(key)
    options = data.get_profile_options(key)

    if library_model and library_model not in options['library_models']:
        raise UnknownProfile(
            f"{key} does not make a '{library_model}'",
            [f"its models: {', '.join(options['library_models'])}"])

    everything = densities_of(profile.drive_models)
    wanted = (media or '').strip().upper() or None
    if wanted and wanted not in [density.upper() for density in everything]:
        raise UnknownProfile(
            f"no {key} drive takes {media}",
            [f"its densities: {', '.join(everything)}"])

    models = [library_model] if library_model else list(options['library_models'])
    rows, drives_seen = [], []
    for model in models:
        takes = [drive for drive in data.get_valid_drives_for_library(key, model)
                 if _matches(drive, wanted, like)]
        if (wanted or like) and not takes:
            # Narrowed away: a model with nothing left to offer is not an
            # answer, and listing it would suggest it could take the tape.
            continue
        limits = options['library_limits'].get(model) or {}
        rows.append({
            'model': model,
            'default': model == options['defaults']['library_model'],
            'drives': takes,
            'drive_count': len(takes),
            'default_drive': default_drive_for(key, model, media=wanted),
            'layout': limits.get('layout'),
            'max_drives': limits.get('max_drives'),
            'max_slots': limits.get('max_slots'),
            'max_maps': limits.get('max_maps'),
        })
        for drive in takes:
            if drive not in drives_seen:
                drives_seen.append(drive)

    default_drive = options['defaults']['drive_model']
    return {
        'profile': key,
        'vendor': profile.library_vendor,
        'library_model': library_model,
        'media': wanted,
        'like': like,
        'defaults': dict(options['defaults']),
        'models': rows,
        'drives': [{
            'model': drive,
            'generation': data.lto_generation(drive) or None,
            'loads': list(data.creatable_media(drive)),
            'writes': writes(drive),
            'reads_only': list(data.read_only_media(drive)),
            'personality': options['drive_personality'].get(drive),
            'default': drive == default_drive,
        } for drive in drives_seen],
        'densities': densities_of(drives_seen),
    }


#: Which family a density belongs to, for grouping a long list of them.
#:
#: Moved here from views.py:_media_family on 4 October 2026. The vendor page
#: had it to group its cards; `profile show` needs the same grouping for its
#: densities line, and the interactive create needs it to ask "which tape?" -
#: so it is one function rather than the three it was about to be.
_FAMILIES = (('LTO', 'LTO'), ('T10K', 'T10000'), ('9840', '9840'),
             ('9940', '9940'), ('AIT', 'AIT'), ('SDLT', 'SDLT'), ('DLT', 'DLT'))
_JAGUAR = {'J1A': '3592', 'E05': '3592', 'E06': '3592', 'E07': '3592'}


def media_family(density: str) -> str:
    """'LTO' for LTO9, 'T10000' for T10KB, '3592' for E07."""
    for prefix, family in _FAMILIES:
        if density.startswith(prefix):
            return family
    return _JAGUAR.get(density, density)


def media_order(density: str):
    """A sort key that puts a family's generations in generation order.

    Sorting the strings gives LTO1, LTO10, LTO10P, LTO2 - which is what both
    front ends printed until this existed. The generation is taken from the
    digits, so LTO10 follows LTO9, and LTO10P follows LTO10 because the
    suffix sorts after nothing. J1A, E05, E06, E07 come out in that order for
    the same reason, where sorting the strings put J1A last.
    """
    family = media_family(density)
    digits = ''.join(ch for ch in density if ch.isdigit())
    return (family, int(digits) if digits else 0, density)


def densities_of(drive_models) -> List[str]:
    """Every density these drives take, each once, family by family.

    Not a set, and not the order they were met in: a drive lists its native
    density first, so walking drives gives LTO3, LTO2, LTO1, LTO4 - true, and
    unreadable. Grouped by family and ordered by generation instead.
    """
    found = []
    for drive in drive_models:
        for density in data.creatable_media(drive):
            if density not in found:
                found.append(density)
    return sorted(found, key=media_order)


def _matches(drive: str, media: Optional[str], like: Optional[str]) -> bool:
    if media and media not in [d.upper() for d in writes(drive)]:
        return False
    if like and like.lower() not in drive.lower():
        return False
    return True
