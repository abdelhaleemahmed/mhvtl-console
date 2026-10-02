"""The packaged sudoers rules must allow every command the code runs with sudo.

The installed GUI runs as mhvtl-gui with these rules and nothing else. A command
missing from them works on the development server - which runs as a user with
full sudo - and fails only once installed: Attach on this host (iscsiadm) and
library creation (systemctl enable) both did.

TWO FILES, because two packages grant rights to the same account:

    packaging/rpm/mhvtl-gui.sudoers          mhvtl-gui itself
    packaging/ltfs-rpm/SOURCES/ltfs.sudoers  ltfs-mhvtl-config, for LTFS

Both are in this repository. The second used to be read from a sibling tree
outside it, which meant these tests passed on one machine and failed for
everyone who cloned the repo - CI found that, three failures, after v3.0.0
went out.

A command granted in either is allowed on an installed host, so both are read
here. Keeping them apart is deliberate: installing LTFS support is a separate
decision from installing the console, and the LTFS rules are narrower than a
line in the main file would be - fusermount is restricted to the mount tree, and
mkltfs is not granted at all.
"""
import re
from pathlib import Path

from django.test import SimpleTestCase

GUI = Path(__file__).resolve().parents[3]
SUDOERS = GUI / 'packaging' / 'rpm' / 'mhvtl-gui.sudoers'
LTFS_SUDOERS = GUI / 'packaging' / 'ltfs-rpm' / 'SOURCES' / 'ltfs.sudoers'
SUDOERS_FILES = [path for path in (SUDOERS, LTFS_SUDOERS) if path.exists()]
SOURCES = [p for p in (GUI / 'apps').rglob('*.py') if '/tests/' not in str(p)] + \
          [p for p in (GUI / 'mhvtl_cli').rglob('*.py') if '/tests/' not in str(p)]

#: shell.sudo(['cmd', ...]) and shell.sudo([*node ...]) where node = ['cmd', ...]
LITERAL = re.compile(r"sudo\(\[\s*'([\w./-]+)'")
LIST = re.compile(r"=\s*\[\s*'(iscsiadm|targetcli)'")
#: units._control('enable', unit) and friends
ACTION = re.compile(r"_control\('([\w-]+)'")


def allowed():
    """Every command rule from every packaged sudoers file.

    A file may carry several NOPASSWD: blocks - ltfs.sudoers has three, one per
    kind of operation - so each is taken in turn rather than only the first.
    """
    rules = []
    for path in SUDOERS_FILES:
        text = path.read_text().replace('\\\n', ' ')
        for line in text.splitlines():
            if 'NOPASSWD:' not in line:
                continue
            for rule in line.split('NOPASSWD:', 1)[1].split(','):
                rule = rule.strip()
                if rule:
                    rules.append(rule)
    return rules


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
        searched = ', '.join(path.name for path in SUDOERS_FILES)
        self.assertEqual(missing, [], f'not in {searched}: {missing}')

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

    def test_both_packaged_sudoers_files_are_present(self):
        """If one goes missing the test above would quietly pass by not
        looking for its commands at all."""
        self.assertIn(SUDOERS, SUDOERS_FILES)
        self.assertIn(LTFS_SUDOERS, SUDOERS_FILES,
                      'packaging/ltfs-rpm/SOURCES/ltfs.sudoers is missing')

    def test_mkltfs_is_not_granted_to_the_service_account(self):
        """It partitions and erases a cartridge. A human with root can run it;
        the console must not be able to."""
        self.assertEqual([r for r in allowed() if 'mkltfs' in r], [])

    def test_fusermount_is_restricted_to_the_ltfs_mount_tree(self):
        rules = [r for r in allowed() if 'fusermount' in r]
        self.assertTrue(rules, 'fusermount is not granted anywhere')
        for rule in rules:
            self.assertIn('/var/lib/ltfs/mnt/', rule,
                          f'fusermount rule is not scoped to the mount tree: {rule}')
