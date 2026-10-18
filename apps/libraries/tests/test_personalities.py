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

    @staticmethod
    def _start_addresses(path: str, init: str) -> dict:
        """The four element addresses MHVTL gives a personality, from source.

        Resolved the way the C does rather than read out of a line range. A
        personality sets them either in its init function

            smc_pm.start_picker  = 0x0001;

        or in the file-level template the function registers unchanged

            static struct smc_personality_template smc_pm = {
                    .start_picker = 0x0001,

        so the function wins and the template is the fallback.

        LAYOUTS still carries a "file.c:109-112" citation, and it is still
        worth having - it tells a reader where to look. It is NOT what this
        test reads. Line numbers are only ever right for one commit: all of
        them were exact against MHVTL 1.8 and every one moved when upstream
        was pulled forward, failing fifteen subtests here that were about
        nothing but the file having grown. The console supports 1.8 and up,
        so a check that can only pass against one revision is not a check.
        """
        text = '\n'.join(read(path))
        found = {}

        template = re.search(r'struct smc_personality_template\s+\w+\s*=\s*\{(.*?)\n\};',
                             text, re.S)
        if template:
            for name, value in re.findall(
                    r'\.start_(\w+)\s*=\s*(0x[0-9a-fA-F]+|\d+)', template.group(1)):
                found[name] = int(value, 0)

        body = re.search(rf'\nvoid {init}\s*\([^)]*\)\s*\{{(.*?)\n\}}', text, re.S)
        if body:
            for name, value in re.findall(
                    r'\.?start_(\w+)\s*=\s*(0x[0-9a-fA-F]+|\d+)', body.group(1)):
                found[name] = int(value, 0)
        return found

    #: An address that differs between the MHVTLs we support, and what the
    #: older one says. Unlike the selector, a single number cannot be a union,
    #: so the table carries the current value and this records the other.
    #:
    #: 27d4a80 (11 October 2026) moved the 3584's first import/export element
    #: from 0x300 to 0x301: IBM's TS3500 manual puts import/export at 769 to
    #: 1023 and reserves 512 to 768 for shuttle stations. It costs one MAP
    #: slot - 255 rather than 256, IBM's documented maximum - and only
    #: software caching absolute element addresses sees it, since
    #: library_contents and vtlcmd both use relative MAP numbers.
    ALSO_VALID = {('init_ibm3584', 'map'): 0x0300}

    def test_every_layout_matches_the_addresses_its_personality_sets(self):
        for layout in p.LAYOUTS.values():
            with self.subTest(layout=layout.init):
                path = layout.source.split(':')[0]
                found = self._start_addresses(path, layout.init)
                wanted = {'picker': layout.start_picker, 'map': layout.start_map,
                          'drive': layout.start_drive,
                          'storage': layout.start_storage}
                for name, value in wanted.items():
                    other = self.ALSO_VALID.get((layout.init, name))
                    if other is not None and found.get(name) == other:
                        continue           # the older MHVTL's value, recorded
                    self.assertEqual(found.get(name), value,
                                     f'{layout.init} start_{name}')

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
            """Every rule the C expresses, one per (field, prefix) tested.

            A branch may test several things with ||, over several lines:

                else if (!strncasecmp(lu->vendor_id, "ADIC", 4) ||
                         !strncasecmp(lu->vendor_id, "QUANTUM", 7) ||
                         !strncasecmp(lu->product_id, "Scalar", 6))
                        init_scalar_smc(lu);

            That is three rules leading to one personality, and an earlier
            version of this parser kept only the last condition before the
            action - reading the branch above as "product Scalar" alone and
            losing both vendor rules. Conditions accumulate until an action
            consumes them.
            """
            rules, pending = [], []
            for line in body:
                condition = re.search(
                    r'strncasecmp\(lu->(vendor|product)_id, "([^"]+)", (\d+)\)', line)
                action = re.search(r'\b((?:init|customise)_\w+)\(lu\)', line)
                if condition:
                    pending.append((condition.group(1),
                                    condition.group(2)[:int(condition.group(3))]))
                elif action:
                    name = action.group(1)
                    outcome = (parse(function(name)) if name.startswith('customise')
                               else name)
                    for field, prefix in pending or [(None, None)]:
                        rules.append((field, prefix, outcome))
                    pending = []
            return rules

        return parse(function('customise_lu'))

    @staticmethod
    def _leaves(rules, outer=()):
        """Every (vendor-test, product-test, personality) the rules can reach.

        Flattened deliberately. Comparing the nested shape makes one changed
        sub-rule look like a whole vendor branch mismatching - when upstream
        added a "1x8" match beside HP's "MSL", a nested comparison reported
        the entire HP branch as unknown and said nothing about which rule had
        moved. A leaf is what actually decides a personality, so a leaf is
        what is compared.
        """
        found = set()
        for field, prefix, outcome in rules:
            here = outer + ((field, prefix.lower()),) if prefix else outer
            if isinstance(outcome, str):
                found.add((here, outcome))
            else:
                found |= SourceTests._leaves(outcome, here)
        return found

    def test_the_library_selector_covers_everything_vtllibrary_tests(self):
        """The table must contain every rule the source has.

        Not equality. LIBRARY_SELECTOR is deliberately the UNION of two MHVTL
        versions - see the comment above it - because the Scalar rules changed
        in ef7b972 and the HP rules in the same release, while the console
        supports 1.8 as well as current upstream. So the table is a superset
        of whichever source it is checked against, and what matters is that
        nothing the source tests is missing from it.

        The extra rules are checked separately, below, so the union cannot
        quietly grow something nobody decided on.
        """
        missing = self._leaves(self._selector_from_source()) - \
            self._leaves(p.LIBRARY_SELECTOR)
        self.assertEqual(missing, set(),
                         'LIBRARY_SELECTOR is missing rules vtllibrary.c tests')

    def test_the_selector_carries_no_rule_nobody_decided_on(self):
        """Everything in the table this MHVTL does not test is kept for another
        version, and every one of them is listed here.

        Read against current upstream the extras are 1.8's two ADIC/QUANTUM
        product matches; read against 1.8 they are the vendor matches ef7b972
        added, its kept "Scalar" product match, and HP's "1x8". Both
        directions are allowed, because both are MHVTLs the console supports.
        """
        allowed = {
            # 1.8 matched ADIC and QUANTUM on the product (24f7342)
            ((('product', 'adic'),), 'init_scalar_smc'),
            ((('product', 'quantum'),), 'init_scalar_smc'),
            # ef7b972 matches them on the vendor, and keeps a product match
            ((('vendor', 'adic'),), 'init_scalar_smc'),
            ((('vendor', 'quantum'),), 'init_scalar_smc'),
            ((('product', 'scalar'),), 'init_scalar_smc'),
            # and the HP 1U autoloader, "1x8 G3 AUTOLDR", added after 1.8
            ((('vendor', 'hp'), ('product', '1x8')), 'init_hp_msl_smc'),
        }
        extra = self._leaves(p.LIBRARY_SELECTOR) - \
            self._leaves(self._selector_from_source())
        self.assertEqual(extra - allowed, set(),
                         'a rule in LIBRARY_SELECTOR is in neither this MHVTL '
                         'nor the documented set kept for another version')

    def test_the_drive_table_matches_vtltape(self):
        text = '\n'.join(read('usr/cmd/vtltape.c'))
        table = re.search(r'tape_drives\[\] = \{(.*?)\{NULL, NULL\}', text, re.S).group(1)
        found = {name.rstrip(): init
                 for name, init in re.findall(r'\{"([^"]+)",\s*(\w+)\}', table)}
        self.assertEqual(found, p.DRIVE_PERSONALITIES)

    def test_the_limits_match_the_source(self):
        source = '\n'.join(read('include/utils/q.h'))
        self.assertIn('#define MAXPRIOR 1024', source)
        self.assertEqual(p.MAX_DEVICE_ID, 1023)
        source = '\n'.join(read('kernel/mhvtl.c'))
        self.assertIn('#define DEF_MAX_LUNS 32', source)
        self.assertEqual(p.MAX_LUN, 31)

    #: What personalities.py says MHVTL's source says, as (file, text) rather
    #: than (file, line). Every one of these was a line number once, and every
    #: one of them was exact against MHVTL 1.8 - then upstream moved 172
    #: commits forward and twenty-four of them pointed at the wrong line while
    #: still being perfectly true about the source.
    #:
    #: A line number can only be right for one revision, and the console
    #: supports 1.8 and up. The citations in personalities.py keep theirs,
    #: because a reader wants somewhere to look; what is CHECKED is that the
    #: code it describes is still there.
    CITED = [
        ('include/utils/q.h', '#define MAXPRIOR'),
        ('kernel/mhvtl.c', '#define DEF_MAX_LUNS'),
        ('kernel/mhvtl.c', '#define DEF_MAX_MINOR_NO'),
        ('kernel/mhvtl.c', 'Max luns exceeded'),
        ('usr/smc.c', 'put_unaligned_be16(s->slot_location'),
        ('usr/cmd/vtllibrary.c', 'check_overflow'),
        ('usr/cmd/vtllibrary.c', 'slot_location = slt + smc_p->pm->start_drive - 1'),
        ('usr/cmd/vtllibrary.c', 'NAA: %02x'),
        ('usr/cmd/vtllibrary.c', 'customise_ibm_lu'),
        ('usr/cmd/vtllibrary.c', 'customise_stk_lu'),
        ('usr/cmd/vtllibrary.c', 'customise_hp_lu'),
        ('usr/cmd/vtllibrary.c', 'customise_spectra_lu'),
        ('usr/cmd/vtllibrary.c', 'customise_lu'),
        ('usr/cmd/vtllibrary.c', 'my_id >= MAXPRIOR'),
        ('usr/cmd/vtltape.c', 'tape_drives_table'),
        ('usr/cmd/vtltape.c', 'strncmp(tape_drives[i].name'),
        ('usr/cmd/vtltape.c', 'my_id >= MAXPRIOR'),
        # The serial number. Ten is not a file limit and not the buffer: it is
        # the window the LIBRARY reports in its element descriptor, which is
        # what a backup application enumerating drives through the changer
        # reads. See SerialNumberTests below and guides/plan-drive-serials.
        ('include/vtllib.h', '#define SCSI_SN_LEN'),
        ('usr/cmd/vtltape.c', 'conf_clamp_string(v,'),
        ('usr/cmd/vtllibrary.c', 'conf_clamp_string(v,'),
        ('usr/vtllib.c', 'lu_serial_no'),
    ]

    def test_every_cited_thing_is_still_in_the_source(self):
        for path, expected in self.CITED:
            with self.subTest(reference=f'{path}: {expected}'):
                self.assertIn(expected, '\n'.join(read(path)),
                              f'{expected!r} is no longer in {path}; '
                              f'personalities.py describes code that has gone')


