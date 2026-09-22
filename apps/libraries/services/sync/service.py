"""Configuration files to database.

Moved from services/sync_service.py. It read device.conf through the old
library adapter, which answered an unreadable file with an empty list - and an
empty list here deactivates every library in the database. It reads the file
through ConfigService now, and an unreadable file is an error.

This is the one module in services permitted to import apps.libraries.models,
and the boundary is deliberate: the config files are authoritative and the
database is a cache of them. Everything else in the layer stays ORM-free so the
CLI can call it without Django's app registry being loaded.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models  <- this module is the exception
"""
import logging

from django.utils import timezone

from apps.libraries.models import Library, LibraryBrand, LibraryModel, Drive

from ..config.service import ConfigService

logger = logging.getLogger(__name__)


class ConfigUnreadable(RuntimeError):
    """device.conf could not be read; nothing was changed."""


def sync_mhvtl_to_django(config_directory=None):
    """
    Sync MHVTL device.conf into the Django database.

    - Creates Library and Drive records for MHVTL libraries not yet in the DB.
    - Activates DB libraries that exist in device.conf but are inactive.
    - Deactivates DB libraries that are active but missing from device.conf.
    - Brings an existing row back in line with device.conf. A library deleted
      and recreated keeps its id, so the row described the library that used
      to have that id - the wrong vendor, product, brand and SCSI address on
      every page that reads the database.
    - For every library device.conf declares, makes its database drives match:
      missing ones are created, inactive ones reactivated, ones device.conf no
      longer lists deactivated. It used to import drives only when it created
      the library, so a library deleted and recreated kept its drives inactive
      and the drive pages showed none.

    Returns:
        dict with keys: libraries_found, created, updated, drives_imported,
        drives_activated, drives_deactivated, activated, deactivated,
        total_db, total_drives

    Raises ConfigUnreadable, and changes nothing, when device.conf cannot be
    read.
    """
    conf = ConfigService(config_directory).device_conf()
    if conf is None:
        raise ConfigUnreadable('device.conf could not be read; the database '
                               'was left as it is')
    mhvtl_ids = set(conf.libraries)
    db_ids = set(Library.objects.values_list('library_id', flat=True))

    created_count = 0
    drives_count = 0

    # Create libraries that exist in device.conf but not in Django DB
    for library_id, lib in sorted(conf.libraries.items()):
        if library_id in db_ids:
            continue

        vendor = lib.get('vendor') or 'Unknown'
        product = lib.get('product') or 'Unknown'
        brand, _ = LibraryBrand.objects.get_or_create(
            name=vendor.upper(),
            defaults={'display_name': vendor}
        )
        model, _ = LibraryModel.objects.get_or_create(
            brand=brand,
            name=product,
            defaults={'product_identification': product}
        )

        db_lib = Library.objects.create(
            library_id=library_id,
            channel=lib.get('channel') or 0,
            target=lib.get('target') or 0,
            lun=lib.get('lun') or 0,
            brand=brand,
            model=model,
            vendor_identification=vendor,
            product_identification=product,
            unit_serial_number=lib.get('serial') or '',
            naa=lib.get('naa'),
            home_directory=lib.get('home_directory'),
            discovery_status='discovered',
            config_source='device.conf',
            is_active=True,
        )

        drives_count += _import_drives(db_lib, conf.drives_of(library_id))
        created_count += 1
        logger.info('Created library %s: %s %s', library_id, vendor, product)

    # Bring existing rows back in line with what device.conf now says.
    # The slot counts come from library_contents, not device.conf, and they
    # matter most when an id is reused: the row for a deleted library 40 kept
    # the old one's slots and sync time, and the detail page showed them.
    config = ConfigService(config_directory)
    updated = 0
    for db_lib in Library.objects.filter(library_id__in=mhvtl_ids):
        contents = config.library_contents(db_lib.library_id)
        if _refresh(db_lib, conf.libraries[db_lib.library_id], contents):
            updated += 1

        # Activate libraries in device.conf that are inactive in Django
    activated = Library.objects.filter(
        library_id__in=mhvtl_ids, is_active=False
    ).update(is_active=True)

    # Deactivate libraries not in device.conf
    deactivated = Library.objects.filter(
        is_active=True
    ).exclude(library_id__in=mhvtl_ids).update(is_active=False)

    # Drives: the database follows device.conf for every declared library
    drives_activated = drives_deactivated = 0
    for db_lib in Library.objects.filter(library_id__in=mhvtl_ids):
        declared = conf.drives_of(db_lib.library_id)
        drives_count += _import_drives(db_lib, declared)
        drives_activated += db_lib.drives.filter(
            drive_id__in=list(declared), is_active=False).update(is_active=True)
        drives_deactivated += db_lib.drives.filter(is_active=True).exclude(
            drive_id__in=list(declared)).update(is_active=False)
    drives_deactivated += Drive.objects.filter(is_active=True).exclude(
        library__library_id__in=mhvtl_ids).update(is_active=False)

    return {
        'libraries_found': len(mhvtl_ids),
        'created': created_count,
        'updated': updated,
        'drives_imported': drives_count,
        'drives_activated': drives_activated,
        'drives_deactivated': drives_deactivated,
        'activated': activated,
        'deactivated': deactivated,
        'total_db': Library.objects.filter(is_active=True).count(),
        'total_drives': Drive.objects.filter(is_active=True).count(),
    }


