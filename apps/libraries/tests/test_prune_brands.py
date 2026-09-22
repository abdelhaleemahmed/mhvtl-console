"""manage.py prune_brands: one brand row per vendor.

The table had grown a row per spelling - Dell and DELL, Spectra and SPECTRA,
Sony and SONY, Quantum and QUANTUM, Overland and OVERLAND - because the pages
and the sync created them with different case, plus a TestVendor left by a
test run. The setup pages read the profiles now, so these rows only matter to
the custom-setup form, but a row that is deleted while a library points at it
takes the library with it (both foreign keys cascade), so the merge moves
models and libraries first.
"""
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from apps.libraries.models import Drive, Library, LibraryBrand, LibraryModel


def brand(name):
    return LibraryBrand.objects.create(name=name, display_name=name)


def model(brand_row, name):
    return LibraryModel.objects.create(brand=brand_row, name=name,
                                       product_identification=name)


def library(library_id, brand_row, model_row, active=True):
    return Library.objects.create(
        library_id=library_id, channel=0, target=library_id, lun=0,
        brand=brand_row, model=model_row, vendor_identification=brand_row.name,
        product_identification=model_row.name, is_active=active)


def run(*arguments):
    out = StringIO()
    call_command('prune_brands', *arguments, stdout=out)
    return out.getvalue()


class PruneBrandsTests(TestCase):

    def setUp(self):
        self.upper = brand('SPECTRA')
        self.mixed = brand('Spectra')
        self.upper_model = model(self.upper, 'PYTHON')
        self.mixed_model = model(self.mixed, 'GATOR')

    def test_a_dry_run_changes_nothing(self):
        output = run()
        self.assertIn("merge brand 'Spectra'", output)
        self.assertIn('Dry run', output)
        self.assertEqual(LibraryBrand.objects.count(), 2)

    def test_it_keeps_the_row_the_sync_writes(self):
        run('--apply')
        self.assertEqual([b.name for b in LibraryBrand.objects.all()], ['SPECTRA'])

    def test_models_move_across(self):
        run('--apply')
        self.assertEqual(sorted(m.name for m in self.upper.models.all()),
                         ['GATOR', 'PYTHON'])

    def test_a_library_on_the_duplicate_survives_and_moves(self):
        """Both foreign keys cascade: deleting the row without moving the
        library first would delete the library."""
        moved = library(40, self.mixed, self.mixed_model)
        Drive.objects.create(library=moved, drive_id=41, channel=0, target=41, lun=0)
        run('--apply')
        moved.refresh_from_db()
        self.assertEqual(moved.brand, self.upper)
        self.assertEqual(moved.model.name, 'GATOR')
        self.assertEqual(moved.drives.count(), 1)

    def test_a_model_name_both_rows_have_is_folded_into_one(self):
        duplicate = model(self.mixed, 'PYTHON')
        on_duplicate = library(50, self.mixed, duplicate)
        run('--apply')
        on_duplicate.refresh_from_db()
        self.assertEqual(on_duplicate.model, self.upper_model)
        self.assertEqual(LibraryModel.objects.filter(name='PYTHON').count(), 1)

    def test_a_vendor_no_profile_names_is_removed(self):
        brand('TestVendor')
        run('--apply')
        self.assertFalse(LibraryBrand.objects.filter(name='TestVendor').exists())

    def test_but_not_while_a_library_points_at_it(self):
        stray = brand('TestVendor')
        kept = library(60, stray, model(stray, 'T-1000'))
        output = run('--apply')
        self.assertIn("keeping 'TestVendor'", output)
        self.assertTrue(Library.objects.filter(pk=kept.pk).exists())

    def test_running_it_twice_is_a_no_op(self):
        run('--apply')
        self.assertIn('Nothing to do', run())

    def test_a_tidy_table_is_left_alone(self):
        LibraryBrand.objects.all().delete()
        keep = brand('IBM')
        model(keep, '03584L32')
        self.assertIn('Nothing to do', run())
        self.assertEqual(LibraryBrand.objects.count(), 1)
