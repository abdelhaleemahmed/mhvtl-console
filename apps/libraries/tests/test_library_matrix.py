"""Every library we offer, with every drive it may carry, created for real.

The question this answers: can the application create each supported library,
with each supported drive, and produce a device.conf that MHVTL 1.8 will emulate
as intended?

For every profile, every library model it offers and every drive model that
library may carry, a library is created in a scratch configuration directory
beside the real fixture libraries, then read back and checked:

  - validation accepts it and creation succeeds
  - device.conf holds the library and its drives, with the strings chosen
  - no id or SCSI target collides with the libraries already there
  - MHVTL would emulate the drive as that model, not as a generic drive
  - MHVTL would emulate the library with the layout listed in EXPECTED_LAYOUT
  - library_contents holds the tapes, with a barcode suffix the drive reads

EXPECTED_LAYOUT is written out by hand from usr/cmd/vtllibrary.c
customise_lu() rather than computed, so it checks personalities.library_layout
as well as the profiles. docs/sphinx/guides/library-limits.rst explains the
selector and the limits.

systemd is never touched: every library is created with start_services=False.
"""
import shutil
import tempfile
from pathlib import Path

from django.test import TestCase

from apps.libraries.services.config import device_conf, library_contents
from apps.libraries.services.libraries import lifecycle, validation
from apps.libraries.services.profiles import PROFILES, personalities
from apps.libraries.services.profiles.data import (get_default_media_for_drive,
                                                   get_media_suffix,
                                                   get_valid_drives_for_library)

FIXTURES = Path(__file__).parent / 'fixtures'
NEW_LIBRARY_ID = 40

#: (profile, library model) -> the SMC layout vtllibrary picks. See the module
#: docstring; ADIC, QUANTUM and OVERLAND are matched on the product string.
EXPECTED_LAYOUT = {
    ('ADIC', 'Scalar i2000'): 'init_default_smc',
    ('ADIC', 'Scalar 1000'): 'init_default_smc',
    ('ADIC', 'scalar'): 'init_default_smc',
    # Scalar-layout variants (profiles/data.with_scalar_layout)
    ('ADIC', 'ADIC i2000'): 'init_scalar_smc',
    ('ADIC', 'ADIC Scalar 1000'): 'init_scalar_smc',
    ('ADIC', 'ADIC scalar'): 'init_scalar_smc',
    ('DELL', 'PV-136T'): 'init_default_smc',
    ('HP', 'MSL G3 Series'): 'init_hp_msl_smc',
    ('HP', 'MSL6000 Series'): 'init_hp_msl_smc',
    ('HP', 'EML E-Series'): 'init_hp_eml_smc',
    ('HP', 'ESL E-Series'): 'init_hp_eml_smc',
    **{('IBM', model): 'init_ibm3584' for model in (
        '03584L32', '03584D32', '03584L52', '03584D52', '03584L53', '03584D53',
        '03584L22', '03584D22', '03584L23', '03584D23', '03584L42')},
    ('IBM', '3573-TL'): 'init_ibmts3100',
    ('IBM', 'ULT3582-TL'): 'init_default_smc',
    ('OVERLAND', 'NEO Series'): 'init_default_smc',
    ('OVERLAND', 'OVERLAND'): 'init_overland_smc',
    ('QUANTUM', 'Scalar i500'): 'init_default_smc',
    ('QUANTUM', 'Scalar i6000'): 'init_default_smc',
    ('QUANTUM', 'DXi6700'): 'init_default_smc',
    ('QUANTUM', 'PX720'): 'init_default_smc',
    ('QUANTUM', 'DX5000'): 'init_default_smc',
    ('QUANTUM', 'QUANTUM i500'): 'init_scalar_smc',
    ('QUANTUM', 'QUANTUM i6000'): 'init_scalar_smc',
    ('SONY', 'LIB-302'): 'init_default_smc',
    ('SONY', 'LIB-152'): 'init_default_smc',
    ('SPECTRA', 'PYTHON'): 'init_spectra_logic_smc',
    ('SPECTRA', 'GECKO'): 'init_spectra_logic_smc',
    ('SPECTRA', '215'): 'init_spectra_215_smc',
    ('SPECTRA', 'GATOR'): 'init_spectra_gator_smc',
    ('STK', 'SL500'): 'init_stkslxx',
    ('STK', 'L20'): 'init_stkl20',
    ('STK', 'L40'): 'init_stkl20',
    ('STK', 'L80'): 'init_stkl20',
    ('STK', 'SL150'): 'init_stklxx',
    ('STK', 'SL3000'): 'init_stklxx',
    ('STK', 'L700'): 'init_stklxx',
    ('STK', 'L180'): 'init_stklxx',
    ('STK', 'L120'): 'init_stklxx',
}


