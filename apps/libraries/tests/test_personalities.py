"""What MHVTL 1.8 emulates, checked against the MHVTL source itself.

services/profiles/personalities.py transcribes MHVTL's library layouts, its two
selectors and its id and LUN limits, with a line reference for each. A
transcription is only as good as the last time someone compared it, so the
SourceTests below re-read the MHVTL tree and fail if any of it has drifted -
after an upstream update, say, or a local patch.

They run against the tree in ../mhvtl beside this project, or MHVTL_SOURCE if
set, and are skipped when neither exists. Everything else here needs no source.
"""
import os
import re
import unittest
from pathlib import Path

from django.test import TestCase

from apps.libraries.services.profiles import personalities as p

GUI_ROOT = Path(__file__).resolve().parents[3]
SOURCE = Path(os.environ.get('MHVTL_SOURCE', GUI_ROOT.parent / 'mhvtl'))
HAVE_SOURCE = (SOURCE / 'usr' / 'cmd' / 'vtllibrary.c').exists()


def read(relative: str) -> list:
    return (SOURCE / relative).read_text(errors='replace').splitlines()


@unittest.skipUnless(HAVE_SOURCE, f'MHVTL source not found at {SOURCE}')
class SourceTests(TestCase):
    """The transcription against the source it transcribes."""

    def test_every_layout_matches_its_source_lines(self):
        for layout in p.LAYOUTS.values():
            with self.subTest(layout=layout.init):
                path, span = layout.source.split(':')
                first, last = (int(n) for n in span.split('-'))
                found = {}
                for line in read(path)[first - 1:last]:
                    match = re.search(r'start_(\w+)\s*=\s*(0x[0-9a-fA-F]+|\d+)', line)
                    if match:
                        found[match.group(1)] = int(match.group(2), 0)
                self.assertEqual(found, {
                    'picker': layout.start_picker, 'map': layout.start_map,
                    'drive': layout.start_drive, 'storage': layout.start_storage})

    def test_every_layout_is_the_one_its_init_function_uses(self):
        """The cited lines sit inside the named function, or in the file-level
        template that function registers unchanged."""
        for layout in p.LAYOUTS.values():
            with self.subTest(layout=layout.init):
                path, span = layout.source.split(':')
                first = int(span.split('-')[0])
                lines = read(path)
                start = next(i for i, line in enumerate(lines)
                             if re.match(rf'void {layout.init}\s*\(', line))
                end = next(i for i in range(start + 1, len(lines))
                           if lines[i].startswith('}'))
                body = '\n'.join(lines[start:end])
                if start < first - 1 <= end:
                    continue                    # overrides inside the function
                self.assertNotIn('start_drive', body,
                                 f'{layout.init} overrides the template it uses')

    def test_there_are_no_layouts_we_do_not_know_about(self):
        known = set(p.LAYOUTS)
        for path in sorted((SOURCE / 'usr' / 'pm').glob('*.c')):
            text = path.read_text(errors='replace')
            if 'smc_personality_module_register' not in text:
                continue
            for init in re.findall(r'^void (init_\w+)\s*\(', text, re.M):
                with self.subTest(init=init):
                    self.assertIn(init, known)

    def _selector_from_source(self):
        lines = read('usr/cmd/vtllibrary.c')

        def function(name):
            start = next(i for i, line in enumerate(lines)
                         if line.startswith(f'static void {name}('))
            end = next(i for i in range(start, len(lines)) if lines[i] == '}')
            return lines[start + 1:end]

        def parse(body):
            rules, pending = [], None
            for line in body:
                condition = re.search(
                    r'strncasecmp\(lu->(vendor|product)_id, "([^"]+)", (\d+)\)', line)
                action = re.search(r'\b((?:init|customise)_\w+)\(lu\)', line)
                if condition:
                    pending = (condition.group(1),
                               condition.group(2)[:int(condition.group(3))])
                elif action:
                    name = action.group(1)
                    outcome = (parse(function(name)) if name.startswith('customise')
                               else name)
                    field, prefix = pending if pending else (None, None)
                    rules.append((field, prefix, outcome))
                    pending = None
            return rules

        return parse(function('customise_lu'))

    def test_the_library_selector_matches_vtllibrary(self):
        def normalise(rules):
            return [(f, x.lower() if x else x,
                     o if isinstance(o, str) else normalise(o))
                    for f, x, o in rules]
        self.assertEqual(normalise(self._selector_from_source()),
                         normalise(p.LIBRARY_SELECTOR))

    def test_the_drive_table_matches_vtltape(self):
        text = '\n'.join(read('usr/cmd/vtltape.c'))
        table = re.search(r'tape_drives\[\] = \{(.*?)\{NULL, NULL\}', text, re.S).group(1)
        found = {name.rstrip(): init
                 for name, init in re.findall(r'\{"([^"]+)",\s*(\w+)\}', table)}
        self.assertEqual(found, p.DRIVE_PERSONALITIES)

    def test_the_limits_match_the_source(self):
        self.assertIn('#define MAXPRIOR 1024', ' '.join(read('include/utils/q.h')[49].split()))
        self.assertEqual(p.MAX_DEVICE_ID, 1023)
        self.assertIn('#define DEF_MAX_LUNS 32', ' '.join(read('kernel/mhvtl.c')[125].split()))
        self.assertEqual(p.MAX_LUN, 31)

    def test_every_cited_line_says_what_the_module_claims(self):
        cited = {
            ('include/utils/q.h', 50): 'MAXPRIOR',
            ('kernel/mhvtl.c', 126): 'DEF_MAX_LUNS',
            ('kernel/mhvtl.c', 186): 'DEF_MAX_MINOR_NO',
            ('kernel/mhvtl.c', 603): 'Max luns exceeded',
            ('usr/smc.c', 482): 'put_unaligned_be16(s->slot_location',
            ('usr/cmd/vtllibrary.c', 704): 'check_overflow',
            ('usr/cmd/vtllibrary.c', 815): 'slot_location = slt + smc_p->pm->start_drive - 1',
            ('usr/cmd/vtllibrary.c', 1286): 'NAA: %02x',
            ('usr/cmd/vtllibrary.c', 1421): 'customise_ibm_lu',
            ('usr/cmd/vtllibrary.c', 1430): 'customise_stk_lu',
            ('usr/cmd/vtllibrary.c', 1443): 'customise_hp_lu',
            ('usr/cmd/vtllibrary.c', 1450): 'customise_spectra_lu',
            ('usr/cmd/vtllibrary.c', 1461): 'customise_lu',
            ('usr/cmd/vtllibrary.c', 1610): 'my_id >= MAXPRIOR',
            ('usr/cmd/vtltape.c', 132): 'tape_drives_table',
            ('usr/cmd/vtltape.c', 1899): 'strncmp(tape_drives[i].name',
            ('usr/cmd/vtltape.c', 2153): '%-16s',
            ('usr/cmd/vtltape.c', 2430): 'my_id >= MAXPRIOR',
        }
        for (path, number), expected in cited.items():
            with self.subTest(reference=f'{path}:{number}'):
                self.assertIn(expected, read(path)[number - 1])


