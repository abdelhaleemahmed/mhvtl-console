"""Create the shared GUI account.

Before this, the login view compared the submitted password against the literal
'mhvtl' and no account existed at all. Seeding it with the same password keeps
existing installs working unchanged, while the password now lives hashed in the
database where it can be changed.
"""
from django.contrib.auth.hashers import make_password
from django.db import migrations

DEFAULT_PASSWORD = 'mhvtl'
DEFAULT_USERNAME = 'admin'


def create_gui_account(apps, schema_editor):
    User = apps.get_model('authentication', 'User')
    if User.objects.exists():
        # An install that already has accounts keeps them; nothing to seed.
        return
    User.objects.create(
        username=DEFAULT_USERNAME,
        password=make_password(DEFAULT_PASSWORD),
        role='admin',
        is_staff=True,
        is_superuser=True,
        is_active=True,
    )


def remove_gui_account(apps, schema_editor):
    User = apps.get_model('authentication', 'User')
    User.objects.filter(username=DEFAULT_USERNAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('authentication', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(create_gui_account, remove_gui_account),
    ]
