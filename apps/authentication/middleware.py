# apps/authentication/middleware.py
"""Site-wide login enforcement.

Every view in this project runs privileged operations behind sudo, so the
default is that you must be logged in. Views opt out by path here, not by
remembering to add a check - the old per-view `request.session['mhvtl_logged_in']`
checks were missing on several endpoints (library list, library status and the
iSCSI status API all answered anonymously).
"""
import re

from django.conf import settings
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import redirect
from django.urls import reverse

#: Paths reachable without a session, as regular expressions matched against
#: request.path. Keep this list short and specific.
EXEMPT_PATTERNS = [
    r'^/auth/login/?$',
    r'^/auth/logout/?$',
    r'^/admin/login/?$',       # Django admin has its own login form
    r'^/static/',
    r'^/health/?$',            # for monitoring; exposes no library data
]


class LoginRequiredMiddleware:
    """Require an authenticated session for everything except EXEMPT_PATTERNS."""

    def __init__(self, get_response):
        self.get_response = get_response
        extra = getattr(settings, 'LOGIN_EXEMPT_PATTERNS', [])
        self._exempt = [re.compile(p) for p in list(EXEMPT_PATTERNS) + list(extra)]

    def __call__(self, request):
        if self._is_exempt(request.path) or self._is_authenticated(request):
            # Make sure the csrftoken cookie is set on every page, so the
            # JavaScript in static/js/csrf.js always has a token to send. Most
            # pages here are standalone templates that render no form and so
            # would otherwise never trigger the cookie.
            get_token(request)
            return self.get_response(request)
        return self._deny(request)

    def _is_exempt(self, path):
        return any(pattern.match(path) for pattern in self._exempt)

    @staticmethod
    def _is_authenticated(request):
        user = getattr(request, 'user', None)
        return user is not None and user.is_authenticated

    @staticmethod
    def _wants_json(request):
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return True
        if '/ajax/' in request.path or request.path.startswith('/api/'):
            return True
        return request.headers.get('Accept', '').startswith('application/json')

    def _deny(self, request):
        """Answer a browser with a redirect and a script with a status code.

        An AJAX caller that follows a redirect gets the login page as a 200 and
        reports it as corrupt data, which is a confusing way to learn that the
        session expired.
        """
        if self._wants_json(request):
            return JsonResponse(
                {'success': False, 'error': 'Authentication required'},
                status=403,
            )
        login_url = reverse('authentication:login')
        return redirect(f'{login_url}?{REDIRECT_FIELD_NAME}={request.get_full_path()}')