@unittest.skipUnless(HAVE_SOURCE, f'MHVTL source not found at {SOURCE}')
class LtoMediaSourceTests(TestCase):
    """Which cartridges each LTO drive loads, against the drive personalities.

    Both our tables used to be wrong here: profiles offered every generation
    down to LTO-1, and the compatibility table had LTO-8 reading LTO-6.
    """

    def _from_source(self):
        """{generation: (read_write, read_only)} from IBM and HP personalities."""
        found = {}
        for path, prefix in (('usr/pm/ult3580_pm.c', 'init_ult3580_td'),
                             ('usr/pm/hp_ultrium_pm.c', 'init_hp_ult_')):
            text = '\n'.join(read(path))
            for match in re.finditer(rf'\nvoid ({prefix}(\w+))\s*\([^)]*\)\s*\{{(.*?)\n\}}',
                                     text, re.S):
                rows = re.findall(r'add_drive_media_list\(lu, LOAD_(RW|RO), "(LTO\d+)"\)',
                                  match.group(3))
                if not rows:
                    continue
                generation = int(match.group(2))
                entry = ({n for mode, n in rows if mode == 'RW'},
                         {n for mode, n in rows if mode == 'RO'})
                self.assertEqual(found.setdefault(generation, entry), entry,
                                 f'IBM and HP disagree about LTO-{generation}')
            for match in re.finditer(r'ult_gen_media td(\w)_media\[\] = \{(.*?)\};',
                                     text, re.S):
                generation = 10 if match.group(1) == 'a' else int(match.group(1))
                rows = re.findall(r'\{&density_\w+,\s*(\d),\s*"([^"]+)"', match.group(2))
                found[generation] = ({n for w, n in rows if w == '1'},
                                     {n for w, n in rows if w != '1'})
        return found

    def test_the_profile_media_lists_match_mhvtl(self):
        from apps.libraries.services.profiles.data import (_lto_media,
                                                           lto_read_only_media)
        source = self._from_source()
        self.assertEqual(sorted(source), list(range(1, 11)))
        for generation, (read_write, read_only) in source.items():
            with self.subTest(generation=generation):
                self.assertEqual(set(_lto_media(generation)), read_write | read_only)
                self.assertEqual(set(lto_read_only_media(generation)), read_only)

    def test_the_compatibility_table_matches_mhvtl(self):
        from apps.libraries.services.tapes.compatibility import LTO_COMPATIBILITY

        def generation(name):                   # LTO10P is an LTO-10 cartridge
            # Not inside the f-string: a backslash in an f-string's expression
            # is a SyntaxError before Python 3.12, and the suite runs on 3.11.
            number = int(re.match(r'LTO(\d+)', name).group(1))
            return f'LTO-{number}'

        for number, (read_write, read_only) in self._from_source().items():
            with self.subTest(generation=number):
                table = LTO_COMPATIBILITY[f'LTO-{number}']
                self.assertEqual(set(table['write']),
                                 {generation(n) for n in read_write})
                self.assertEqual(set(table['read']),
                                 {generation(n) for n in read_write | read_only})


