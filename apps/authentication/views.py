from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views import View

from .models import DEFAULT_PASSWORD, gui_username


class LoginView(View):
    """Password-only login for the single shared GUI account.

    The form asks for a password and nothing else, as the original PHP console
    did. The password is checked against the hashed one in the database rather
    than a literal in this file, so it can be changed from the UI and reset with
    `manage.py changepassword`.
    """
    template_name = 'authentication/login.html'

    def get(self, request):
        if request.user.is_authenticated:
            return redirect('authentication:dashboard')
        return render(request, self.template_name)

    def post(self, request):
        password = request.POST.get('pswd', '')
        user = authenticate(request, username=gui_username(), password=password)

        if user is None:
            messages.error(request, 'Wrong Password, Please Try Again')
            return render(request, self.template_name, status=401)

        # login() cycles the session key, so a session id captured before
        # logging in cannot be reused afterwards.
        login(request, user)

        # Kept so the per-view session checks still scattered through the
        # libraries app keep working; they are redundant now that
        # LoginRequiredMiddleware is in place and can be removed.
        request.session['mhvtl_logged_in'] = True
        request.session['login_time'] = str(timezone.now())
        request.session['using_default_password'] = user.check_password(DEFAULT_PASSWORD)

        forwarded = request.headers.get('X-Forwarded-For', '')
        user.last_login_ip = (forwarded.split(',')[0].strip() if forwarded
                              else request.META.get('REMOTE_ADDR'))
        user.save(update_fields=['last_login_ip'])

        return redirect(request.GET.get('next') or 'authentication:dashboard')


class LogoutView(View):
    """Clear the session. Accepts GET because the nav links are plain links."""

    def get(self, request):
        return self._logout(request)

    def post(self, request):
        return self._logout(request)

    @staticmethod
    def _logout(request):
        logout(request)
        request.session.flush()
        return redirect('authentication:login')


class ChangePasswordView(View):
    """Change the shared account's password from the UI."""
    template_name = 'authentication/change_password.html'

    def get(self, request):
        return render(request, self.template_name,
                      {'form': PasswordChangeForm(request.user)})

    def post(self, request):
        form = PasswordChangeForm(request.user, request.POST)
        if not form.is_valid():
            return render(request, self.template_name, {'form': form}, status=400)

        user = form.save()
        # Changing the password rotates the session auth hash, which would log
        # this session out immediately without this call.
        update_session_auth_hash(request, user)
        request.session['using_default_password'] = user.check_password(DEFAULT_PASSWORD)

        messages.success(request, 'Password changed.')
        return redirect('authentication:dashboard')


class DashboardView(View):
    """Main dashboard view after login."""
    template_name = 'authentication/dashboard.html'

    def get(self, request):
        context = {
            'title': 'MHVTL Web Console',
            'system_name': 'Linux Virtual Tape Library System',
        }
        return render(request, self.template_name, context)
