"""Media names, barcode suffixes and drive media lists, against MHVTL itself.

services/profiles/personalities.py carries three tables that decide whether a
tape can be created and whether it can be mounted:

    DRIVE_MEDIA         what each drive personality loads, and writes
    SUFFIX_BY_DENSITY   the barcode suffix of each density
    DENSITY_BY_SUFFIX   the reverse, with WORM suffixes

The profiles used to name media after drives (T10000A, DLT7000), which mktape
refuses, and gave 9840 and SDLT drives cartridges MHVTL does not load. These
tests read the MHVTL tree and fail if the tables drift from it; the LTO
personalities are checked in test_personalities.LtoMediaSourceTests.

Skipped when the MHVTL source is not beside this project (or at MHVTL_SOURCE).
"""
import re
import unittest

from django.test import TestCase

from apps.libraries.services.profiles import personalities as p
from apps.libraries.services.profiles.data import PROFILES, get_media_suffix
from apps.libraries.services.tapes import barcodes
from apps.libraries.services.config import library_contents

from .test_personalities import HAVE_SOURCE, SOURCE, read

#: MHVTL's media names (the drive media lists) to mktape density names
#: (set_media_params), joined by the media type both map to.
MEDIA_NAME_TO_DENSITY = {
    'T10KA': 'T10KA', 'T10KB': 'T10KB', 'T10KC': 'T10KC',
    '03592 JA': 'J1A', '03592 JB': 'E05', '03592 JC': 'E06', '03592 JK': 'E07',
    'SDLT': 'SDLT1', 'SDLT 220': 'SDLT220', 'SDLT 320': 'SDLT320',
    'SDLT 600': 'SDLT600',
}

#: Suffixes upstream make_vtl_media cannot read back (make_vtl_media.in:91 at
#: 59f32ee): its second-character class is [123456789ABHKUVWXYZ], which has no
#: C or T, and it has no branch for JC. The local tree carries the fix,
#: patches/1.8-upstream-submission/0006; these tests accept either tree.
UPSTREAM_GAPS = ('JC', 'LT', 'TC')


def _tree_has_patch_0006(script: str) -> bool:
    """The fix adds a JC -> E06 branch; upstream has none."""
    return 'DENSITY="E06"' in script

NON_LTO_FILES = ('usr/pm/t10000_pm.c', 'usr/pm/stk9x40_pm.c',
                 'usr/pm/quantum_dlt_pm.c', 'usr/pm/ibm_03592_pm.c',
                 'usr/pm/ait_pm.c')


def _densities_from_source():
    """Every density name set_media_params() accepts, with its media type."""
    text = '\n'.join(read('usr/vtllib.c'))
    body = text[text.index('unsigned int set_media_params'):]
    body = body[:body.index('\n}\n')]
    return dict(re.findall(
        r'strn?cmp\(density, "([^"]+)"(?:, \d+)?\)\)\) \{.*?MediaType\s*=\s*(\w+)',
        body, re.S))


@unittest.skipUnless(HAVE_SOURCE, f'MHVTL source not found at {SOURCE}')
class NonLtoMediaSourceTests(TestCase):

    def _from_source(self):
        found = {}
        for path in NON_LTO_FILES:
            text = '\n'.join(read(path))
            for match in re.finditer(r'\nvoid (init_\w+)\s*\([^)]*\)\s*\{(.*?)\n\}',
                                     text, re.S):
                rows = re.findall(r'add_drive_media_list\(lu, LOAD_(RW|RO), "([^"]+)"\)',
                                  match.group(2))
                rows = [(mode, MEDIA_NAME_TO_DENSITY.get(name, name))
                        for mode, name in rows
                        if not name.endswith(('Clean', 'WORM'))]
                if rows:
                    found[match.group(1)] = (
                        {n for mode, n in rows if mode == 'RW'},
                        {n for mode, n in rows if mode == 'RO'})
        return found

    def test_every_non_lto_drive_media_list_matches_mhvtl(self):
        source = self._from_source()
        ours = {init: support for init, support in p.DRIVE_MEDIA.items()
                if not init.startswith(('init_ult3580', 'init_hp_ult'))}
        self.assertEqual(set(ours), set(source))
        for init, (read_write, read_only) in source.items():
            with self.subTest(init=init):
                self.assertEqual(set(ours[init].read_write), read_write)
                self.assertEqual(set(ours[init].read_only), read_only)

    def test_every_drive_personality_has_a_media_list(self):
        for product, init in p.DRIVE_PERSONALITIES.items():
            with self.subTest(product=product):
                self.assertIn(init, p.DRIVE_MEDIA)

    def test_every_media_name_is_a_density_mktape_accepts(self):
        densities = _densities_from_source()
        for support in p.DRIVE_MEDIA.values():
            for density in support.loads:
                with self.subTest(density=density):
                    self.assertIn(density, densities)

    def test_the_media_name_translation_is_by_media_type(self):
        """'03592 JC' and E06 both mean Media_3592_JX, and so on."""
        densities = _densities_from_source()
        names = {}
        for path in ('usr/pm/default_ssc_pm.c', 'usr/pm/ibm_03592_pm.c',
                     'usr/pm/quantum_dlt_pm.c', 'usr/pm/t10000_pm.c'):
            for name, media_type in re.findall(r'\{"([^"]+)",\s*(Media_\w+)',
                                               '\n'.join(read(path))):
                names.setdefault(name, media_type)
        for name, density in MEDIA_NAME_TO_DENSITY.items():
            with self.subTest(name=name):
                self.assertEqual(names[name], densities[density])


    def test_native_capacities_match_mhvtl(self):
        text = '\n'.join(read('usr/vtllib.c'))
        body = text[text.index('uint64_t media_native_capacity'):]
        body = body[:body.index('\n}\n')]
        capacity, pending = {}, []
        for line in body.splitlines():
            case = re.search(r'case (Media_\w+):', line)
            if case:
                pending.append(case.group(1))
            size = re.search(r'return (\d+) \* GB;', line)
            if size:
                for media_type in pending:
                    capacity[media_type] = int(size.group(1))
                pending = []
        densities = _densities_from_source()
        for density, gb in p.NATIVE_CAPACITY_GB.items():
            with self.subTest(density=density):
                self.assertEqual(capacity[densities[density]], gb)
        listed = {d for d, t in densities.items()
                  if t in capacity and not d.startswith('DDS')}
        self.assertEqual(set(p.NATIVE_CAPACITY_GB), listed)
        # 9840 and 9940 fall to the 1 GB default branch
        self.assertEqual({d for d in densities if densities[d] not in capacity},
                         {'9840A', '9840B', '9840C', '9840D', '9940A', '9940B'})


