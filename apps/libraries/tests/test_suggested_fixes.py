"""A refusal tells you what would have worked.

validation.validate already computes the valid values whenever it rejects
one - it has the vendor profile open - and puts them in
ValidationResult.suggested_fixes. Two callers then dropped them on the floor:

    LibraryService.validate   took errors, left suggested_fixes behind, so
                              `mhvtl library create` printed "Drive model
                              'T10000C' is not valid for IBM" and withheld the
                              list of models that are
    lifecycle.preview         put errors and warnings in its data and not the
                              fixes, so --dry-run had the same hole

lifecycle.create, next door to the first, had always sent both - which is how
the omission was spotted. These tests keep all three paths honest, because
the fix is one line in each and the regression would be invisible: the command
still works, still refuses, still says something true.
"""
from .base import SimpleTestCase, TestCase

import shutil
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / 'fixtures'

#: A real IBM library model with a drive from another vendor's catalogue.
#: Refused for two separate reasons, each with its own fix.
WRONG_DRIVE = {'profile': 'IBM', 'library_id': 95, 'library_model': '03584L32',
               'drive_model': 'T10000C', 'num_drives': 2}


class ValidationCarriesFixesTests(SimpleTestCase):
    """The source of the fixes, which was never the problem."""

    def test_validate_computes_them(self):
        from apps.libraries.services.libraries import validation

        result = validation.validate(WRONG_DRIVE)
        self.assertFalse(result.is_valid)
        self.assertTrue(result.suggested_fixes,
                        'validation has the profile open and must say what is '
                        'valid')
        self.assertTrue(any('ULT3580-TD8' in fix
                            for fix in result.suggested_fixes))

    def test_no_fix_line_is_left_dangling(self):
        """An empty list must not produce "Drive 'X' supports:" and nothing.

        T10000C is not in IBM's catalogue at all, so its media list under that
        profile is empty. A fix line ending in a colon reads as a broken tool
        rather than a wrong command.
        """
        from apps.libraries.services.libraries import validation

        for fix in validation.validate(WRONG_DRIVE).suggested_fixes:
            self.assertFalse(fix.rstrip().endswith(':'),
                             f'fix line says nothing after the colon: {fix!r}')


class ServiceCarriesFixesTests(TestCase):
    """LibraryService.validate - where they were dropped."""

    def setUp(self):
        self.config = self.tmpdir()
        shutil.copy(FIXTURES / 'device.conf', self.config)

    def service(self):
        from apps.libraries.services.libraries import LibraryService
        return LibraryService(self.config)

    def test_a_refusal_includes_the_valid_values(self):
        result = self.service().validate(WRONG_DRIVE)
        self.assertFalse(result.success)
        joined = ' '.join(result.errors)
        self.assertIn("not valid for IBM", joined, 'the problem')
        self.assertIn('Valid drive models for IBM', joined, 'the answer')

    def test_a_valid_specification_carries_no_fixes(self):
        """Nothing to fix, nothing to print."""
        good = {'profile': 'IBM', 'library_id': 95, 'library_model': '03584L32',
                'drive_model': 'ULT3580-TD6', 'media_type': 'LTO6',
                'num_drives': 2}
        result = self.service().validate(good)
        self.assertTrue(result.success, result.errors)
        self.assertEqual(result.errors, [])


class PreviewCarriesFixesTests(TestCase):
    """lifecycle.preview - the --dry-run path, where they were also dropped."""

    def setUp(self):
        self.config = self.tmpdir()
        shutil.copy(FIXTURES / 'device.conf', self.config)

    def test_the_preview_reports_them(self):
        from apps.libraries.services.libraries import lifecycle

        result = lifecycle.preview(dict(WRONG_DRIVE), self.config)
        self.assertTrue(result.success, 'a preview succeeds even when the '
                                        'specification it previews would not')
        self.assertTrue(result.data['errors'])
        self.assertTrue(result.data['fixes'],
                        'the preview reported the problem and withheld the '
                        'answer until this was fixed')
        self.assertTrue(any('ULT3580-TD8' in fix
                            for fix in result.data['fixes']))

    def test_a_clean_preview_has_nothing_to_suggest(self):
        from apps.libraries.services.libraries import lifecycle

        good = {'profile': 'IBM', 'library_id': 95, 'library_model': '03584L32',
                'drive_model': 'ULT3580-TD6', 'media_type': 'LTO6',
                'num_drives': 2}
        result = lifecycle.preview(good, self.config)
        self.assertEqual(result.data['errors'], [])
        self.assertEqual(result.data['fixes'], [])


class WorkflowCarriesFixesTests(TestCase):
    """The path `mhvtl library create` actually takes.

    It goes through create_library_workflow, which reports the validate step
    and returns LibraryService.validate's errors - so the fix in the service
    is what makes the command line useful, and this is the test that says so.
    """

    def setUp(self):
        self.config = self.tmpdir()
        shutil.copy(FIXTURES / 'device.conf', self.config)

    def test_a_rejected_creation_says_what_would_work(self):
        from apps.libraries.services.libraries import create_library_workflow

        result = create_library_workflow(dict(WRONG_DRIVE),
                                         restart=False, create_media=False,
                                         config_directory=self.config)
        self.assertFalse(result.success)
        joined = ' '.join(result.errors)
        self.assertIn('Valid drive models for IBM', joined)
        self.assertIn('ULT3580-TD8', joined)

    def test_nothing_was_written(self):
        """A refusal that had created something would be worse than no fixes."""
        from apps.libraries.services.libraries import create_library_workflow
        from apps.libraries.services.config import device_conf

        before = (self.config / 'device.conf').read_text()
        create_library_workflow(dict(WRONG_DRIVE), restart=False,
                                create_media=False,
                                config_directory=self.config)
        after = (self.config / 'device.conf').read_text()
        self.assertEqual(after, before)
        self.assertNotIn(95, device_conf.parse(after).libraries)
