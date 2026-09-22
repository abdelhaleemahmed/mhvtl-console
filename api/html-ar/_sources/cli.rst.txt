Command line
============

The ``mhvtl`` command, ``mhvtl_cli``. It holds no logic: each command parses
arguments, calls one service method and prints the result.

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

``privileges``
--------------

Who is allowed to run a mutating command.

.. automodule:: mhvtl_cli.privileges
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

list, show, create, update, delete, orphans, next-id.

.. automodule:: mhvtl_cli.commands.library
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