def combinations():
    """Every (profile, library model, drive model) the profiles allow."""
    for key, profile in sorted(PROFILES.items()):
        for model in profile.library_models:
            for drive in get_valid_drives_for_library(key, model):
                yield key, model, drive


class MatrixTests(TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())

    def _fresh_config(self):
        """A scratch /etc/mhvtl holding the three real fixture libraries."""
        directory = Path(tempfile.mkdtemp(dir=self.base))
        for name in ('device.conf', 'library_contents.10',
                     'library_contents.20', 'library_contents.30'):
            shutil.copy(FIXTURES / name, directory)
        return directory

    def test_every_library_model_has_an_expected_layout(self):
        """A model added to a profile must be given an expected layout here."""
        offered = {(key, model) for key, profile in PROFILES.items()
                   for model in profile.library_models}
        self.assertEqual(offered, set(EXPECTED_LAYOUT))

    def test_every_library_model_resolves_to_its_expected_layout(self):
        for (key, model), expected in EXPECTED_LAYOUT.items():
            with self.subTest(profile=key, model=model):
                vendor = PROFILES[key].library_vendor
                self.assertEqual(personalities.library_layout(vendor, model).init,
                                 expected)

    def test_no_profile_offers_a_drive_mhvtl_would_emulate_as_generic(self):
        for key, profile in PROFILES.items():
            for drive in profile.drive_models:
                with self.subTest(profile=key, drive=drive):
                    self.assertNotEqual(personalities.drive_personality(drive),
                                        personalities.GENERIC_DRIVE)

    def test_every_combination_can_be_created(self):
        checked = 0
        for key, model, drive in combinations():
            with self.subTest(profile=key, model=model, drive=drive):
                self._create_and_check(key, model, drive)
                checked += 1
        self.assertGreater(checked, 100, 'the matrix should not be empty')

    def _create_and_check(self, key, model, drive):
        config = self._fresh_config()
        before = device_conf.parse((config / 'device.conf').read_text())
        media = get_default_media_for_drive(key, drive)
        spec = {'profile': key, 'library_id': NEW_LIBRARY_ID,
                'library_model': model, 'drive_model': drive,
                'media_type': media, 'num_drives': 2, 'media_count': 3}

        checked = validation.validate(dict(spec), config)
        self.assertTrue(checked.is_valid, checked.errors)

        result = lifecycle.create(dict(spec), config, start_services=False)
        self.assertTrue(result.success, result.errors)

        after = device_conf.parse((config / 'device.conf').read_text())
        library = after.libraries[NEW_LIBRARY_ID]
        self.assertEqual(library['vendor'], PROFILES[key].library_vendor)
        self.assertEqual(library['product'], model)

        drives = after.drives_of(NEW_LIBRARY_ID)
        self.assertEqual(len(drives), 2)
        for drive_id, entry in drives.items():
            self.assertEqual(entry['product'], drive)
            self.assertNotIn(drive_id, before.libraries)
            self.assertNotIn(drive_id, before.drives)
            self.assertNotEqual(drive_id % 10, 0)

        new_targets = {library['target']} | {d['target'] for d in drives.values()}
        self.assertEqual(len(new_targets), 3)
        self.assertFalse(new_targets & before.used_targets())

        self.assertEqual(personalities.library_layout(library['vendor'],
                                                      library['product']).init,
                         EXPECTED_LAYOUT[(key, model)])
        self.assertNotEqual(personalities.drive_personality(drive),
                            personalities.GENERIC_DRIVE)

        contents = library_contents.parse(
            (config / f'library_contents.{NEW_LIBRARY_ID}').read_text())
        self.assertEqual(len(contents.occupied), 3)
        self.assertEqual(contents.drive_count, 2)
        suffix = get_media_suffix(media)
        self.assertTrue(all(slot.barcode.endswith(suffix)
                            for slot in contents.occupied), (media, suffix))


