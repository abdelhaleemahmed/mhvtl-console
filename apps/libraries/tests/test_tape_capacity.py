"""How big a new cartridge is, and why that is one rule.

Two numbers stood in for a cartridge's capacity until 4 October 2026, and
they disagreed:

    services/tapes/service.py          DEFAULT_SIZE_MB = 500
    mhvtl_cli/commands/tape.py         --size-mb default=500000  (x2)
    tape_operations_views.py           '500000'                  (x3)
    create_tape.html, _bulk.html       value="500000"            (x2)
    static/js/tape-media.js            UNKNOWN_SIZE_MB = 1000

So a library's own tapes were 500 MB and any tape added to it afterwards was
500 GB. `mhvtl tape list` showed them side by side - 500 for the three LTO-8
cartridges a library was created with, 500000 for the LTO-6 added next to
them - which is how this was found.

Neither number was ever a cartridge. The capacities are in
profiles/personalities.NATIVE_CAPACITY_GB, transcribed from MHVTL and checked
against it, and the create forms were already *suggesting* them through
`native_mb`. Now they are the default, and every front end leaves the number
alone.
"""
import re
import shutil
from pathlib import Path
from unittest import mock

from .base import SimpleTestCase, TestCase

from apps.libraries.services.core.shell import CommandResult
from apps.libraries.services.profiles import personalities
from apps.libraries.services.tapes import TapeService, media
from apps.libraries.services.tapes.service import (UNKNOWN_SIZE_MB,
                                                   native_size_mb)

GUI = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).parent / 'fixtures'


def ok(stdout=''):
    return CommandResult(['fake'], 0, stdout, '')


class NativeSizeTests(SimpleTestCase):

    def test_it_is_the_density_s_native_capacity(self):
        """A real cartridge, not a round number: an LTO-8 holds 12 TB."""
        self.assertEqual(native_size_mb('LTO8'), 12_000_000)
        self.assertEqual(native_size_mb('LTO6'), 2_500_000)
        self.assertEqual(native_size_mb('LTO9'), 18_000_000)
        self.assertEqual(native_size_mb('T10KC'), 5_000_000)
        self.assertEqual(native_size_mb('AIT4'), 200_000)

    def test_every_density_mhvtl_knows_gets_its_own_capacity(self):
        for density, capacity_gb in personalities.NATIVE_CAPACITY_GB.items():
            with self.subTest(density=density):
                self.assertEqual(native_size_mb(density), capacity_gb * 1000)

    def test_a_density_with_no_native_capacity_gets_mhvtl_s_own_fallback(self):
        """MHVTL gives 9840 and 9940 one gigabyte, and so does this."""
        self.assertNotIn('9840C', personalities.NATIVE_CAPACITY_GB)
        self.assertEqual(native_size_mb('9840C'), UNKNOWN_SIZE_MB)
        self.assertEqual(UNKNOWN_SIZE_MB, 1000)

    def test_it_does_not_mind_the_case_or_a_missing_density(self):
        self.assertEqual(native_size_mb('lto8'), 12_000_000)
        self.assertEqual(native_size_mb(None), UNKNOWN_SIZE_MB)
        self.assertEqual(native_size_mb(''), UNKNOWN_SIZE_MB)

    def test_a_capacity_fits_the_forms_own_maximum(self):
        """The create forms cap the field at 50 TB; the largest cartridge
        MHVTL knows has to be offerable in them."""
        largest = max(native_size_mb(d)
                      for d in personalities.NATIVE_CAPACITY_GB)
        self.assertLessEqual(largest, 50_000_000)