class SerialNumberTests(TestCase):
    """One serial rule, and the two properties MHVTL's source makes necessary.

    A serial has to be unique - it is what a backup application identifies a
    device by - and at most ten characters, because standard INQUIRY and VPD
    page 0x83 report only the first ten while page 0x80 reports the whole
    string. Above ten, one device has two identities and two devices can share
    one of them.

    `{library_serial}D{slot}` broke both at slot 10: eleven characters, and
    `80000085D10` and `80000085D1` are the same ten bytes to INQUIRY.
    """

    def test_a_serial_is_the_device_id_with_a_fixed_prefix(self):
        self.assertEqual(p.device_serial(85), '80000085')
        self.assertEqual(p.device_serial(1), '80000001')
        self.assertEqual(p.device_serial(1023), '80001023')

    def test_every_serial_fits_the_inquiry_window(self):
        """Every id MHVTL can have, not a sample of them."""
        for device_id in range(1, p.MAX_DEVICE_ID + 1):
            self.assertLessEqual(len(p.device_serial(device_id)),
                                 p.MAX_SERIAL_LENGTH)

    def test_no_two_devices_share_a_serial(self):
        """Including in the first ten characters, which is the only part
        standard INQUIRY and page 0x83 report."""
        serials = [p.device_serial(device_id)[:p.MAX_SERIAL_LENGTH]
                   for device_id in range(1, p.MAX_DEVICE_ID + 1)]
        self.assertEqual(len(set(serials)), len(serials))

    def test_a_library_and_its_drives_read_as_one_family(self):
        """Library 85 is 80000085 and its drives 86-89 are 80000086-89, which
        is the point of using the id: the devices of one library are visibly
        related without the serial having to carry a slot."""
        library = p.device_serial(85)
        self.assertEqual([p.device_serial(i) for i in (86, 87, 88, 89)],
                         ['80000086', '80000087', '80000088', '80000089'])
        self.assertTrue(all(drive[:4] == library[:4]
                            for drive in (p.device_serial(86),
                                          p.device_serial(89))))

    def test_a_hand_given_serial_is_cut_rather_than_refused(self):
        self.assertEqual(p.truncate_serial('  ABCDEFGHIJKLMNOP  '),
                         'ABCDEFGHIJ')
        self.assertEqual(len(p.truncate_serial('X' * 40)),
                         p.MAX_SERIAL_LENGTH)
        self.assertEqual(p.truncate_serial(None), '')

    def test_the_slot_based_scheme_this_replaced_would_collide(self):
        """The defect, written down as a test so it cannot come back: two
        drives of one library reported the same ten bytes to INQUIRY."""
        old = [f'{p.device_serial(85)}D{slot}' for slot in (1, 10)]
        self.assertNotEqual(old[0], old[1])
        self.assertEqual(old[0][:p.MAX_SERIAL_LENGTH],
                         old[1][:p.MAX_SERIAL_LENGTH])
        new = [p.device_serial(86), p.device_serial(95)]
        self.assertNotEqual(new[0][:p.MAX_SERIAL_LENGTH],
                            new[1][:p.MAX_SERIAL_LENGTH])


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

    def test_a_real_scalar_gets_the_scalar_layout(self):
        """What a Quantum or ADIC library actually reports: the vendor name in
        the vendor field, a model in the product field.

        This used to assert the opposite - that QUANTUM / Scalar i500 got the
        DEFAULT layout - because MHVTL 1.8 matched ADIC and QUANTUM against
        the product, so the vendor name never appeared where it was looked
        for. Upstream calls that a defect and fixed it in ef7b972, and the
        table is now the union of both versions, so a real Scalar resolves.
        """
        self.assertEqual(p.library_layout('QUANTUM', 'Scalar i500').init,
                         'init_scalar_smc')
        self.assertEqual(p.library_layout('ADIC', 'Scalar i2000').init,
                         'init_scalar_smc')

    def test_the_old_product_match_still_resolves(self):
        """The 1.8 spelling keeps working, which is the point of the union.

        A device.conf written against the old check puts the vendor name in
        the product - "QUANTUM i500" - and upstream deliberately kept a
        product match so those configurations are not broken by the fix.
        """
        self.assertEqual(p.library_layout('X', 'QUANTUM i500').init,
                         'init_scalar_smc')
        self.assertEqual(p.library_layout('X', 'ADIC i2000').init,
                         'init_scalar_smc')

    def test_overland_still_matches_on_the_product_only(self):
        """Unchanged by ef7b972, and a genuine difference rather than an
        oversight: only a product beginning OVERLAND gets that personality, so
        the NEO Series - the model name real hardware reports - does not."""
        self.assertEqual(p.library_layout('OVERLAND', 'OVERLAND').init,
                         'init_overland_smc')
        self.assertEqual(p.library_layout('OVERLAND', 'NEO Series').init,
                         'init_default_smc')

    def test_the_hp_autoloader_shares_the_msl_layout(self):
        """Added upstream after 1.8: the 1U autoloader reports "1x8 G3
        AUTOLDR" rather than an MSL name, and the EML E-Series it otherwise
        falls through to has a different element layout."""
        self.assertEqual(p.library_layout('HP', '1x8 G3 AUTOLDR').init,
                         'init_hp_msl_smc')
        self.assertEqual(p.library_layout('HP', 'MSL G3 Series').init,
                         'init_hp_msl_smc')
        self.assertEqual(p.library_layout('HP', 'EML E-Series').init,
                         'init_hp_eml_smc')

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