class LayoutArithmeticTests(TestCase):
    """The capacity rule, independent of the source."""

    def test_a_range_holds_up_to_the_next_start(self):
        stk = p.LAYOUTS['init_stklxx']            # drives 500, storage 1000
        self.assertEqual(stk.max_drives, 500)

    def test_the_top_range_is_bounded_by_the_address_width(self):
        stk = p.LAYOUTS['init_stklxx']            # storage is highest at 1000
        self.assertEqual(stk.max_slots, 0x10000 - 1000)

    def test_the_small_models_are_small(self):
        self.assertEqual(p.LAYOUTS['init_spectra_215_smc'].max_drives, 55)
        self.assertEqual(p.LAYOUTS['init_spectra_215_smc'].max_slots, 30)
        self.assertEqual(p.LAYOUTS['init_spectra_gator_smc'].max_drives, 32)
        self.assertEqual(p.LAYOUTS['init_hp_msl_smc'].max_slots, 416)
        self.assertEqual(p.LAYOUTS['init_overland_smc'].max_slots, 253)
        self.assertEqual(p.LAYOUTS['init_overland_smc'].max_maps, 1)

    def test_hp_eml_is_not_hp_msl(self):
        """An earlier summary gave EML the MSL numbers."""
        self.assertEqual(p.LAYOUTS['init_hp_eml_smc'].max_drives, 500)
        self.assertEqual(p.LAYOUTS['init_hp_msl_smc'].max_slots, 416)

    def test_limits_problems_names_the_model_and_the_source(self):
        problems = p.limits_problems('SPECTRA', '215', drives=60, slots=40)
        self.assertEqual(len(problems), 2)
        self.assertIn('Spectra 215', problems[0])
        self.assertIn('usr/pm/spectra_pm.c', problems[0])

    def test_within_limits_is_no_problem(self):
        self.assertEqual(p.limits_problems('STK', 'L700', drives=4, slots=40,
                                           maps=4), [])


class SelectorTests(TestCase):
    """How our profiles' strings resolve. The expectations are the answer
    MHVTL gives, not the one the vendor name suggests."""

    def test_stk_models(self):
        self.assertEqual(p.library_layout('STK', 'L700').init, 'init_stklxx')
        self.assertEqual(p.library_layout('STK', 'L80').init, 'init_stkl20')
        self.assertEqual(p.library_layout('STK', 'SL500').init, 'init_stkslxx')

    def test_matching_is_case_insensitive_for_libraries(self):
        self.assertEqual(p.library_layout('stk', 'l80').init, 'init_stkl20')

    def test_quantum_and_adic_match_on_the_product(self):
        self.assertEqual(p.library_layout('QUANTUM', 'Scalar i500').init,
                         'init_default_smc')
        self.assertEqual(p.library_layout('X', 'QUANTUM i500').init,
                         'init_scalar_smc')

    def test_an_unknown_vendor_gets_the_default_layout(self):
        self.assertEqual(p.library_layout('SONY', 'LIB-302').init, 'init_default_smc')

    def test_drive_matching_is_exact_and_case_sensitive(self):
        self.assertEqual(p.drive_personality('ULT3580-TD8'), 'init_ult3580_td8')
        self.assertEqual(p.drive_personality('ult3580-td8'), p.GENERIC_DRIVE)
        self.assertEqual(p.drive_personality('ULT3580-TD8X'), p.GENERIC_DRIVE)

    def test_half_height_drives_share_the_full_height_personality(self):
        self.assertEqual(p.drive_personality('ULT3580-HH9'), 'init_ult3580_td9')


@unittest.skipUnless((GUI_ROOT / 'docs' / 'sphinx').is_dir(),
                     'the generated guides (docs/sphinx) are not in this checkout')
class DocumentationTests(TestCase):
    """The generated guide pages match the code they are generated from."""

    PAGES = ('library-limits', 'hardware')

    def test_the_generated_pages_are_up_to_date(self):
        import subprocess
        import sys
        import tempfile

        tools = GUI_ROOT / 'docs' / 'sphinx' / 'tools'
        for page in self.PAGES:
            with self.subTest(page=page), tempfile.TemporaryDirectory() as scratch:
                fresh = Path(scratch) / f'{page}.rst'
                subprocess.run([sys.executable, str(tools / 'fill_library_limits.py'),
                                str(tools / f'{page}.rst.in'), str(fresh)],
                               cwd=GUI_ROOT, check=True, capture_output=True)
                committed = GUI_ROOT / 'docs' / 'sphinx' / 'guides' / f'{page}.rst'
                self.assertEqual(
                    committed.read_text(), fresh.read_text(),
                    f'regenerate it: python docs/sphinx/tools/fill_library_limits.py '
                    f'docs/sphinx/tools/{page}.rst.in docs/sphinx/guides/{page}.rst')

    def test_the_docs_contain_no_markdown(self):
        """The Sphinx tree is reStructuredText only."""
        docs = GUI_ROOT / 'docs' / 'sphinx'
        found = [str(path.relative_to(docs)) for path in docs.rglob('*.md')
                 if '_build' not in path.parts]
        self.assertEqual(found, [])