class LimitTests(TestCase):
    """The layouts' limits are refused before anything is written."""

    def setUp(self):
        self.config = Path(tempfile.mkdtemp())
        shutil.copy(FIXTURES / 'device.conf', self.config)

    def _validate(self, **spec):
        return validation.validate({'library_id': NEW_LIBRARY_ID, **spec},
                                   self.config)

    def test_spectra_215_holds_55_drives(self):
        self.assertTrue(self._validate(profile='SPECTRA', library_model='215',
                                       num_drives=55, media_count=10).is_valid)
        refused = self._validate(profile='SPECTRA', library_model='215',
                                 num_drives=56, media_count=10)
        self.assertFalse(refused.is_valid)
        self.assertIn('at most 55 drives', ' '.join(refused.errors))

    def test_spectra_215_holds_30_slots(self):
        refused = self._validate(profile='SPECTRA', library_model='215',
                                 num_drives=2, media_count=25, empty_slots=6)
        self.assertIn('at most 30 storage slots', ' '.join(refused.errors))

    def test_hp_msl_holds_416_slots(self):
        refused = self._validate(profile='HP', library_model='MSL G3 Series',
                                 num_drives=2, media_count=417)
        self.assertIn('at most 416 storage slots', ' '.join(refused.errors))

    def test_hp_eml_is_not_limited_like_msl(self):
        self.assertTrue(self._validate(profile='HP', library_model='EML E-Series',
                                       num_drives=2, media_count=417).is_valid)

    def test_overland_has_one_map_slot(self):
        refused = self._validate(profile='OVERLAND', library_model='OVERLAND',
                                 num_drives=2, media_count=10, map_count=2)
        self.assertIn('at most 1 MAP slots', ' '.join(refused.errors))

    def test_more_than_nine_drives_is_fine_where_the_model_allows_it(self):
        """The old cap was an artefact of library_id + slot."""
        result = self._validate(profile='STK', library_model='L700',
                                drive_model='ULT3580-TD8', media_type='LTO8',
                                num_drives=40, media_count=10)
        self.assertTrue(result.is_valid, result.errors)

    def test_a_library_id_used_by_a_drive_is_refused(self):
        refused = validation.validate({'library_id': 11, 'profile': 'STK'},
                                      self.config)
        self.assertIn('already used by drive 11', ' '.join(refused.errors))

    def test_a_library_id_above_1023_is_refused(self):
        refused = validation.validate({'library_id': 1030, 'profile': 'STK'},
                                      self.config)
        self.assertIn('between 1 and 1023', ' '.join(refused.errors))

    def test_a_three_digit_library_id_is_a_warning_not_an_error(self):
        result = validation.validate({'library_id': 110, 'profile': 'STK'},
                                     self.config)
        self.assertTrue(result.is_valid, result.errors)
        self.assertIn('NAA', ' '.join(result.warnings))


class DefaultsTests(TestCase):
    """Every library model, created with nothing but its profile's defaults.

    A default must be valid for the model MHVTL emulates. The OVERLAND profile
    defaulted to four MAP slots in a layout with room for one, and MHVTL
    silently dropped the other three; Spectra 215 defaulted to 50 tapes in a
    30-slot library.
    """

    def test_every_library_model_accepts_its_own_defaults(self):
        for (key, model) in sorted(EXPECTED_LAYOUT):
            with self.subTest(profile=key, model=model):
                config = Path(tempfile.mkdtemp())
                shutil.copy(FIXTURES / 'device.conf', config)
                drive = get_valid_drives_for_library(key, model)[0]
                spec = {'profile': key, 'library_id': NEW_LIBRARY_ID,
                        'library_model': model, 'drive_model': drive,
                        'media_type': get_default_media_for_drive(key, drive)}

                checked = validation.validate(dict(spec), config)
                self.assertTrue(checked.is_valid, checked.errors)
                result = lifecycle.create(dict(spec), config, start_services=False)
                self.assertTrue(result.success, result.errors)

                layout = personalities.LAYOUTS[EXPECTED_LAYOUT[(key, model)]]
                contents = library_contents.parse(
                    (config / f'library_contents.{NEW_LIBRARY_ID}').read_text())
                self.assertLessEqual(len(contents.map_slots), layout.max_maps)
                self.assertLessEqual(len(contents.slots), layout.max_slots)
                self.assertLessEqual(contents.drive_count, layout.max_drives)

    def test_overland_defaults_to_its_single_map_slot(self):
        from apps.libraries.services.libraries import spec as spec_rules
        filled = spec_rules.apply_defaults({'profile': 'OVERLAND',
                                            'library_id': 40,
                                            'library_model': 'OVERLAND'})
        self.assertEqual(filled['map_count'], 1)

    def test_spectra_215_defaults_fit_its_30_slots(self):
        from apps.libraries.services.libraries import spec as spec_rules
        filled = spec_rules.apply_defaults({'profile': 'SPECTRA',
                                            'library_id': 40,
                                            'library_model': '215'})
        self.assertLessEqual(filled['media_count'] + filled['empty_slots'], 30)

    def test_an_explicit_value_over_the_limit_is_still_refused(self):
        config = Path(tempfile.mkdtemp())
        shutil.copy(FIXTURES / 'device.conf', config)
        refused = validation.validate({'profile': 'OVERLAND', 'library_id': 40,
                                       'library_model': 'OVERLAND',
                                       'map_count': 4}, config)
        self.assertFalse(refused.is_valid)


