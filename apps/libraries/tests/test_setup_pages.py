"""The setup pages: choosing a vendor, and choosing by tape.

The brand page listed LibraryBrand rows from the database. That table had
grown case-duplicated pairs (Dell and DELL, Spectra and SPECTRA, ...) and a
TestVendor left by a test run, so the page showed fifteen "vendors" for nine
profiles - and a profile with no row would not have appeared at all. The
profiles are the authority, so the page reads them.
"""
import re
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.storage.fallback import FallbackStorage
from django.http import Http404
from django.test import RequestFactory, override_settings
from django.utils.html import escape

from .base import TestCase

from apps.libraries import views
from apps.libraries.models import LibraryBrand
from apps.libraries.services.profiles import PROFILES


def request_for(path='/'):
    request = RequestFactory().get(path)
    request.session = {'mhvtl_logged_in': True}
    request._messages = FallbackStorage(request)
    request.user = AnonymousUser()
    return request


#: One `<option>` of a rendered select, however the template wrapped its
#: attributes across lines.
OPTION = re.compile(r'<option value="([^"]*)"\s*(selected)?[^>]*>([^<]*)</option>',
                    re.S)


def rows(page, kind):
    """Every rendered row of one section: (values, selected, count).

    The form holds a row per kind of drive and of tape since 5 October 2026,
    so there is no one ``driveModelSelect`` to read any more. The rows carry
    no ids - they repeat - and are marked with ``data-kind``.
    """
    chunks = page.split(f'data-kind="{kind}"')[1:]
    found = []
    for chunk in chunks:
        chunk = chunk.split('data-kind=')[0]
        block = re.search(r'<select[^>]*>(.*?)</select>', chunk, re.S)
        options = OPTION.findall(block.group(1)) if block else []
        chosen = [value for value, mark, _label in options if mark]
        count = re.search(r'class="form-input kind-count"\s*\n?\s*value="(\d+)"',
                          chunk)
        found.append(([value for value, _m, _l in options],
                      chosen[0] if chosen else None,
                      int(count.group(1)) if count else None))
    return found


def rendered(page, select_id):
    """(values, the selected value, labels by value) of one rendered select.

    The page embedded a JSON profile and built its options in JavaScript until
    4 October 2026, so these tests read the JSON. They read the HTML now,
    because the options and the selection are rendered by the server - which
    is the change this reads back.
    """
    block = re.search(rf'id="{select_id}"(.*?)</select>', page, re.S)
    assert block, f'no select with id={select_id!r} on the page'
    found = OPTION.findall(block.group(1))
    values = [value for value, _mark, _label in found]
    chosen = [value for value, mark, _label in found if mark]
    assert len(chosen) <= 1, f'{select_id} marks {len(chosen)} options selected'
    return (values, chosen[0] if chosen else None,
            {value: label.strip() for value, _mark, label in found})


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
        """Asking for LTO-10 leaves the drive that writes it, and its own two
        densities are what the page then offers."""
        response = views.BrandConfigView.as_view()(
            request_for('/?wanted_media=LTO10'), brand_name='SPECTRA')
        page = response.content.decode()
        drives, chosen, _count = rows(page, 'drive')[0]
        self.assertIn('ULT3580-TDA', drives)
        self.assertEqual(chosen, 'ULT3580-TDA')
        densities, _chosen, _count = rows(page, 'media')[0]
        self.assertEqual(densities, ['LTO10', 'LTO10P'])


