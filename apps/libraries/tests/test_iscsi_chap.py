"""CHAP authentication on iSCSI targets: per ACL, target-wide, and Attach.

Everything that would reach targetcli or iscsiadm is mocked.
"""
import json
from unittest import mock

from django.test import SimpleTestCase
from django.urls import reverse

from apps.libraries.services.core import CommandResult, success_result
from apps.libraries.services.iscsi import initiator, parsing, targetcli
from apps.libraries.services.iscsi.service import IscsiService
from apps.libraries.tests.base import LibraryTestBase

IQN = 'iqn.2026-09.com.example:tapevault'
HOST = 'iqn.1994-05.com.redhat:671eac9ac9e6'
OK = CommandResult([], 0, '', '')


def config(*, acl_chap=None, tpg_chap=None, required=True, open_to_all=False):
    acl = {'node_wwn': HOST, 'mapped_luns': []}
    acl.update(acl_chap or {})
    tpg = {'tag': 1, 'luns': [], 'portals': [], 'node_acls': [acl],
           'attributes': {'authentication': int(required),
                          'generate_node_acls': int(open_to_all)}}
    tpg.update(tpg_chap or {})
    return {'targets': [{'fabric': 'iscsi', 'wwn': IQN, 'tpgs': [tpg]}], 'storage_objects': []}


class ValidationTests(SimpleTestCase):

    def test_a_good_pair_passes(self):
        targetcli.validate_chap('backupserver', 'Secret-12345', 'target', 'Other-secret1')

    def test_the_password_must_be_12_to_16_characters(self):
        for bad in ('short', 'x' * 17):
            with self.assertRaises(targetcli.InvalidChap):
                targetcli.validate_chap('user', bad)

    def test_characters_targetcli_would_split_are_refused(self):
        for bad in ('has space 1234', 'quote"12345678', 'equals=1234567'):
            with self.assertRaises(targetcli.InvalidChap):
                targetcli.validate_chap('user', bad)

    def test_mutual_needs_both_halves_and_a_different_password(self):
        with self.assertRaises(targetcli.InvalidChap):
            targetcli.validate_chap('user', 'Secret-12345', 'target', '')
        with self.assertRaises(targetcli.InvalidChap):
            targetcli.validate_chap('user', 'Secret-12345', 'target', 'Secret-12345')


class TargetcliTests(SimpleTestCase):

    def test_acl_credentials_go_to_that_acl(self):
        with mock.patch.object(targetcli, 'run', return_value=OK) as run:
            targetcli.set_auth(IQN, initiator=HOST, userid='backupserver',
                               password='Secret-12345')
        self.assertEqual(run.call_args.args[0], [
            f'/iscsi/{IQN}/tpg1/acls/{HOST}', 'set', 'auth', 'userid=backupserver',
            'password=Secret-12345', "mutual_userid=''", "mutual_password=''"])

    def test_target_wide_credentials_go_to_the_tpg(self):
        with mock.patch.object(targetcli, 'run', return_value=OK) as run:
            targetcli.set_auth(IQN, userid='anyone', password='Secret-12345')
        self.assertEqual(run.call_args.args[0][0], f'/iscsi/{IQN}/tpg1')

    def test_empty_values_are_written_as_quoted_empties(self):
        """targetcli reads a bare 'userid= password=' as userid='password='."""
        with mock.patch.object(targetcli, 'run', return_value=OK) as run:
            targetcli.set_auth(IQN, initiator=HOST)
        self.assertEqual(run.call_args.args[0][3:], ["userid=''", "password=''",
                                                     "mutual_userid=''", "mutual_password=''"])


class ParsingTests(SimpleTestCase):
    """saveconfig.json names the fields chap_*; the parser read userid and
    never found anything, so every ACL showed no authentication."""

    def test_acl_and_target_credentials_are_read_and_masked(self):
        cfg = config(acl_chap={'chap_userid': 'backupserver', 'chap_password': 'Secret-12345',
                               'chap_mutual_userid': 'target',
                               'chap_mutual_password': 'Other-secret1'},
                     tpg_chap={'chap_userid': 'anyone', 'chap_password': 'Secret-99999'})
        target = parsing.targets_from_config(cfg)[0].to_dict()
        tpg = target['tpgs'][0]
        acl = tpg['acls'][0]
        self.assertEqual((acl['userid'], acl['password'], acl['mutual_userid']),
                         ('backupserver', '***', 'target'))
        self.assertEqual((tpg['chap_userid'], tpg['chap_password'], tpg['authentication']),
                         ('anyone', '***', True))
        self.assertNotIn('Secret-12345', json.dumps(target))


class ServiceTests(SimpleTestCase):

    def test_set_chap_sets_credentials_then_requires_authentication(self):
        with mock.patch.object(targetcli, 'run', return_value=OK) as run, \
             mock.patch.object(targetcli, 'save_config', return_value=OK):
            result = IscsiService().set_chap(IQN, 'backupserver', 'Secret-12345',
                                             initiator=HOST)
        self.assertTrue(result.success, result.errors)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0][1:3], ['set', 'auth'])
        self.assertEqual(commands[1][1:], ['set', 'attribute', 'authentication=1'])
        self.assertNotIn('Secret-12345', result.message)

    def test_a_bad_password_is_refused_before_targetcli(self):
        with mock.patch.object(targetcli, 'run') as run:
            result = IscsiService().set_chap(IQN, 'user', 'short', initiator=HOST)
        self.assertFalse(result.success)
        run.assert_not_called()

    def test_clearing_the_last_credentials_stops_requiring_authentication(self):
        with mock.patch.object(targetcli, 'run', return_value=OK) as run, \
             mock.patch.object(targetcli, 'save_config', return_value=OK), \
             mock.patch.object(IscsiService, 'chap_in_use', return_value=False):
            result = IscsiService().clear_chap(IQN, initiator=HOST)
        self.assertTrue(result.success)
        self.assertEqual(run.call_args_list[-1].args[0][1:],
                         ['set', 'attribute', 'authentication=0'])

    def test_clearing_one_acl_keeps_it_required_while_others_have_credentials(self):
        with mock.patch.object(targetcli, 'run', return_value=OK) as run, \
             mock.patch.object(targetcli, 'save_config', return_value=OK), \
             mock.patch.object(IscsiService, 'chap_in_use', return_value=True):
            IscsiService().clear_chap(IQN, initiator=HOST)
        self.assertEqual(len(run.call_args_list), 1)


class AttachTests(SimpleTestCase):

    def test_this_hosts_acl_credentials_are_used(self):
        cfg = config(acl_chap={'chap_userid': 'backupserver', 'chap_password': 'Secret-12345',
                               'chap_mutual_userid': 'target',
                               'chap_mutual_password': 'Other-secret1'})
        chap = initiator.chap_for(IQN, HOST, cfg)
        self.assertEqual((chap['userid'], chap['mutual_userid']), ('backupserver', 'target'))

    def test_an_initiator_with_its_own_acl_does_not_get_the_target_wide_credentials(self):
        """LIO uses the ACL when there is one; offering the target-wide
        credentials instead failed the login."""
        cfg = config(tpg_chap={'chap_userid': 'anyone', 'chap_password': 'Secret-99999'},
                     open_to_all=True)
        chap = initiator.chap_for(IQN, HOST, cfg)
        self.assertEqual(chap, {'required': True})

    def test_target_wide_credentials_for_an_initiator_without_an_acl(self):
        cfg = config(tpg_chap={'chap_userid': 'anyone', 'chap_password': 'Secret-99999'},
                     open_to_all=True)
        cfg['targets'][0]['tpgs'][0]['node_acls'] = []
        self.assertEqual(initiator.chap_for(IQN, HOST, cfg)['userid'], 'anyone')

    def test_no_chap_when_not_required(self):
        self.assertIsNone(initiator.chap_for(IQN, HOST, config(required=False)))

    def test_attach_puts_the_credentials_in_the_node_record_before_login(self):
        ran = []
        chap = {'required': True, 'userid': 'backupserver', 'password': 'Secret-12345',
                'mutual_userid': 'target', 'mutual_password': 'Other-secret1'}
        with mock.patch.object(initiator.shell, 'sudo',
                               side_effect=lambda argv, **kw: ran.append(' '.join(argv)) or OK), \
             mock.patch.object(initiator, 'session_for', return_value=None), \
             mock.patch.object(initiator, 'portal_for', return_value='127.0.0.1:3261'), \
             mock.patch.object(initiator, 'chap_for', return_value=chap), \
             mock.patch.object(initiator, 'exported_as', return_value={}), \
             mock.patch.object(initiator, 'attached_devices', return_value=[]):
            result = initiator.attach(IQN, wait=0)
        self.assertTrue(result.success)
        self.assertIn('mutual CHAP', result.message)
        login = next(i for i, line in enumerate(ran) if line.endswith('--login'))
        for setting in ('node.session.auth.authmethod -v CHAP',
                        'node.session.auth.username -v backupserver',
                        'node.session.auth.password -v Secret-12345',
                        'node.session.auth.username_in -v target'):
            where = next(i for i, line in enumerate(ran) if setting in line)
            self.assertLess(where, login, setting)

    def test_a_target_requiring_chap_without_credentials_for_this_host_is_refused(self):
        with mock.patch.object(initiator.shell, 'sudo', return_value=OK), \
             mock.patch.object(initiator, 'session_for', return_value=None), \
             mock.patch.object(initiator, 'portal_for', return_value='127.0.0.1:3261'), \
             mock.patch.object(initiator, 'chap_for', return_value={'required': True}):
            result = initiator.attach(IQN, wait=0)
        self.assertFalse(result.success)
        self.assertIn('requires CHAP', result.message)


class ChapPageTests(LibraryTestBase):

    def setUp(self):
        super().setUp()
        self.login_as_user()
        session = self.client.session
        session['mhvtl_logged_in'] = True
        session.save()

    def post(self, body):
        return self.client.post(reverse('libraries:iscsi_chap_ajax'), json.dumps(body),
                                content_type='application/json')

    def test_set_goes_to_set_chap_and_the_password_is_not_echoed(self):
        with mock.patch.object(IscsiService, 'set_chap',
                               return_value=success_result('CHAP required')) as set_chap:
            response = self.post({'iqn': IQN, 'initiator': HOST, 'action': 'set',
                                  'userid': 'backupserver', 'password': 'Secret-12345'})
        set_chap.assert_called_once_with(IQN, 'backupserver', 'Secret-12345', initiator=HOST,
                                         mutual_userid='', mutual_password='')
        self.assertNotIn('Secret-12345', response.content.decode())

    def test_clear_target_wide(self):
        with mock.patch.object(IscsiService, 'clear_chap',
                               return_value=success_result('removed')) as clear:
            self.post({'iqn': IQN, 'initiator': '', 'action': 'clear'})
        clear.assert_called_once_with(IQN, initiator=None)


class TargetPageTests(ChapPageTests):
    """The target page shows who has CHAP - never a password - and the buttons."""

    def test_the_page_shows_chap_state_without_passwords(self):
        from apps.libraries import iscsi_views
        cfg = config(acl_chap={'chap_userid': 'backupserver', 'chap_password': 'Secret-12345',
                               'chap_mutual_userid': 'tapevault',
                               'chap_mutual_password': 'Other-secret1'},
                     tpg_chap={'chap_userid': 'anyone', 'chap_password': 'Secret-99999'})
        target = parsing.targets_from_config(cfg)[0].to_dict()
        with mock.patch.object(iscsi_views, '_target', return_value=target), \
             mock.patch.object(iscsi_views, '_backstores', return_value=[]), \
             mock.patch.object(iscsi_views, '_attached_here',
                               return_value={'attached': False, 'devices': []}), \
             mock.patch.object(iscsi_views, '_local_initiator', return_value=HOST):
            page = self.client.get(reverse('libraries:iscsi_target_detail', args=[IQN]))
        body = page.content.decode()
        self.assertEqual(page.status_code, 200)
        for text in ('Mutual CHAP', 'backupserver', 'id="set-chap-target"',
                     'id="clear-chap-1"', 'required', 'anyone'):
            self.assertIn(text, body)
        for secret in ('Secret-12345', 'Other-secret1', 'Secret-99999'):
            self.assertNotIn(secret, body)
