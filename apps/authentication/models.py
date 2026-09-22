# apps/authentication/models.py
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AbstractUser
from django.db import models

#: Password the account is seeded with, matching the original PHP console.
#: The GUI warns on every page until it is changed.
DEFAULT_PASSWORD = 'mhvtl'

#: Username of the single shared account. There is no user management UI: the
#: account is created by a migration, and its password is changed from the UI or
#: with `manage.py changepassword <username>`.
DEFAULT_USERNAME = 'admin'


def gui_username():
    """Username the password-only login form authenticates against.

    The login form asks for a password and no username, so the account has to be
    worked out here:

    1. ``settings.MHVTL_GUI_USERNAME`` when that account exists - set it when an
       install has several accounts and you want a specific one;
    2. otherwise the only active account, so an install that predates this and
       named its account something else keeps working;
    3. otherwise the seeded default, which simply fails to authenticate and
       leaves the operator at the login page.
    """
    configured = getattr(settings, 'MHVTL_GUI_USERNAME', DEFAULT_USERNAME)

    user_model = get_user_model()
    if user_model.objects.filter(username=configured, is_active=True).exists():
        return configured

    active = list(user_model.objects.filter(is_active=True)[:2])
    if len(active) == 1:
        return active[0].username

    return configured


class User(AbstractUser):
    """Extended user model for MHVTL"""
    
    ROLE_CHOICES = [
        ('admin', 'Administrator'),
        ('operator', 'Operator'),
        ('viewer', 'Viewer'),
    ]
    
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='operator')
    last_login_ip = models.GenericIPAddressField(null=True, blank=True)
    
    def can_manage_libraries(self):
        return self.role in ['admin', 'operator']
    
    def can_control_services(self):
        return self.role == 'admin'