class StartFromTheTapeTests(TestCase):
    """"I want an LTO-10 library" has to be answerable without knowing that
    LTO-10 means a TDA drive.

    The vendor page's filter carries the tape into the form
    (?wanted_media=), the form has the same choice on it, and both leave only
    the models and drives that take it.

    ``wanted_media`` and not ``media``: a cartridge row is ``media=LTO8:20``,
    and while both were spelled ``media`` the row answered the filter - see
    views._asked.
    """

    def chooser(self, brand, query=''):
        """(the densities offered, the one chosen, their labels)."""
        response = views.BrandConfigView.as_view()(request_for('/' + query),
                                                   brand_name=brand)
        return rendered(response.content.decode(), 'wantedMedia')

    def test_the_tape_arrives_from_the_vendor_page(self):
        self.assertEqual(self.chooser('ADIC', '?wanted_media=LTO10')[1], 'LTO10')
        self.assertEqual(self.chooser('ADIC')[1], '')

    def test_a_tape_this_vendor_does_not_take_is_ignored(self):
        """A stale link opens the page instead of failing."""
        self.assertEqual(self.chooser('SONY', '?wanted_media=LTO10')[1], '')

    def test_the_page_carries_the_labels_the_chooser_shows(self):
        from apps.libraries.services.profiles import catalogue, personalities

        offered, _chosen, labels = self.chooser('STK')
        self.assertEqual(labels['LTO9'], personalities.media_label('LTO9'))
        self.assertEqual(
            offered[1:],              # the first is "any tape"
            catalogue.densities_of(
                PROFILES['STK'].drive_models))

    def test_the_form_has_the_chooser_and_asks_the_server_with_it(self):
        """The filtering was five functions of JavaScript and is one fetch.
        What is checked is that the page has the chooser, that changing it
        asks, and that none of the decisions came back."""
        from django.template.loader import get_template

        source = get_template('libraries/brand_config.html').template.source
        self.assertIn('id="wantedMedia"', source)
        self.assertIn("libraries:setup_form_ajax", source)
        for gone in ('function modelTakes', 'function driveTakes',
                     'function bestDriveFor', 'function updateDriveModels',
                     'function updateMediaTypes', 'function applyLimits',
                     'const PROFILE', 'const PRESET'):
            self.assertNotIn(gone, source,
                             f'{gone} is a decision, and the service makes it')

    def test_the_vendor_cards_pass_the_tape_on(self):
        from django.template.loader import get_template

        source = get_template('libraries/brand_selection.html').template.source
        self.assertIn("link.searchParams.set('wanted_media', wanted)", source)

    def test_the_wanted_tape_and_a_cartridge_row_are_two_questions(self):
        """Both were spelled ``media``, and QueryDict.get answers with the
        LAST value, so the row answered the filter: the wanted tape arrived
        as 'LTO8:50', no vendor takes a density by that name, and choosing a
        tape at the top of the form quietly stopped narrowing the drives
        under it. Invisible to every test here until a browser showed an
        LTO-6 library full of LTO-8 cartridges.
        """
        page = views.BrandConfigView.as_view()(
            request_for('/?wanted_media=LTO6&drive=ULT3580-TD8:4'
                        '&media=LTO8:50'),
            brand_name='IBM').content.decode()
        self.assertEqual(rendered(page, 'wantedMedia')[1], 'LTO6')
        # The drive row asked for a TD8, which cannot touch an LTO-6 tape, so
        # the narrowing replaces it with the newest drive that writes one.
        self.assertEqual(rows(page, 'drive')[0][1], 'ULT3580-TD7')
        self.assertEqual(rows(page, 'drive')[0][2], 4)
        self.assertEqual(rows(page, 'media')[0][1], 'LTO6')
        self.assertEqual(rows(page, 'media')[0][2], 50)

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


def post_request_for(data, path='/'):
    """A POST the setup views will accept, with its messages readable after."""
    request = RequestFactory().post(path, data)
    request.session = {'mhvtl_logged_in': True}
    request._messages = FallbackStorage(request)
    request.user = AnonymousUser()
    return request


def said(request):
    """Every message the view added, as text."""
    return [str(message) for message in request._messages]


#: The three narrowing dropdowns, as the service names them and as the page
#: renders them. The tape chooser is beside them in SELECTS below.
#: The dropdowns that are one of a kind, by the id they are rendered with.
#: The drives and the cartridges are rows now and are read with rows().
SELECTS = (('library_model', 'libraryModelSelect'),
           ('wanted_media', 'wantedMedia'))


