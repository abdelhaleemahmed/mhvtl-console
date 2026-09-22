"""
Django management command: gui_session

    python manage.py gui_session

Prints a session key for the shared GUI account, for driving the pages from a
script (tests/e2e/gui_e2e.py) without putting the password in a command line.
It needs local access to the database, which is the same access that could
change the password anyway.

    --user NAME   a different account (default: the GUI account)
    --minutes N   how long the session lasts (default 60)
"""
from django.conf import settings
from django.contrib.auth import (BACKEND_SESSION_KEY, HASH_SESSION_KEY,
                                 SESSION_KEY, get_user_model)
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.authentication.models import gui_username


class Command(BaseCommand):
    help = 'Print a session key for the GUI account (local testing)'

    def add_arguments(self, parser):
        parser.add_argument('--user', default=None)
        parser.add_argument('--minutes', type=int, default=60)

    def handle(self, *args, **options):
        from django.contrib.sessions.backends.db import SessionStore

        username = options['user'] or gui_username()
        user = get_user_model().objects.filter(username=username).first()
        if user is None:
            user = get_user_model().objects.filter(is_active=True).order_by('id').first()
        if user is None:
            raise CommandError('no user to make a session for')

        backends = getattr(settings, 'AUTHENTICATION_BACKENDS', None)
        session = SessionStore()
        session[SESSION_KEY] = str(user.pk)
        session[BACKEND_SESSION_KEY] = (backends[0] if backends
                                        else 'django.contrib.auth.backends.ModelBackend')
        session[HASH_SESSION_KEY] = user.get_session_auth_hash()
        session['mhvtl_logged_in'] = True
        session['login_time'] = str(timezone.now())
        session.set_expiry(options['minutes'] * 60)
        session.create()

        self.stdout.write(session.session_key)