@unittest.skipUnless(HAVE_SOURCE, f'MHVTL source not found at {SOURCE}')
class SuffixSourceTests(TestCase):

    def _set_density(self):
        """{suffix: density} by running make_vtl_media's own set_density()."""
        import subprocess
        script = '\n'.join(read('usr/cmd/make_vtl_media.in'))
        start = script.index('set_density()')
        end = script.index('\n}\n', start) + 3
        suffixes = sorted(p.DENSITY_BY_SUFFIX)
        program = script[start:end] + ''.join(
            f'set_density ABC123{s}; echo "{s} $DENSITY"\n' for s in suffixes)
        output = subprocess.run(['bash', '-c', program], capture_output=True,
                                text=True, check=True).stdout
        return dict(line.split() for line in output.splitlines())

    def _script(self):
        return '\n'.join(read('usr/cmd/make_vtl_media.in'))

    def test_every_suffix_reads_back_as_its_density(self):
        found = self._set_density()
        patched = _tree_has_patch_0006(self._script())
        for suffix, density in p.DENSITY_BY_SUFFIX.items():
            if suffix in UPSTREAM_GAPS and not patched:
                continue
            with self.subTest(suffix=suffix):
                self.assertEqual(found[suffix], density)

    def test_the_gaps_are_exactly_what_patch_0006_fixes(self):
        """In an unpatched tree the three gaps, and only those, are UNKNOWN.
        When upstream merges the fix this still passes (the tree then counts
        as patched), and the note in SUFFIX_BY_DENSITY can go."""
        found = self._set_density()
        unknown = {s for s, d in found.items() if d == 'UNKNOWN'}
        if _tree_has_patch_0006(self._script()):
            self.assertEqual(unknown, set())
        else:
            self.assertEqual(unknown, set(UPSTREAM_GAPS))

    def test_every_documented_suffix_is_known(self):
        """The legend is not complete (it has no TB, S1 or X1), but everything
        it lists must be in the table."""
        legend = '\n'.join(read('etc/generate_library_contents.in'))
        documented = set(re.findall(r'"([A-Z0-9]{2})" -', legend))
        self.assertGreater(len(documented), 20)
        self.assertEqual(documented - set(p.DENSITY_BY_SUFFIX), set())


class OneTableTests(TestCase):
    """There used to be three suffix tables, and they disagreed."""

    def test_the_other_modules_use_the_same_table(self):
        self.assertIs(barcodes.DENSITY_BY_SUFFIX, p.DENSITY_BY_SUFFIX)
        self.assertIs(barcodes.SUFFIX_BY_DENSITY, p.SUFFIX_BY_DENSITY)
        self.assertIs(library_contents.DENSITY_BY_SUFFIX, p.DENSITY_BY_SUFFIX)

    def test_a_barcode_made_for_a_density_reads_back_as_it(self):
        for density, suffix in p.SUFFIX_BY_DENSITY.items():
            with self.subTest(density=density):
                self.assertEqual(barcodes.density_for(f'ABC001{suffix}'), density)

    def test_every_profile_medium_can_be_created(self):
        for key, profile in PROFILES.items():
            media = set(profile.library_supported_media)
            for drive, offered in profile.drive_media_by_model.items():
                for medium in offered:
                    with self.subTest(profile=key, drive=drive, medium=medium):
                        self.assertTrue(get_media_suffix(medium))
                        self.assertIn(medium, media)

    def test_every_profile_drive_offers_what_mhvtl_loads(self):
        for key, profile in PROFILES.items():
            for drive, offered in profile.drive_media_by_model.items():
                with self.subTest(profile=key, drive=drive):
                    self.assertEqual(offered, p.creatable_media(drive))

    def test_legacy_names_are_gone(self):
        for key, profile in PROFILES.items():
            names = set(profile.library_supported_media)
            for offered in profile.drive_media_by_model.values():
                names |= set(offered)
            with self.subTest(profile=key):
                self.assertFalse(names & {'T10000A', 'T10000B', 'T10000C',
                                          'DLT7000', 'DLT8000', 'SDLT3'})
