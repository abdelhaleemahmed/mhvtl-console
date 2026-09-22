"""
Phase 2: Model Testing
Following the Comprehensive Testing Plan
Testing all 6 models with their actual fields and relationships
"""
from django.test import TestCase
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.contrib.auth import get_user_model
from apps.libraries.models import (
    LibraryBrand,
    LibraryModel,
    Library,
    Drive,
    MediaSlot,
    LibraryOperation
)
from datetime import datetime

User = get_user_model()


def make_library(brand, model, library_id=10, **kwargs):
    """Helper to create a Library with all required fields."""
    defaults = dict(
        library_id=library_id,
        channel=0,
        target=0,
        lun=0,
        brand=brand,
        model=model,
        vendor_identification=brand.name,
        product_identification=model.name,
    )
    defaults.update(kwargs)
    return Library.objects.create(**defaults)


def make_drive(library, drive_id, **kwargs):
    """Helper to create a Drive with all required fields."""
    defaults = dict(
        library=library,
        drive_id=drive_id,
        channel=0,
        target=drive_id,
        lun=0,
        vendor_identification='TEST',
        product_identification='ULTRIUM-8',
        product_revision='1.0',
        unit_serial_number=f'DRV{drive_id:05d}',
    )
    defaults.update(kwargs)
    return Drive.objects.create(**defaults)


class LibraryBrandModelTest(TestCase):
    """Test LibraryBrand model functionality"""

    def setUp(self):
        """Create test brand"""
        self.brand = LibraryBrand.objects.create(
            name='STK',
            display_name='StorageTek',
            color_code='#FF5733',
            is_active=True
        )

    def test_brand_creation(self):
        """Test brand is created with all required fields"""
        self.assertEqual(self.brand.name, 'STK')
        self.assertEqual(self.brand.display_name, 'StorageTek')
        self.assertTrue(self.brand.is_active)
        self.assertIsNotNone(self.brand.created_at)

    def test_brand_str_representation(self):
        """Test string representation of brand"""
        brand_str = str(self.brand)
        self.assertIsNotNone(brand_str)

    def test_create_all_vendor_brands(self):
        """Test creating all 9 vendor brands from the plan"""
        brands = ['IBM', 'HP', 'Quantum', 'Spectra', 'ADIC', 'Sony', 'Dell', 'Overland']
        for brand_name in brands:
            brand = LibraryBrand.objects.create(
                name=brand_name,
                display_name=f'{brand_name} Libraries',
                is_active=True
            )
            self.assertTrue(brand.id)

        # Including STK from setUp, should have 9 total
        self.assertEqual(LibraryBrand.objects.count(), 9)


class LibraryModelModelTest(TestCase):
    """Test LibraryModel model functionality"""

    def setUp(self):
        """Create test brand and model"""
        self.brand = LibraryBrand.objects.create(
            name='HP',
            display_name='HP Libraries'
        )
        self.model = LibraryModel.objects.create(
            brand=self.brand,
            name='MSL G3 Series',
            product_identification='MSL-G3',
            default_revision='1.0',
            is_active=True
        )

    def test_model_creation(self):
        """Test model is created with all fields"""
        self.assertEqual(self.model.brand.name, 'HP')
        self.assertEqual(self.model.name, 'MSL G3 Series')
        self.assertEqual(self.model.product_identification, 'MSL-G3')
        self.assertTrue(self.model.is_active)

    def test_brand_relationship(self):
        """Test foreign key relationship to LibraryBrand"""
        self.assertEqual(self.model.brand, self.brand)
        self.assertIn(self.model, self.brand.models.all())


