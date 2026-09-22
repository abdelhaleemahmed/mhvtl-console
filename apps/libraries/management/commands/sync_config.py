"""
Management command to sync MHVTL device.conf into the Django database.

Creates missing libraries and drives, activates/deactivates as needed.
Can be run from CLI, cron, RPM %post, or called from views.

Usage:
    python manage.py sync_config
    python manage.py sync_config --quiet
"""
from django.core.management.base import BaseCommand

from apps.libraries.services.sync.service import sync_mhvtl_to_django


class Command(BaseCommand):
    help = 'Sync MHVTL device.conf libraries and drives into the Django database'

    def add_arguments(self, parser):
        parser.add_argument(
            '--quiet', '-q',
            action='store_true',
            help='Suppress output (for use in scripts)',
        )

    def handle(self, *args, **options):
        quiet = options['quiet']

        try:
            stats = sync_mhvtl_to_django()
        except Exception as e:
            self.stderr.write(self.style.ERROR(f'Sync failed: {e}'))
            return

        if quiet:
            return

        self.stdout.write(f"Libraries found in MHVTL: {stats['libraries_found']}")
        self.stdout.write(f"  New libraries created:  {stats['created']}")
        self.stdout.write(f"  Drives imported:        {stats['drives_imported']}")
        self.stdout.write(f"  Activated:              {stats['activated']}")
        self.stdout.write(f"  Deactivated:            {stats['deactivated']}")
        self.stdout.write(f"  Total DB libraries:     {stats['total_db']}")
        self.stdout.write(f"  Total DB drives:        {stats['total_drives']}")

        if stats['created'] > 0 or stats['activated'] > 0 or stats['deactivated'] > 0:
            self.stdout.write(self.style.SUCCESS('Sync completed with changes.'))
        else:
            self.stdout.write(self.style.SUCCESS('Sync completed — database is up to date.'))
