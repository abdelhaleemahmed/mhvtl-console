"""MHVTL service layer.

One package per domain role. A caller - the web views, the CLI, a test - depends
on these packages and nothing depends on the caller.

    core/        shared plumbing (results, shell, locking, retry, paths)
    config/      device.conf and library_contents: parse, render, inventory
    libraries/   library CRUD and the create workflow
    drives/      drive CRUD
    tapes/       tape media and barcodes
    operations/  mount, unmount, move, online, offline
    scsi/        discovery and device mapping
    console/     host, units, modules, logs, disk
    iscsi/       targets, LUNs, ACLs, export
    dashboard/   overview aggregation
    sync/        config to database (the only ORM user)
    profiles/    vendor reference data and MHVTL's personalities
    verification/ the end-to-end backup/restore test

Layer rules, in one place so they are easy to check in review:

    1. Every public function returns a ServiceResult (core.results).
    2. No service imports django.contrib.messages, HttpRequest or anything
       from the view layer.
    3. Every subprocess goes through core.shell. No shell=True, ever.
    4. Only sync/ imports apps.libraries.models.
    5. Paths come from core.paths, which reads settings; never hardcode
       /etc/mhvtl or /opt/mhvtl.
    6. Config writes go through core.locking: lock, write temp, fsync, rename.
    7. THE DEPENDENCY GRAPH IS A DAG, and it points one way: from the packages
       that decide towards the packages that describe. tests/test_service_layers.py
       computes it from the imports and fails on a cycle, so this is checked
       rather than remembered.

           core          (nothing)
           profiles      (nothing)
           config        core, profiles
           sync          config
           console       config, core
           scsi          config, core
           tapes         config, core, profiles
           operations    config, core, profiles, scsi, tapes
           ltfs          core, operations, profiles, scsi, tapes
           iscsi         config, console, core, scsi
           drives        config, console, core, iscsi, operations, profiles, sync
           verification  config, core, operations, scsi
           libraries     config, console, core, drives, iscsi, operations,
                         profiles, sync, tapes
           dashboard     config, console, libraries

       Two consequences worth stating, because both have nearly been broken:

       tapes/ IS LOW. It imports config, core and profiles, and nothing else.
       It must not import operations/, scsi/, drives/ or ltfs/. When tapes/
       needs to know something about a drive - whether LTFS would open it, say -
       it asks profiles/ (a table) and config/ (the file), never operations/,
       which imports tapes/ and would close a cycle.

       NOTHING IMPORTS ltfs/ EXCEPT A CALLER. It is a leaf: the web views and
       the CLI call it, and no service package does. LTFS is a feature layered
       on tapes, not a thing tapes knows about.

STATUS: live. Every view, AJAX endpoint, management command and the mhvtl CLI
call these packages directly; the old adapters package is gone.
docs/sphinx/guides/refactoring.rst records how the layer got here.
"""