class NothingChosenTests(TestCase):
    """A model created with only its profile and id - no drive, no media.

    Twelve models used to refuse this: the profile's single default drive
    (ULT3580-TD8 for IBM, T10000C for STK) is not one they can carry.
    """

    def test_every_library_model_creates_with_nothing_chosen(self):
        for (key, model) in sorted(EXPECTED_LAYOUT):
            with self.subTest(profile=key, model=model):
                config = Path(tempfile.mkdtemp())
                shutil.copy(FIXTURES / 'device.conf', config)
                spec = {'profile': key, 'library_id': NEW_LIBRARY_ID,
                        'library_model': model}
                self.assertTrue(validation.validate(dict(spec), config).is_valid)
                result = lifecycle.create(dict(spec), config, start_services=False)
                self.assertTrue(result.success, result.errors)

                drives = device_conf.parse(
                    (config / 'device.conf').read_text()).drives_of(NEW_LIBRARY_ID)
                products = {entry['product'] for entry in drives.values()}
                self.assertTrue(products <= set(
                    get_valid_drives_for_library(key, model)), products)

    def test_the_default_drive_prefers_the_newest_lto_over_the_last_listed(self):
        """STK L700's list ends with DLT7000."""
        from apps.libraries.services.profiles.data import get_default_drive_for_library
        self.assertEqual(get_default_drive_for_library('STK', 'L700'), 'ULT3580-TDA')

    def test_the_profile_default_is_kept_where_the_model_accepts_it(self):
        from apps.libraries.services.profiles.data import get_default_drive_for_library
        self.assertEqual(get_default_drive_for_library('STK', 'SL500'),
                         PROFILES['STK'].drive_product_default)

    def test_a_3592_only_library_gets_a_3592_drive(self):
        from apps.libraries.services.libraries import spec as spec_rules
        filled = spec_rules.apply_defaults({'profile': 'IBM', 'library_id': 40,
                                            'library_model': '03584L22'})
        self.assertTrue(filled['drive_product'].startswith('03592'))
        self.assertIn(filled['media_type'], ('J1A', 'E05', 'E06', 'E07')
                      + tuple(PROFILES['IBM'].drive_media_by_model[filled['drive_product']]))