class LibraryModelTest(TestCase):
    """Test Library model - the main model with 25 fields"""

    def setUp(self):
        """Create test library"""
        self.brand = LibraryBrand.objects.create(
            name='IBM',
            display_name='IBM Libraries'
        )
        self.model = LibraryModel.objects.create(
            brand=self.brand,
            name='TS3500'
        )

    def test_library_creation_minimal(self):
        """Test library creation with minimal required fields"""
        library = make_library(self.brand, self.model, library_id=10)
        self.assertEqual(library.library_id, 10)
        self.assertEqual(library.brand, self.brand)
        self.assertEqual(library.model, self.model)

    def test_library_creation_full(self):
        """Test library creation with all fields"""
        library = Library.objects.create(
            library_id=20,
            channel=0,
            target=0,
            lun=0,
            brand=self.brand,
            model=self.model,
            vendor_identification='IBM',
            product_identification='TS3500',
            product_revision='2.0',
            unit_serial_number='IBM123456',
            naa='naa.5000000000000001',
            home_directory='/opt/mhvtl',
            media_count=100,
            empty_slots=20,
            is_active=True,
            is_standard_config=True,
            discovery_status='created'
        )

        self.assertEqual(library.library_id, 20)
        self.assertEqual(library.vendor_identification, 'IBM')
        self.assertEqual(library.media_count, 100)
        self.assertEqual(library.empty_slots, 20)
        self.assertTrue(library.is_active)

    def test_discovery_fields(self):
        """Test discovery integration fields"""
        library = make_library(self.brand, self.model, library_id=30,
            discovery_status='discovered',
            mhvtl_id='MHVTL_30',
            config_source='/etc/mhvtl/device.conf'
        )

        self.assertEqual(library.discovery_status, 'discovered')
        self.assertEqual(library.mhvtl_id, 'MHVTL_30')
        self.assertEqual(library.config_source, '/etc/mhvtl/device.conf')

    def test_timestamps(self):
        """Test created_at and updated_at fields"""
        library = make_library(self.brand, self.model, library_id=40)

        self.assertIsNotNone(library.created_at)
        self.assertIsNotNone(library.updated_at)

        from django.utils import timezone
        time_diff = timezone.now() - library.created_at
        self.assertLess(time_diff.total_seconds(), 60)


class DriveModelTest(TestCase):
    """Test Drive model with library relationship"""

    def setUp(self):
        """Create test library for drives"""
        self.brand = LibraryBrand.objects.create(name='HP', display_name='HP')
        self.model = LibraryModel.objects.create(
            brand=self.brand,
            name='MSL G3'
        )
        self.library = make_library(self.brand, self.model, library_id=50)

    def test_drive_creation(self):
        """Test drive creation with required fields"""
        drive = make_drive(self.library, drive_id=51)

        self.assertEqual(drive.drive_id, 51)
        self.assertEqual(drive.library, self.library)

    def test_drive_with_scsi_config(self):
        """Test drive with full SCSI configuration"""
        drive = Drive.objects.create(
            library=self.library,
            drive_id=52,
            channel=0,
            target=1,
            lun=0,
            vendor_identification='HP',
            product_identification='ULTRIUM-8',
            product_revision='1.0',
            unit_serial_number='DRV001',
            is_active=True
        )

        self.assertEqual(drive.channel, 0)
        self.assertEqual(drive.target, 1)
        self.assertEqual(drive.vendor_identification, 'HP')
        self.assertTrue(drive.is_active)

    def test_multiple_drives_per_library(self):
        """Test that a library can have multiple drives"""
        for i in range(4):
            make_drive(self.library, drive_id=60 + i, target=i + 1)

        self.assertEqual(self.library.drives.count(), 4)

    def test_drive_discovery_fields(self):
        """Test drive discovery integration fields"""
        drive = make_drive(self.library, drive_id=70,
            discovery_status='discovered',
            mhvtl_id='DRIVE_70',
            config_source='/etc/mhvtl/device.conf'
        )

        self.assertEqual(drive.discovery_status, 'discovered')
        self.assertEqual(drive.mhvtl_id, 'DRIVE_70')