class PresetsInTheWizardTests(TestCase):
    """The wizard offers the same presets as the command line.

    Step 8 of plan-cli-catalogue, and the test of whether the earlier steps
    went into the right layer: the web cannot import mhvtl_cli, so anything
    the CLI had kept to itself had to move before this page could exist. One
    thing had - the rule for what a preset keeps out of a specification, now
    config.presets.savable.

    Every test here points MHVTL_GUI_CONFIG_DIR at a temporary directory, so
    the host's own /etc/mhvtl-gui/presets.toml is never read or written.
    """

    IBM_SMALL = {'profile': 'IBM', 'library_model': '03584L32',
                 'drive_model': 'ULT3580-TD8', 'media_type': 'LTO8',
                 'num_drives': 2, 'media_count': 20, 'empty_slots': 4}

    def setUp(self):
        from apps.libraries.services.libraries import presets

        self.presets = presets
        self.base = self.tmpdir()
        override = override_settings(MHVTL_GUI_CONFIG_DIR=str(self.base))
        override.enable()
        self.addCleanup(override.disable)

    def save(self, name, values):
        saved = self.presets.save(name, values)
        self.assertTrue(saved.success, saved.errors)
        return saved

    def setup_page(self):
        return views.SetupChoiceView.as_view()(request_for())

    def brand_page(self, brand='IBM', query=''):
        return views.BrandConfigView.as_view()(request_for('/' + query),
                                               brand_name=brand)

    # -- offering them ----------------------------------------------------

    def test_the_setup_page_offers_what_the_command_line_lists(self):
        self.save('ibm-small', self.IBM_SMALL)
        self.save('half-built', {'num_drives': 8})

        page = self.setup_page().content.decode()
        listed = self.presets.names()
        usable = [row['name'] for row in listed.data['presets'] if row['complete']]

        self.assertEqual(usable, ['ibm-small'])
        self.assertIn('A Saved Configuration', page)
        self.assertIn('ibm-small', page)
        # A half-built preset is legal and `preset list` shows it, but a form
        # cannot offer to create a library from one.
        self.assertNotIn('half-built', page)

    def test_the_setup_page_says_nothing_when_nothing_is_saved(self):
        """A card listing nothing teaches nothing."""
        page = self.setup_page().content.decode()
        self.assertNotIn('A Saved Configuration', page)

    def test_choosing_one_opens_the_form_for_its_own_vendor(self):
        """A preset names its profile, so the catalogue step is skipped."""
        self.save('ibm-small', self.IBM_SMALL)

        request = post_request_for({'setup_type': 'preset',
                                    'preset': 'ibm-small'})
        response = views.SetupChoiceView.as_view()(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/libraries/setup/brand/IBM/?preset=ibm-small')

    def test_a_mistyped_name_is_refused_in_the_services_words(self):
        self.save('ibm-small', self.IBM_SMALL)

        request = post_request_for({'setup_type': 'preset', 'preset': 'ibm-smal'})
        response = views.SetupChoiceView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        told = ' '.join(said(request))
        self.assertIn("no preset called 'ibm-smal'", told)
        self.assertIn('ibm-small', told)       # the fixes name what does exist

    def test_the_vendor_page_offers_only_its_own(self):
        self.save('ibm-small', self.IBM_SMALL)
        self.save('stk-t10k', {'profile': 'STK', 'library_model': 'SL500',
                               'drive_model': 'T10000C', 'media_type': 'T10KC'})

        page = self.brand_page('IBM').content.decode()
        self.assertIn('ibm-small', page)
        self.assertNotIn('stk-t10k', page)
        self.assertEqual(
            [row['name'] for row in
             self.presets.for_profile('IBM').data['presets']], ['ibm-small'])

    def test_each_one_shows_what_to_type_with_it(self):
        """The console is where a preset is easiest to make and the terminal
        is where it is most useful, so the page that lists them says what to
        type. The lines are the service's - the same ones `mhvtl preset show`
        ends with - so the browser composes nothing."""
        from apps.libraries.services.config.presets import as_commands

        self.save('ibm-small', self.IBM_SMALL)
        page = self.brand_page('IBM').content.decode()

        for line in as_commands('ibm-small', self.IBM_SMALL):
            with self.subTest(label=line['label']):
                self.assertIn(line['label'], page)
                self.assertIn(escape(line['command']), page)

    def test_the_page_composes_none_of_it(self):
        """No second place that knows how a preset is spelled.

        Comments are stripped before looking: what the page used to say is
        the reason these comments exist, and a guard that reads its own
        explanation finds the string it is forbidding. The literal-capacity
        guard learned the same lesson.
        """
        from django.template.loader import get_template

        source = get_template('libraries/brand_config.html').template.source
        self.assertIn('{% for line in preset.commands %}', source)
        self.assertIn('{{ preset.summary }}', source)

        code = re.sub(r'\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}', '',
                      source, flags=re.S)
        for invented in ('mhvtl preset set', 'mhvtl library create --preset',
                         'preset.spec.num_drives', 'vendor default'):
            self.assertNotIn(invented, code,
                             'the template is composing its own answer')

    def test_a_mixed_preset_is_summarised_by_what_it_holds(self):
        """The card read "03584L32, vendor default drive(s), vendor default x
        vendor default" for this one, because it was built from raw
        specification fields and a mixed preset has none of them - its lists
        imply the count and the density."""
        self.save('lib-ten', {'profile': 'IBM', 'library_model': '03584L32',
                              'drive': [{'model': 'ULT3580-TD8', 'count': 2},
                                        {'model': 'ULT3580-TD6', 'count': 2}],
                              'media': [{'density': 'LTO8', 'count': 20},
                                        {'density': 'LTO6', 'count': 10}]})
        page = self.brand_page('IBM').content.decode()
        self.assertIn('2 x ULT3580-TD8 + 2 x ULT3580-TD6', page)
        self.assertIn('20 x LTO8 + 10 x LTO6', page)
        self.assertNotIn('vendor default', page)

    # -- folded, because four of them filled the page ----------------------

    def test_each_saved_configuration_is_folded_to_one_line(self):
        """Unfolded, a card is the name, the breakdown, the total and two
        long commands - four of them filled 1.2 screens of a phone before the
        form's first field. Closed it is the name and the service's one-line
        brief; the rest is what the line opens onto.

        <details> and not a handler: the fold is a browser behaviour and
        needs no state of ours, which is also why the page keeps deciding
        nothing.
        """
        self.save('ibm-small', self.IBM_SMALL)
        self.save('ibm-other', dict(self.IBM_SMALL, num_drives=4))
        page = self.brand_page('IBM').content.decode()

        cards = re.findall(r'<details class="preview-item"([^>]*)>', page)
        self.assertEqual(len(cards), 2)
        self.assertNotIn('open', ' '.join(cards),
                         'neither is in use, and there is more than one')
        for name in ('ibm-small', 'ibm-other'):
            brief = self.presets.describe(name).data['brief']
            self.assertIn(brief, page)

    def test_the_one_in_use_is_open_and_so_is_an_only_one(self):
        """A preset behind a click is a click for nothing when it is the one
        already applied, or the only one there is."""
        self.save('ibm-small', self.IBM_SMALL)
        alone = self.brand_page('IBM').content.decode()
        self.assertRegex(alone, r'<details class="preview-item"\s+open>')

        self.save('ibm-other', dict(self.IBM_SMALL, num_drives=4))
        applied = self.brand_page('IBM', '?preset=ibm-small').content.decode()
        # (is it open, whose card is it) - by name, because the cards are in
        # the service's order, which is the file's: ibm-other sorts first.
        cards = [(bool(mark.strip()), summary) for mark, summary in re.findall(
            r'<details class="preview-item[^"]*"([^>]*)>(.*?)</summary>',
            applied, re.S)]
        self.assertEqual(len(cards), 2)
        opened = [summary for is_open, summary in cards if is_open]
        self.assertEqual(len(opened), 1, 'only the one in use is open')
        # As read, not as marked up: the badge is its own element so the card
        # can carry the mark, which is what makes it findable in a list.
        said = ' '.join(re.sub(r'<[^>]+>', ' ', opened[0]).split())
        self.assertIn('ibm-small in use', said)

    def test_the_one_in_use_is_marked_on_the_card(self):
        """Open, unlinked and a grey "(in use)" were three small differences,
        none of them visible without reading the list. The card carries the
        mark now, in the green this console uses for a current state."""
        self.save('ibm-small', self.IBM_SMALL)
        self.save('ibm-other', dict(self.IBM_SMALL, num_drives=4))
        page = self.brand_page('IBM', '?preset=ibm-small').content.decode()

        marked = re.findall(r'<details class="preview-item in-use-card"'
                            r'[^>]*>(.*?)</summary>', page, re.S)
        self.assertEqual(len(marked), 1, 'one card is marked, and only one')
        self.assertIn('ibm-small', marked[0])
        self.assertIn('<span class="in-use">in use</span>', marked[0])

    # -- applying one -----------------------------------------------------

    def test_opening_the_form_from_one_fills_in_the_same_values(self):
        """What the page starts from has to be what --preset would use: the
        same resolve() call, so there is nothing to drift."""
        self.save('ibm-small', self.IBM_SMALL)
        resolved = self.presets.resolve('ibm-small')
        self.assertTrue(resolved.success, resolved.errors)
        spec = resolved.data['spec']

        page = self.brand_page('IBM', '?preset=ibm-small').content.decode()

        # The server marks the preset's choice in each dropdown. The script
        # used to prefer PRESET over PROFILE.defaults in three places, which
        # is the half that could be - and was - got wrong.
        self.assertEqual(rendered(page, 'libraryModelSelect')[1],
                         spec['library_model'])
        self.assertEqual(rows(page, 'drive')[0][1], spec['drive_model'])
        self.assertEqual(rows(page, 'media')[0][1], spec['media_type'])
        # The tape goes where the vendor page's filter puts it, so the
        # dropdowns need no second mechanism.
        self.assertEqual(rendered(page, 'wantedMedia')[1], spec['media_type'])
        # The counts are rendered already capped, rather than rendered and
        # then reset: the input said value="2" while the page showed 4.
        self.assertEqual(rows(page, 'drive')[0][2], spec['num_drives'])
        self.assertEqual(rows(page, 'media')[0][2], spec['media_count'])
        self.assertIn(f'id="emptySlots" value="{spec["empty_slots"]}"', page)

    def test_without_a_preset_the_form_starts_from_the_profile(self):
        from apps.libraries.services.profiles.data import get_profile_options

        defaults = get_profile_options('IBM')['defaults']
        page = self.brand_page('IBM').content.decode()

        self.assertEqual(rendered(page, 'libraryModelSelect')[1],
                         defaults['library_model'])
        self.assertEqual(rows(page, 'drive')[0][1], defaults['drive_model'])
        self.assertEqual(rows(page, 'media')[0][1], defaults['media_type'])
        self.assertEqual(rows(page, 'drive')[0][2], defaults['num_drives'])
        self.assertEqual(rows(page, 'media')[0][2], defaults['media_count'])

    def test_the_preset_is_selected_rather_than_preferred_by_a_script(self):
        """The bug this replaces, and why it was invisible.

        The drive count was rendered ``value="2"`` from the preset and the
        page showed 4, because ``applyLimits()`` reset it to the model's
        default on every model change - so the rendered attribute and what
        the page showed were two different facts, and the Django test
        asserting ``value="2"`` passed throughout. It was found in Chromium.

        There is nothing left to prefer: the server marks the option and the
        count is rendered capped, so the attribute IS what the page shows.
        """
        self.save('ibm-small', self.IBM_SMALL)
        page = self.brand_page('IBM', '?preset=ibm-small').content.decode()

        # With this tape and no preset the newest writer of LTO-8 would be
        # chosen; the preset asks for the TD8 and gets it.
        self.assertEqual(rows(page, 'drive')[0][1], 'ULT3580-TD8')
        self.assertEqual(rows(page, 'drive')[0][2], 2)

    def test_an_unusable_preset_leaves_the_page_working(self):
        """It says what is wrong and opens the form anyway, rather than 500."""
        response = self.brand_page('IBM', '?preset=nothing-like-it')
        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        from apps.libraries.services.profiles.data import get_profile_options

        self.assertEqual(rendered(page, 'libraryModelSelect')[1],
                         get_profile_options('IBM')['defaults']['library_model'])

    # -- keeping one ------------------------------------------------------

    def test_saving_from_the_form_keeps_what_the_cli_keeps(self):
        """`--save-preset NAME` and the form's "save it as" are one rule.

        Not a comparison of two implementations - there is only one, and this
        proves the web reaches it.
        """
        from mhvtl_cli.commands import library as command

        library_data = dict(self.IBM_SMALL, library_id=90, product='03584L32',
                            drive_product='ULT3580-TD8', serial='XYZZY_90',
                            barcode_prefix='L90')
        request = post_request_for({'save_preset': 'from-the-form'})
        views.BrandConfigView._keep_preset(request, library_data)

        resolved = self.presets.resolve('from-the-form')
        self.assertTrue(resolved.success, resolved.errors)
        self.assertEqual(resolved.data['spec'], command._savable(library_data))
        self.assertNotIn('library_id', resolved.data['spec'])

    def test_the_form_will_not_save_a_preset_named_after_a_vendor(self):
        """`--preset IBM` is meaningless, so the name is refused here too -
        by the service, which is why it cannot be refused in one place only."""
        request = post_request_for({'save_preset': 'IBM'})
        views.BrandConfigView._keep_preset(request, dict(self.IBM_SMALL))

        self.assertIn('vendor profile', ' '.join(said(request)))
        self.assertEqual(self.presets.names().data['presets'], [])

    def test_nothing_is_saved_when_no_name_is_given(self):
        request = post_request_for({'save_preset': '  '})
        views.BrandConfigView._keep_preset(request, dict(self.IBM_SMALL))
        self.assertEqual(said(request), [])
        self.assertEqual(self.presets.names().data['presets'], [])

    def test_saving_a_mixed_library_keeps_every_kind(self):
        """The form can describe two kinds of drive since 5 October 2026, so
        keeping one has to write both lists - and the counts must *not* come
        with them, because a count beside a list is two answers to one
        question (config.presets.IMPLIED_BY_LIST)."""
        mixed = dict(self.IBM_SMALL, library_id=90, product='03584L32',
                     serial='XYZZY_90', barcode_prefix='L90',
                     drive=[{'model': 'ULT3580-TD8', 'count': 2},
                            {'model': 'ULT3580-TD6', 'count': 2}],
                     media=[{'density': 'LTO8', 'count': 20},
                            {'density': 'LTO6', 'count': 10}])
        request = post_request_for({'save_preset': 'mixed-from-the-form'})
        views.BrandConfigView._keep_preset(request, mixed)

        spec = self.presets.resolve('mixed-from-the-form').data['spec']
        self.assertEqual(spec['drive'], mixed['drive'])
        self.assertEqual(spec['media'], mixed['media'])
        for implied in ('num_drives', 'media_count', 'drive_model',
                        'media_type'):
            self.assertNotIn(implied, spec)

    def test_a_name_the_file_cannot_hold_is_refused_on_the_form(self):
        """`lab.small` is a nested table in TOML, so it would come back as a
        preset called 'small' inside one called 'lab' and stop the whole file
        loading - taking every other preset with it. The form shows the
        service's refusal rather than quietly not saving."""
        request = post_request_for({'save_preset': 'lab.small'})
        views.BrandConfigView._keep_preset(request, dict(self.IBM_SMALL))

        self.assertIn('was not saved', ' '.join(said(request)))
        self.assertEqual(self.presets.names().data['presets'], [])


class SameAnswersAsTheTerminalTests(TestCase):
    """The page and the command line have to answer the same questions alike.

    They did not. The form's script worked out two things for itself, and both
    had drifted from the services by the time anybody compared them:

    - which densities a drive "takes". The script read what a drive LOADS, so
      a drive that can only read the chosen tape was offered as a way to
      write it. `profile show --media` filters on what a drive WRITES.
    - which drive a model gets for a wanted density. The script took the last
      writer listed and IBM lists half-height drives after full-height ones,
      so LTO9 chose a ULT3580-HH9 where the service chooses a ULT3580-TD9.

    Both were lookups embedded in the page, which was better than two rules -
    and the page still chose from them. Since 4 October 2026 the server marks
    the selected option and the page renders it, so the comparison below is
    between what the page shows and what the service said, for every profile.
    There is nothing left that could answer differently.

    Measured in Chromium as well, because a rendered value and what the page
    ends up showing are not the same fact. The browser selects TD9 for LTO9,
    TDA for LTO10 and T10000C for T10KC, matching the service.
    """

    def page(self, brand, query='/'):
        return views.BrandConfigView.as_view()(
            request_for(query), brand_name=brand).content.decode()

    def test_the_page_renders_exactly_what_the_service_decided(self):
        """Every dropdown of every vendor, against setup_form.state()."""
        from apps.libraries.services.libraries import setup_form

        for profile in PROFILES:
            answer = setup_form.state(profile)
            self.assertTrue(answer.success, answer.message)
            page = self.page(profile)
            for field, select_id in SELECTS:
                with self.subTest(profile=profile, field=field):
                    values, chosen, labels = rendered(page, select_id)
                    decided = answer.data[field]
                    self.assertEqual(values,
                                     [option['value']
                                      for option in decided['options']])
                    self.assertEqual(labels,
                                     {option['value']: option['label']
                                      for option in decided['options']})
                    self.assertEqual(chosen, decided['selected'] or '')

    def test_the_counts_and_the_limits_are_the_services_too(self):
        from apps.libraries.services.libraries import setup_form

        for profile in PROFILES:
            answer = setup_form.state(profile).data
            page = self.page(profile)
            with self.subTest(profile=profile):
                self.assertEqual(rows(page, 'drive')[0][2],
                                 answer['drives']['rows'][0]['count'])
                self.assertIn(f'max="{answer["limits"]["max_drives"]}"', page)
                self.assertIn(answer['says']['drives'], page)

    def test_writes_is_not_loads(self):
        """The distinction the page used to miss. An IBM TD3 loads LTO1 and
        cannot write it, so asking for an LTO1 library must not offer it as a
        way to write one."""
        from apps.libraries.services.profiles import catalogue, data

        self.assertIn('LTO1', data.creatable_media('ULT3580-TD3'))
        self.assertNotIn('LTO1', catalogue.writes('ULT3580-TD3'))

        drives, chosen, _count = rows(self.page('IBM', '/?wanted_media=LTO1'),
                                      'drive')[0]
        self.assertNotIn('ULT3580-TD3', drives)
        self.assertIn('LTO1', catalogue.writes(chosen))

    def test_full_height_beats_half_height_on_the_page_too(self):
        """The case that proved the page and the terminal disagreed: it took
        the last writer listed, and IBM lists half-height after full."""
        for density, expected in (('LTO9', 'ULT3580-TD9'),
                                  ('LTO10', 'ULT3580-TDA'),
                                  ('LTO8', 'ULT3580-TD8')):
            with self.subTest(density=density):
                page = self.page('IBM', f'/?wanted_media={density}')
                self.assertEqual(rows(page, 'drive')[0][1], expected)

    def test_a_tape_no_drive_of_the_vendor_writes_leaves_the_page_whole(self):
        """SONY writes no LTO at all, so asking for LTO9 there is a stale
        link: the page opens on the vendor's own defaults."""
        page = self.page('SONY', '/?wanted_media=LTO9')
        self.assertEqual(rendered(page, 'wantedMedia')[1], '')
        self.assertTrue(rows(page, 'drive')[0][0])

    def test_the_vendor_page_and_profile_list_offer_the_same_vendors(self):
        """`mhvtl profile list` and the brand page read one composition."""
        from apps.libraries.services.profiles import catalogue

        self.assertEqual([card['key'] for card in views._brand_catalogue()],
                         catalogue.names())

    def test_the_vendor_page_and_profile_list_agree_on_the_densities(self):
        from apps.libraries.services.profiles import catalogue

        for card in views._brand_catalogue():
            with self.subTest(vendor=card['key']):
                self.assertEqual(
                    card['media'],
                    catalogue.describe(card['key'])['densities'])

    def test_the_media_filter_and_profile_list_media_agree(self):
        """The page's filter links and `profile list --media` are the same
        question: which vendors can write this tape?"""
        from apps.libraries.services.profiles import catalogue

        for density in ('LTO9', 'T10KC', 'AIT4', 'SDLT600'):
            with self.subTest(density=density):
                from_cards = [
                    card['key'] for card in views._brand_catalogue()
                    if any(density in catalogue.writes(row['model'])
                           for row
                           in catalogue.describe(card['key'])['drives'])]
                self.assertEqual(from_cards, catalogue.names(media=density))