class WhatGetsCreatedTests(TestCase):
    """The size that actually reaches mktape."""

    def setUp(self):
        self.config = self.tmpdir()
        for name in ('device.conf', 'library_contents.10'):
            shutil.copy(FIXTURES / name, self.config)
        self.media = self.tmpdir()
        self.service = TapeService(self.config, self.media)

    def test_a_tape_created_with_no_size_gets_its_density_s_capacity(self):
        with mock.patch.object(media, 'create', return_value=ok()) as made:
            result = self.service.create(10, 'E01099L8', slot=25)
        self.assertTrue(result.success, result.errors)
        self.assertEqual(made.call_args[1]['density'], 'LTO8')
        self.assertEqual(made.call_args[1]['size_mb'], native_size_mb('LTO8'))

    def test_an_explicit_size_is_still_honoured(self):
        """The forms and --size-mb still override it; the default was the
        only thing wrong."""
        with mock.patch.object(media, 'create', return_value=ok()) as made:
            self.service.create(10, 'E01098L8', slot=26, size_mb=4096)
        self.assertEqual(made.call_args[1]['size_mb'], 4096)

    def test_each_tape_of_a_mixed_library_gets_its_own_capacity(self):
        """create_missing resolves the density per barcode, so the capacity
        has to follow per barcode: a library holding LTO-8 and LTO-6 gets
        12 TB cartridges and 2.5 TB ones, as a rack would."""
        with mock.patch.object(media, 'exists', return_value=False), \
             mock.patch.object(media, 'create', return_value=ok()) as made:
            self.service.create_missing(10)

        by_barcode = {call[1]['density']: call[1]['size_mb']
                      for call in made.call_args_list}
        self.assertGreater(len(by_barcode), 1, 'the fixture is not mixed')
        for density, size_mb in by_barcode.items():
            with self.subTest(density=density):
                self.assertEqual(size_mb, native_size_mb(density))


class NoFrontEndCarriesTheNumberTests(SimpleTestCase):
    """The guard. Six places held 500000 or 500; none may again."""

    #: Where a tape size could be written out again. The JS is included: it
    #: had the 1 GB fallback, and now takes it from the service through
    #: media_info.
    WATCHED = (
        'mhvtl_cli/commands/tape.py',
        'apps/libraries/tape_operations_views.py',
        'apps/libraries/templates/libraries/operator/create_tape.html',
        'apps/libraries/templates/libraries/operator/create_tapes_bulk.html',
        'static/js/tape-media.js',
        'apps/libraries/services/libraries/workflow.py',
    )

    @staticmethod
    def code_of(text: str) -> str:
        """The file with its comments taken out.

        A comment is allowed to name the old numbers - several do, because
        what they were is the reason this guard exists. Django's
        ``{% comment %}`` blocks and ``{# #}`` count, as do ``<!-- -->``,
        ``//`` and ``#``: the first version of this test failed on its own
        explanation.
        """
        text = re.sub(r'\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}', '',
                      text, flags=re.S)
        text = re.sub(r'\{#.*?#\}', '', text, flags=re.S)
        text = re.sub(r'<!--.*?-->', '', text, flags=re.S)
        kept = []
        for line in text.splitlines():
            if line.lstrip().startswith(('#', '//', '*')):
                continue
            kept.append(re.sub(r'(#|//).*$', '', line))
        return '\n'.join(kept)

    def test_no_caller_writes_a_capacity_of_its_own(self):
        for name in self.WATCHED:
            code = self.code_of((GUI / name).read_text())
            for line in code.splitlines():
                with self.subTest(file=name, line=line.strip()[:60]):
                    self.assertNotRegex(line, r'\b500000\b')
                    self.assertNotRegex(line, r'size_mb\s*[=:]\s*\d')

    def test_the_capacity_table_is_the_only_place_with_capacities(self):
        """personalities.NATIVE_CAPACITY_GB, and nothing else."""
        rule = (GUI / 'apps/libraries/services/tapes/service.py').read_text()
        self.assertIn('NATIVE_CAPACITY_GB', rule)
        self.assertNotIn('DEFAULT_SIZE_MB', rule)

    def test_the_workflow_keeps_no_tape_size(self):
        """A library's own tapes were the 500 MB ones, which is what made the
        disagreement visible."""
        workflow = (GUI
                    / 'apps/libraries/services/libraries/workflow.py').read_text()
        self.assertNotIn('DEFAULT_TAPE_SIZE_MB = ', workflow)

    def test_the_script_takes_the_fallback_from_the_service(self):
        script = (GUI / 'static/js/tape-media.js').read_text()
        self.assertIn('info.unknown_size_mb', script)
        self.assertNotRegex(
            re.sub(r'//.*', '', script), r'UNKNOWN_SIZE_MB\s*=\s*\d')