class MediaSlotModelTest(TestCase):
    """Test MediaSlot model"""

    def setUp(self):
        """Create test library for slots"""
        self.brand = LibraryBrand.objects.create(name='STK', display_name='STK')
        self.model = LibraryModel.objects.create(brand=self.brand, name='L700')
        self.library = make_library(self.brand, self.model, library_id=80)

    def test_filled_slot_creation(self):
        """Test creation of a filled media slot"""
        slot = MediaSlot.objects.create(
            library=self.library,
            slot_number=1,
            is_empty=False,
            media_barcode='STK001L8',
            media_type='LTO8'
        )

        self.assertEqual(slot.slot_number, 1)
        self.assertFalse(slot.is_empty)
        self.assertEqual(slot.media_barcode, 'STK001L8')
        self.assertEqual(slot.media_type, 'LTO8')

    def test_empty_slot_creation(self):
        """Test creation of an empty slot"""
        slot = MediaSlot.objects.create(
            library=self.library,
            slot_number=2,
            is_empty=True
        )

        self.assertTrue(slot.is_empty)
        self.assertIsNone(slot.media_barcode)

    def test_import_export_slot(self):
        """Test I/E slot designation"""
        slot = MediaSlot.objects.create(
            library=self.library,
            slot_number=100,
            is_import_export=True
        )

        self.assertTrue(slot.is_import_export)

    def test_slot_discovery_fields(self):
        """Test slot discovery integration"""
        slot = MediaSlot.objects.create(
            library=self.library,
            slot_number=3,
            discovery_status='discovered',
            config_source='/opt/mhvtl/library_contents.10'
        )

        self.assertEqual(slot.discovery_status, 'discovered')
        self.assertEqual(slot.config_source, '/opt/mhvtl/library_contents.10')


class LibraryOperationModelTest(TestCase):
    """Test LibraryOperation audit trail model"""

    def setUp(self):
        """Create test library for operations"""
        self.brand = LibraryBrand.objects.create(name='Quantum', display_name='Quantum')
        self.model = LibraryModel.objects.create(brand=self.brand, name='i6000')
        self.library = make_library(self.brand, self.model, library_id=90)

    def test_operation_creation(self):
        """Test basic operation logging"""
        operation = LibraryOperation.objects.create(
            library=self.library,
            operation='CREATE',
            description='Library created via Django',
            user_session='test-session-1'
        )

        self.assertEqual(operation.library, self.library)
        self.assertEqual(operation.operation, 'CREATE')
        self.assertIsNotNone(operation.timestamp)

    def test_operation_with_media_info(self):
        """Test operation with media tracking"""
        operation = LibraryOperation.objects.create(
            library=self.library,
            operation='MOVE',
            description='Moved tape from slot 1 to slot 5',
            user_session='test-session-2',
            source_slot=1,
            target_slot=5,
            media_barcode='QTM001L8'
        )

        self.assertEqual(operation.source_slot, 1)
        self.assertEqual(operation.target_slot, 5)
        self.assertEqual(operation.media_barcode, 'QTM001L8')

    def test_discovery_session_tracking(self):
        """Test discovery session correlation"""
        operation = LibraryOperation.objects.create(
            library=self.library,
            operation='SYNC',
            description='Synchronized with MHVTL',
            user_session='test-session-3',
            discovery_session_id='SESSION_123'
        )

        self.assertEqual(operation.discovery_session_id, 'SESSION_123')


class ModelRelationshipTest(TestCase):
    """Test all model relationships and cascade behaviors"""

    def test_library_cascade_delete(self):
        """Test that deleting library deletes related objects"""
        brand = LibraryBrand.objects.create(name='Test', display_name='Test')
        model = LibraryModel.objects.create(brand=brand, name='TestModel')
        library = make_library(brand, model, library_id=100)

        drive = make_drive(library, drive_id=101)
        slot = MediaSlot.objects.create(library=library, slot_number=1)
        operation = LibraryOperation.objects.create(
            library=library,
            operation='CREATE',
            description='test',
            user_session='test-session'
        )

        drive_id = drive.id
        slot_id = slot.id
        operation_id = operation.id

        library.delete()

        self.assertFalse(Drive.objects.filter(id=drive_id).exists())
        self.assertFalse(MediaSlot.objects.filter(id=slot_id).exists())
        self.assertFalse(LibraryOperation.objects.filter(id=operation_id).exists())

        # Brand and model should still exist
        self.assertTrue(LibraryBrand.objects.filter(id=brand.id).exists())
        self.assertTrue(LibraryModel.objects.filter(id=model.id).exists())