class CoverageTests(TestCase):
    """Everything MHVTL 1.8 can emulate is offered by some profile.

    Spectra Gator, the LTO-10 drives and the ULTRIUM-* names used to be missing,
    and so was the Scalar layout: MHVTL reaches it only for a product beginning
    ADIC or QUANTUM (upstream 24f7342), which no real Scalar reports.
    profiles/data.with_scalar_layout now adds a model of that form for each
    ADIC and QUANTUM Scalar.
    """

    NOT_OFFERED_ON_PURPOSE = set()

    def test_every_library_layout_is_reachable(self):
        reached = {personalities.library_layout(PROFILES[key].library_vendor,
                                                model).init
                   for key, model in EXPECTED_LAYOUT}
        self.assertEqual(set(personalities.LAYOUTS) - reached,
                         self.NOT_OFFERED_ON_PURPOSE)

    def test_each_scalar_model_has_a_scalar_layout_variant(self):
        for key in ('ADIC', 'QUANTUM'):
            profile = PROFILES[key]
            for model in profile.library_models:
                if 'scalar' not in model.lower() or model.upper().startswith(key):
                    continue
                with self.subTest(profile=key, model=model):
                    variant = personalities.scalar_layout_product(key, model)
                    self.assertIn(variant, profile.library_models)
                    self.assertLessEqual(len(variant), personalities.PRODUCT_ID_LEN)
                    self.assertEqual(profile.library_drive_mapping[variant],
                                     profile.library_drive_mapping[model])

    def test_the_variant_rule(self):
        product = personalities.scalar_layout_product
        self.assertEqual(product('ADIC', 'Scalar 1000'), 'ADIC Scalar 1000')
        self.assertEqual(product('ADIC', 'Scalar i2000'), 'ADIC i2000')
        self.assertEqual(product('QUANTUM', 'Scalar i6000'), 'QUANTUM i6000')
        self.assertIsNone(product('QUANTUM', 'DXi6700'))
        self.assertIsNone(product('STK', 'Scalar i500'))

    def test_every_drive_mhvtl_recognises_is_offered(self):
        offered = {drive for profile in PROFILES.values()
                   for drive in profile.drive_models}
        self.assertEqual(set(personalities.DRIVE_PERSONALITIES) - offered, set())

    def test_every_offered_drive_has_media_with_a_barcode_suffix(self):
        for key, profile in PROFILES.items():
            for drive, media in profile.drive_media_by_model.items():
                for medium in media:
                    with self.subTest(profile=key, drive=drive, media=medium):
                        self.assertTrue(get_media_suffix(medium))

    def test_lto10_media_gets_the_suffixes_mhvtl_expects(self):
        """etc/generate_library_contents.in:74 - LA data, PA Premium."""
        self.assertEqual(get_media_suffix('LTO10'), 'LA')
        self.assertEqual(get_media_suffix('LTO10P'), 'PA')


class CreateFormDataTests(TestCase):
    """What get_profile_options gives the create-library form.

    The form used to pick the last drive listed (DLT7000 on an STK L700),
    cap every model at 64 drives and 15,000 slots, and send a num_maps key
    nothing read. Its behaviour was checked in a browser DOM (jsdom) when
    this changed; these keep the data it relies on.
    """

    def test_every_model_has_its_default_drive_and_limits(self):
        from apps.libraries.services.profiles.data import (
            get_default_drive_for_library, get_profile_options)

        for key, profile in PROFILES.items():
            options = get_profile_options(key)
            for model in profile.library_models:
                with self.subTest(profile=key, model=model):
                    drive = options['default_drive_by_library'][model]
                    self.assertEqual(drive, get_default_drive_for_library(key, model))
                    self.assertIn(drive, options['library_drive_mapping'].get(
                        model, profile.drive_models))
                    limits = options['library_limits'][model]
                    layout = personalities.library_layout(profile.library_vendor, model)
                    self.assertEqual(limits['max_slots'], layout.max_slots)
                    self.assertLessEqual(limits['default_num_maps'], layout.max_maps)
            for drive in profile.drive_models:
                with self.subTest(profile=key, drive=drive):
                    self.assertEqual(options['read_only_media'][drive],
                                     personalities.read_only_media(drive))
                    self.assertNotEqual(options['drive_personality'][drive],
                                        personalities.GENERIC_DRIVE)

    def test_l700_does_not_default_to_dlt(self):
        from apps.libraries.services.profiles.data import get_profile_options
        self.assertEqual(get_profile_options('STK')['default_drive_by_library']['L700'],
                         'ULT3580-TDA')

    def test_the_form_carries_no_fixed_limits(self):
        from django.template.loader import get_template
        source = get_template('libraries/brand_config.html').template.source
        for token in ('max="64"', '15000', '15,000'):
            self.assertNotIn(token, source)
        self.assertIn('name="map_count"', source)
        self.assertIn('default_drive_by_library', source)
        self.assertIn('library_limits', source)

    def test_the_view_sends_map_count_not_num_maps(self):
        import inspect
        from apps.libraries import views
        source = inspect.getsource(views.BrandConfigView.post)
        self.assertNotIn("'num_maps'", source)
        self.assertIn("library_data['map_count']", source)
