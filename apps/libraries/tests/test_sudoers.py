"""packaging/rpm/mhvtl-gui.sudoers must allow every command the code runs with sudo.

The installed GUI runs as mhvtl-gui with these rules and nothing else. A command
missing from them works on the development server - which runs as a user with
full sudo - and fails only once installed: Attach on this host (iscsiadm) and
library creation (systemctl enable) both did.
"""
import re
from pathlib import Path

from django.test import SimpleTestCase

GUI = Path(__file__).resolve().parents[3]
SUDOERS = GUI / 'packaging' / 'rpm' / 'mhvtl-gui.sudoers'
SOURCES = [p for p in (GUI / 'apps').rglob('*.py') if '/tests/' not in str(p)] + \
          [p for p in (GUI / 'mhvtl_cli').rglob('*.py') if '/tests/' not in str(p)]

#: shell.sudo(['cmd', ...]) and shell.sudo([*node ...]) where node = ['cmd', ...]
LITERAL = re.compile(r"sudo\(\[\s*'([\w./-]+)'")
LIST = re.compile(r"=\s*\[\s*'(iscsiadm|targetcli)'")
#: units._control('enable', unit) and friends
ACTION = re.compile(r"_control\('([\w-]+)'")


def allowed():
    text = SUDOERS.read_text().replace('\\\n', ' ')
    rules = text.split('NOPASSWD:', 1)[1]
    return [rule.strip() for rule in rules.split(',') if rule.strip()]


def commands_used():
    used = {'cat', 'tee'}                   # shell.sudo_cat and shell.sudo_tee
    for path in SOURCES:
        source = path.read_text()
        used.update(LITERAL.findall(source))
        if 'shell.sudo' in source or 'targetcli.run' in source:
            used.update(LIST.findall(source))
    used.add('targetcli')                   # targetcli.run() goes through shell.sudo
    return used


class SudoersTests(SimpleTestCase):

    def test_every_command_run_with_sudo_is_allowed(self):
        names = {Path(rule.split()[0]).name for rule in allowed()}
        missing = sorted(commands_used() - names)
        self.assertEqual(missing, [], f'not in {SUDOERS.name}: {missing}')

    def test_every_systemctl_action_on_mhvtl_units_is_allowed(self):
        source = (GUI / 'apps/libraries/services/console/units.py').read_text()
        actions = set(ACTION.findall(source)) | {'start', 'stop', 'restart', 'reset-failed'}
        rules = allowed()
        missing = sorted(action for action in actions
                         if f'/usr/bin/systemctl {action} vtl*' not in rules)
        self.assertEqual(missing, [], f'systemctl actions not allowed on vtl*: {missing}')
        self.assertIn('/usr/bin/systemctl daemon-reload', rules)

    def test_iscsiadm_is_allowed_for_attach_on_this_host(self):
        self.assertIn('/usr/sbin/iscsiadm', allowed())
