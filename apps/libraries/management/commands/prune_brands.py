"""
Django management command: prune_brands

    python manage.py prune_brands              # say what it would do
    python manage.py prune_brands --apply      # do it

The brand rows are display names for the custom-setup form; the vendor and
model lists an operator chooses from come from the profiles. The table had
grown one row per spelling - Dell and DELL, Spectra and SPECTRA, Sony and
SONY, Quantum and QUANTUM, Overland and OVERLAND - because the pages and the
sync created them with different case, and a TestVendor left by a test run.

This merges each set of rows that differ only in case into the one the sync
writes (upper case), moving their models and libraries across, and removes
rows that name no vendor the application knows and that no library uses.
Nothing that describes MHVTL is touched: device.conf is the authority, and
`manage.py sync_config` rebuilds these rows from it.
"""
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.libraries.models import Library, LibraryBrand, LibraryModel
from apps.libraries.services.profiles import PROFILES


class Command(BaseCommand):
    help = 'Merge case-duplicate library brands and remove unknown ones'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='make the changes (the default only reports)')

    def handle(self, *args, **options):
        apply_changes = options['apply']
        planned = []

        with transaction.atomic():
            planned += self._merge_duplicates(apply_changes)
            planned += self._drop_unknown(apply_changes)
            if not apply_changes:
                transaction.set_rollback(True)

        if not planned:
            self.stdout.write('Nothing to do: one row per vendor, all known.')
            return
        for line in planned:
            self.stdout.write(line)
        self.stdout.write(self.style.SUCCESS('Applied.') if apply_changes
                          else 'Dry run. Pass --apply to make these changes.')

    # -- merging --------------------------------------------------------

    def _merge_duplicates(self, apply_changes):
        """One row per vendor, keeping the upper-case one the sync writes."""
        by_name = defaultdict(list)
        for brand in LibraryBrand.objects.order_by('id'):
            by_name[brand.name.upper()].append(brand)

        report = []
        for name, brands in sorted(by_name.items()):
            if len(brands) == 1:
                continue
            keep = next((b for b in brands if b.name == name), brands[0])
            for other in brands:
                if other.pk == keep.pk:
                    continue
                models = LibraryModel.objects.filter(brand=other)
                libraries = Library.objects.filter(brand=other)
                report.append(
                    f'merge brand {other.name!r} (id {other.pk}) into {keep.name!r}: '
                    f'{models.count()} model(s), {libraries.count()} library(ies)')
                if apply_changes:
                    self._move_models(models, keep)
                    libraries.update(brand=keep)
                    other.refresh_from_db()
                    other.delete()
        return report

    def _move_models(self, models, keep):
        """Move models to `keep`, folding away a name it already has."""
        for model in models:
            existing = LibraryModel.objects.filter(brand=keep,
                                                   name=model.name).first()
            if existing is None:
                model.brand = keep
                model.save(update_fields=['brand'])
                continue
            Library.objects.filter(model=model).update(model=existing)
            model.delete()

    # -- unknown vendors -------------------------------------------------

    def _drop_unknown(self, apply_changes):
        """Rows that name no profile and that no library uses."""
        report = []
        for brand in LibraryBrand.objects.order_by('name'):
            if brand.name.upper() in PROFILES:
                continue
            libraries = Library.objects.filter(brand=brand)
            if libraries.exists():
                report.append(
                    f'keeping {brand.name!r}: not a vendor profile, but '
                    f'{libraries.count()} library(ies) still point at it')
                continue
            report.append(f'remove brand {brand.name!r} (id {brand.pk}): '
                          f'no profile, no libraries')
            if apply_changes:
                brand.delete()
        return report
