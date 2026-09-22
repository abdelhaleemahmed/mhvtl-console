"""
Django management command: create_library

    python manage.py create_library --auto-id --vendor STK --product L700 --drives 4
    python manage.py create_library --library-id 40 --vendor IBM --dry-run

Kept for the scripts that call it; the work is `mhvtl library create`
(mhvtl_cli/commands/library.py), which runs the same validate, write, start,
verify and make-the-tapes sequence as the web form. This command used to
carry its own copy of that sequence, built on an adapter, and its --dry-run
called a method the adapter did not have.

--vendor names the vendor profile (STK, IBM, HP, ...), --product the library
model. --no-sync is accepted and ignored: the database is synced from
device.conf when the library pages load.
"""
from django.core.management.base import BaseCommand, CommandError

from mhvtl_cli import main as cli


class Command(BaseCommand):
    help = 'Create an MHVTL library (forwards to `mhvtl library create`)'

    def add_arguments(self, parser):
        ids = parser.add_mutually_exclusive_group()
        ids.add_argument('--library-id', type=int, help='library id to create')
        ids.add_argument('--auto-id', action='store_true',
                         help='use the next free library id (the default)')
        parser.add_argument('--vendor', default='STK',
                            help='vendor profile (default: STK)')
        parser.add_argument('--product', default=None,
                            help="library model (default: the profile's)")
        parser.add_argument('--serial', help='unit serial number')
        parser.add_argument('--drives', type=int, default=4,
                            help='number of drives (default: 4)')
        parser.add_argument('--no-restart', action='store_true',
                            help='write the configuration but do not start the daemons')
        parser.add_argument('--dry-run', action='store_true',
                            help='print the device.conf text and stop')
        parser.add_argument('--no-sync', action='store_true', help='ignored')
        parser.add_argument('--restart-services', action='store_true',
                            help='ignored; starting is the default')
        parser.add_argument('--sync-discovery', action='store_true', help='ignored')

    def handle(self, *args, **options):
        argv = ['library', 'create', '--profile', options['vendor'].upper(),
                '--drives', str(options['drives'])]
        if options['library_id'] is not None:
            argv += ['--id', str(options['library_id'])]
        if options['product']:
            argv += ['--model', options['product']]
        if options['serial']:
            argv += ['--serial', options['serial']]
        if options['no_restart']:
            argv.append('--no-start')
        if options['dry_run']:
            argv.append('--dry-run')

        code = cli.main(argv)
        if code != 0:
            raise CommandError(f'mhvtl {" ".join(argv)} exited with {code}')
