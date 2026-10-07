Command line
============

The ``mhvtl`` command, ``mhvtl_cli``. It holds no logic: each command parses
arguments, calls one service method and prints the result.

The ``--version`` flag answers with the console's version, its author and its
licence, all of it from ``services.about``:

.. code:: console

   $ mhvtl --version
   mhvtl-gui 3.1.0
   Ahmed Abdelhaleem Ahmed <ahmedhal@gmail.com>
   GPL-2.0-only
   https://github.com/abdelhaleemahmed/mhvtl-console

Its argparse action defers that import until the flag is used, because
``build_parser()`` runs before Django is set up.

``main``
--------

Entry point and argument parser.

.. automodule:: mhvtl_cli.main
   :members:
   :undoc-members:
   :show-inheritance:

``output``
----------

Printing: human tables by default, JSON on request.

.. automodule:: mhvtl_cli.output
   :members:
   :undoc-members:
   :show-inheritance:

``colour``
----------

The only file here that contains an escape sequence. It renders the tokens
``services.tapes.palette`` decides, and is off unless the terminal can show
them apart.

.. automodule:: mhvtl_cli.colour
   :members:
   :undoc-members:
   :show-inheritance:

``privileges``
--------------

Who is allowed to run a mutating command.

.. automodule:: mhvtl_cli.privileges
   :members:
   :undoc-members:
   :show-inheritance:

``prompt``
----------

The only file here that reads from stdin, for ``library create
--interactive``. It decides nothing about libraries: the options come from
the profiles catalogue, the defaults from ``spec.apply_defaults`` and the
check from ``lifecycle.preview``.

.. automodule:: mhvtl_cli.prompt
   :members:
   :undoc-members:
   :show-inheritance:

``runs``
--------

``--drive MODEL[:COUNT]`` and ``--media DENSITY[:COUNT]``: the repeatable
flags that ask for a library holding more than one kind of drive or
cartridge. ``library create`` and ``preset set`` both register them from
here, so the flag is spelled once and parsed once - and it refuses a count
beside a list, which is two answers to one question.

.. automodule:: mhvtl_cli.runs
   :members:
   :undoc-members:
   :show-inheritance:

``sizes``
---------

``--size-mb 12TB``: the argparse type behind every flag that takes a
cartridge's capacity, so ``tape create``, ``tape bulk`` and
``library create`` accept what ``mhvtl settings set`` accepts. It wraps the
service's parser and re-raises its complaint as an ``ArgumentTypeError``,
which argparse prints as written.

.. automodule:: mhvtl_cli.sizes
   :members:
   :undoc-members:
   :show-inheritance:

``config``
----------

list, show, validate, export, backup, restore, regenerate, sync.

.. automodule:: mhvtl_cli.commands.config
   :members:
   :undoc-members:
   :show-inheritance:

``console``
-----------

logs, modules, disk, system.

.. automodule:: mhvtl_cli.commands.console
   :members:
   :undoc-members:
   :show-inheritance:

``drive``
---------

list, show, add, remove.

.. automodule:: mhvtl_cli.commands.drive
   :members:
   :undoc-members:
   :show-inheritance:

``iscsi``
---------

status, targets, backstores, export, remap, target, lun, acl, portal, service.

.. automodule:: mhvtl_cli.commands.iscsi
   :members:
   :undoc-members:
   :show-inheritance:

``library``
-----------

list, show, create, delete, orphans, next-id, slots.

.. automodule:: mhvtl_cli.commands.library
   :members:
   :undoc-members:
   :show-inheritance:

``preset``
----------

list, show, set, unset, delete. The configurations an operator built, which
are profiles they wrote themselves.

.. automodule:: mhvtl_cli.commands.preset
   :members:
   :undoc-members:
   :show-inheritance:

``profile``
-----------

list, show. The vendor catalogues, and the only noun with no write verbs:
nothing may edit what a vendor makes.

.. automodule:: mhvtl_cli.commands.profile
   :members:
   :undoc-members:
   :show-inheritance:

``ltfs``
--------

tapes, drives, mount, unmount, check, format, support. Its own noun because
``op mount`` already means the robot putting a cartridge into a drive, and
this is a filesystem over one already there.

.. automodule:: mhvtl_cli.commands.ltfs
   :members:
   :undoc-members:
   :show-inheritance:

``operations``
--------------

mount, unmount, move, online, offline, map, inventory. ``mount`` refuses a
tape the drive's MHVTL personality does not load; ``--force`` mounts it anyway.

.. automodule:: mhvtl_cli.commands.operations
   :members:
   :undoc-members:
   :show-inheritance:

``scsi``
--------

devices, map.

.. automodule:: mhvtl_cli.commands.scsi
   :members:
   :undoc-members:
   :show-inheritance:

``service``
-----------

start, stop, restart, status.

.. automodule:: mhvtl_cli.commands.service
   :members:
   :undoc-members:
   :show-inheritance:

``settings``
------------

list, get, set, reset - the console's own preferences, by dotted keys that are
the path through ``/etc/mhvtl-gui/settings.toml``.

.. automodule:: mhvtl_cli.commands.settings
   :members:
   :undoc-members:
   :show-inheritance:

``status``
----------

library, drive, dashboard, system.

.. automodule:: mhvtl_cli.commands.status
   :members:
   :undoc-members:
   :show-inheritance:

``tape``
--------

list, media, create, bulk, delete, next-barcode, slots. ``media`` shows which
densities a library's drives load; ``create`` and ``bulk`` take ``--density``
and ``--kind`` and refuse a density no drive loads.

.. automodule:: mhvtl_cli.commands.tape
   :members:
   :undoc-members:
   :show-inheritance:
