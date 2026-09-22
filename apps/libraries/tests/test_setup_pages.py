"""The setup pages: choosing a vendor, and choosing by tape.

The brand page listed LibraryBrand rows from the database. That table had
grown case-duplicated pairs (Dell and DELL, Spectra and SPECTRA, ...) and a
TestVendor left by a test run, so the page showed fifteen "vendors" for nine
profiles - and a profile with no row would not have appeared at all. The
profiles are the authority, so the page reads them.
"""
import json
import re
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.storage.fallback import FallbackStorage
from django.http import Http404
from django.test import RequestFactory, TestCase

from apps.libraries import views
from apps.libraries.models import LibraryBrand
from apps.libraries.services.profiles import PROFILES


def request_for(path='/'):
    request = RequestFactory().get(path)
    request.session = {'mhvtl_logged_in': True}
    request._messages = FallbackStorage(request)
    request.user = AnonymousUser()
    return request


class CatalogueTests(TestCase):

    def test_every_profile_is_offered_once(self):
        catalogue = views._brand_catalogue()
        self.assertEqual([entry['key'] for entry in catalogue], sorted(PROFILES))

    def test_each_entry_counts_its_models_drives_and_media(self):
        by_key = {entry['key']: entry for entry in views._brand_catalogue()}
        sony = by_key['SONY']
        self.assertEqual(sony['media'], ['AIT1', 'AIT2', 'AIT3', 'AIT4'])
        self.assertEqual(sony['families'], ['AIT'])
        self.assertIsNone(sony['newest_lto'])
        stk = by_key['STK']
        self.assertEqual(stk['newest_lto'], 'LTO-10')
        self.assertIn('T10000', stk['families'])
        self.assertEqual(stk['model_count'], len(PROFILES['STK'].library_models))

    def test_the_media_of_an_entry_are_what_its_drives_load(self):
        for entry in views._brand_catalogue():
            profile = PROFILES[entry['key']]
            for density in entry['media']:
                with self.subTest(vendor=entry['key'], density=density):
                    self.assertTrue(any(density in media for media
                                        in profile.drive_media_by_model.values()))

    def test_families_and_generations(self):
        self.assertEqual(views._media_family('LTO10'), 'LTO')
        self.assertEqual(views._media_family('T10KB'), 'T10000')
        self.assertEqual(views._media_family('E06'), '3592')
        self.assertEqual(views._media_family('SDLT600'), 'SDLT')
        self.assertEqual(views._newest_lto({'LTO8', 'LTO9'}), 'LTO-9')
        self.assertEqual(views._newest_lto({'LTO8', 'LTO10P'}), 'LTO-10')
        self.assertIsNone(views._newest_lto({'AIT4'}))


class BrandSelectionPageTests(TestCase):

    def page(self, query=''):
        response = views.BrandSelectionView.as_view()(request_for('/' + query))
        return response.content.decode()

    def test_it_lists_every_profile_and_no_database_row(self):
        LibraryBrand.objects.create(name='TestVendor', display_name='TestVendor')
        LibraryBrand.objects.create(name='Spectra', display_name='Spectra')
        page = self.page()
        names = re.findall(r'<div class="brand-name">([^<]+)</div>', page)
        self.assertEqual(sorted(n.strip() for n in names), sorted(PROFILES))
        self.assertNotIn('TestVendor', page)

    def test_each_card_carries_the_media_its_libraries_take(self):
        page = self.page()
        media = dict(re.findall(r'brand-option brand-(\w+)"\s+data-media="([^"]*)"', page))
        self.assertIn('LTO10', media['spectra'].split())
        self.assertIn('LTO10', media['overland'].split())
        self.assertIn('T10KC', media['stk'].split())
        self.assertNotIn('LTO9', media['hp'].split())       # MHVTL's HP stops at 8

    def test_the_media_filter_offers_every_creatable_density(self):
        from apps.libraries.services.profiles import personalities

        page = self.page()
        offered = set(re.findall(r'<option value="(\w+)"', page))
        for density in ('LTO9', 'LTO10', 'T10KC', 'AIT4', 'E07', 'SDLT600'):
            with self.subTest(density=density):
                self.assertIn(density, offered)
                self.assertIn(personalities.media_label(density), page)


class BrandConfigPageTests(TestCase):

    def view(self, brand_name):
        return views.BrandConfigView.as_view()(request_for(), brand_name=brand_name)

    def test_a_profile_without_a_database_row_still_opens(self):
        self.assertFalse(LibraryBrand.objects.exists())
        self.assertEqual(self.view('SPECTRA').status_code, 200)

    def test_the_name_is_matched_whatever_its_case(self):
        self.assertEqual(self.view('Spectra').status_code, 200)
        self.assertEqual(self.view('spectra').status_code, 200)

    def test_an_unknown_vendor_is_404_not_a_crash(self):
        with self.assertRaises(Http404):
            self.view('nosuch')

    def test_the_page_offers_lto10_where_the_profile_does(self):
        page = self.view('SPECTRA').content.decode()
        options = json.loads(re.search(r'const PROFILE = (\{.*?\});', page, re.S).group(1))
        self.assertIn('ULT3580-TDA', options['drive_models'])
        self.assertEqual(options['drive_media_mapping']['ULT3580-TDA'],
                         ['LTO10', 'LTO10P'])


class StartFromTheTapeTests(TestCase):
    """"I want an LTO-10 library" has to be answerable without knowing that
    LTO-10 means a TDA drive.

    The vendor page's filter carries the tape into the form (?media=), the
    form has the same choice on it, and both leave only the models and drives
    that take it.
    """

    def options(self, brand, query=''):
        response = views.BrandConfigView.as_view()(request_for('/' + query),
                                                   brand_name=brand)
        page = response.content.decode()
        return json.loads(re.search(r'const PROFILE = (\{.*?\});', page, re.S).group(1))

    def test_the_tape_arrives_from_the_vendor_page(self):
        self.assertEqual(self.options('ADIC', '?media=LTO10')['wanted_media'], 'LTO10')
        self.assertEqual(self.options('ADIC')['wanted_media'], '')

    def test_a_tape_this_vendor_does_not_take_is_ignored(self):
        """A stale link opens the page instead of failing."""
        self.assertEqual(self.options('SONY', '?media=LTO10')['wanted_media'], '')

    def test_the_page_carries_the_labels_the_chooser_shows(self):
        from apps.libraries.services.profiles import personalities

        options = self.options('STK')
        self.assertEqual(options['media_labels']['LTO9'],
                         personalities.media_label('LTO9'))
        self.assertEqual(set(options['media_labels']), set(options['media_types']))

    def test_the_form_has_the_chooser_and_filters_with_it(self):
        from django.template.loader import get_template

        source = get_template('libraries/brand_config.html').template.source
        self.assertIn('id="wantedMedia"', source)
        for helper in ('function modelTakes', 'function driveTakes',
                       'function bestDriveFor'):
            self.assertIn(helper, source)

    def test_the_vendor_cards_pass_the_tape_on(self):
        from django.template.loader import get_template

        source = get_template('libraries/brand_selection.html').template.source
        self.assertIn("link.searchParams.set('media', wanted)", source)

    #: A vendor whose drives only read a tape it offers. Creating such a
    #: library is legitimate - it restores and is never written to - and the
    #: form marks the medium read-only. Listed here so a new one cannot
    #: appear unnoticed: STK's oldest drive is an LTO-3, which reads LTO-1.
    READ_ONLY_ONLY = {('STK', 'LTO1')}

    def test_every_media_a_vendor_offers_is_loadable_and_mostly_writable(self):
        """What the chooser promises: pick a tape, get a drive that takes it -
        and, unless it is one of the known read-only pairs, writes it."""
        from apps.libraries.services.profiles import PROFILES, personalities

        read_only_only = set()
        for key, profile in PROFILES.items():
            for density in profile.library_supported_media:
                loaders = [drive for drive, media
                           in profile.drive_media_by_model.items()
                           if density in media]
                writers = [drive for drive in loaders
                           if density not in personalities.read_only_media(drive)]
                with self.subTest(vendor=key, density=density):
                    self.assertTrue(loaders,
                                    f'{key} offers {density} and no drive loads it')
                if not writers:
                    read_only_only.add((key, density))
        self.assertEqual(read_only_only, self.READ_ONLY_ONLY)
