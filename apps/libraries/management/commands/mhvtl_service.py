"""
Django management command: mhvtl_service

    python manage.py mhvtl_service status
    python manage.py mhvtl_service restart --library-id 10
    python manage.py mhvtl_service start|stop
    python manage.py mhvtl_service reload            # systemctl daemon-reload

Kept for the scripts that call it; the work is `mhvtl service`
(mhvtl_cli/commands/service.py). This command used to read the status
through an adapter method that did not exist, so `status` always failed.
"""
from django.core.management.base import BaseCommand, CommandError

from mhvtl_cli import main as cli


class Command(BaseCommand):
    help = 'Manage the MHVTL services (forwards to `mhvtl service`)'

    def add_arguments(self, parser):
        parser.add_argument('action',
                            choices=['start', 'stop', 'restart', 'status', 'reload'])
        parser.add_argument('--library-id', type=int,
                            help='only this library, instead of the whole target')
        parser.add_argument('--target-only', action='store_true',
                            help='ignored; without --library-id the target is used')
        parser.add_argument('--force', action='store_true', help='ignored')

    def handle(self, *args, **options):
        action = options['action']
        if action == 'reload':
            from apps.libraries.services.console import units
            result = units.daemon_reload()
            if not result.ok:
                raise CommandError(f'systemctl daemon-reload failed: {result.output.strip()}')
            self.stdout.write('systemd configuration reloaded')
            return

        argv = ['service', action]
        if options['library_id'] is not None and action != 'status':
            argv += ['--library', str(options['library_id'])]
        code = cli.main(argv)
        if code != 0:
            raise CommandError(f'mhvtl {" ".join(argv)} exited with {code}')