def _import_drives(db_lib, declared) -> int:
    """Create the database drives device.conf declares and the database lacks."""
    existing = set(db_lib.drives.values_list('drive_id', flat=True))
    created = 0
    for drive_id, d in sorted(declared.items()):
        if drive_id in existing:
            continue
        Drive.objects.create(
            library=db_lib,
            drive_id=drive_id,
            channel=d.get('channel') or 0,
            target=d.get('target') or 0,
            lun=d.get('lun') or 0,
            vendor_identification=d.get('vendor'),
            product_identification=d.get('product'),
            product_revision='1068',
            unit_serial_number=d.get('serial'),
            discovery_status='discovered',
            config_source='device.conf',
            is_active=True,
        )
        created += 1
    return created


def _refresh(db_lib, declared, contents=None) -> bool:
    """Make one database row match its device.conf record. True if it changed.

    contents is the library's parsed library_contents, where the slots live;
    without it the stored counts are left alone rather than zeroed.
    """
    vendor = declared.get('vendor') or 'Unknown'
    product = declared.get('product') or 'Unknown'
    fields = {
        'vendor_identification': vendor,
        'product_identification': product,
        'unit_serial_number': declared.get('serial') or '',
        'channel': declared.get('channel') or 0,
        'target': declared.get('target') or 0,
        'lun': declared.get('lun') or 0,
        'naa': declared.get('naa'),
        'home_directory': declared.get('home_directory'),
    }
    if contents is not None:
        summary = contents.summary()
        fields['media_count'] = summary['full_slots']
        fields['empty_slots'] = summary['empty_slots']
    changed = [name for name, value in fields.items()
               if getattr(db_lib, name) != value]

    brand, _ = LibraryBrand.objects.get_or_create(
        name=vendor.upper(), defaults={'display_name': vendor})
    model, _ = LibraryModel.objects.get_or_create(
        brand=brand, name=product, defaults={'product_identification': product})
    if db_lib.brand_id != brand.id:
        fields['brand'] = brand
        changed.append('brand')
    if db_lib.model_id != model.id:
        fields['model'] = model
        changed.append('model')

    if not changed:
        # Still synced now, even when nothing moved: "last synchronized 7
        # months ago" on a library that is being read every day is wrong.
        db_lib.last_synced = timezone.now()
        db_lib.save(update_fields=['last_synced'])
        return False
    fields['last_synced'] = timezone.now()
    for name, value in fields.items():
        setattr(db_lib, name, value)
    db_lib.save(update_fields=list(fields))
    logger.info('library %s updated from device.conf: %s',
                db_lib.library_id, ', '.join(sorted(set(changed))))
    return True
