Services
========

The service layer, ``apps/libraries/services``: one package per domain role.
The web views and the ``mhvtl`` command line call it directly; it
never sees a request and returns a ``ServiceResult`` from ``core.results``.
Most of its modules open with the rules the layer keeps, under *Rules for
this layer*.

``services.core``
-----------------

Shared plumbing: results, shell execution, locking, retries, paths, errors.

``errors``
~~~~~~~~~~

Typed failures, so callers stop catching bare Exception.

.. automodule:: apps.libraries.services.core.errors
   :members:
   :undoc-members:
   :show-inheritance:

``locking``
~~~~~~~~~~~

Serialised, atomic writes to the MHVTL config files.

.. automodule:: apps.libraries.services.core.locking
   :members:
   :undoc-members:
   :show-inheritance:

``paths``
~~~~~~~~~

Where MHVTL keeps things, read from settings instead of hardcoded.

.. automodule:: apps.libraries.services.core.paths
   :members:
   :undoc-members:
   :show-inheritance:

``results``
~~~~~~~~~~~

The result type every service returns.

.. automodule:: apps.libraries.services.core.results
   :members:
   :undoc-members:
   :show-inheritance:

``retry``
~~~~~~~~~

Retrying SCSI operations that report a transient busy state.

.. automodule:: apps.libraries.services.core.retry
   :members:
   :undoc-members:
   :show-inheritance:

``shell``
~~~~~~~~~

The only place this project runs a subprocess.

.. automodule:: apps.libraries.services.core.shell
   :members:
   :undoc-members:
   :show-inheritance:

``services.config``
-------------------

Reading and writing the MHVTL configuration files.

``device_conf``
~~~~~~~~~~~~~~~

Read and write /etc/mhvtl/device.conf.

.. automodule:: apps.libraries.services.config.device_conf
   :members:
   :undoc-members:
   :show-inheritance:

``ids``
~~~~~~~

Handing out library ids, drive ids and SCSI targets.

.. automodule:: apps.libraries.services.config.ids
   :members:
   :undoc-members:
   :show-inheritance:

``inventory``
~~~~~~~~~~~~~

Listing, reading and exporting the MHVTL configuration directory.

.. automodule:: apps.libraries.services.config.inventory
   :members:
   :undoc-members:
   :show-inheritance:

``library_contents``
~~~~~~~~~~~~~~~~~~~~

Read and write library_contents.N - a library's slot inventory.

.. automodule:: apps.libraries.services.config.library_contents
   :members:
   :undoc-members:
   :show-inheritance:

``service``
~~~~~~~~~~~

Config orchestration: read, back up, write, restore.

.. automodule:: apps.libraries.services.config.service
   :members:
   :undoc-members:
   :show-inheritance:

``services.profiles``
---------------------

Vendor and model reference data. Pure, no I/O.

``data``
~~~~~~~~

Vendor and model reference data.

.. automodule:: apps.libraries.services.profiles.data
   :members:
   :undoc-members:
   :show-inheritance:

``personalities``
~~~~~~~~~~~~~~~~~

What MHVTL 1.8 will actually emulate for a given device.conf entry.

.. automodule:: apps.libraries.services.profiles.personalities
   :members:
   :undoc-members:
   :show-inheritance:

``services.libraries``
----------------------

Library lifecycle: list, create, update, delete, orphans.

``lifecycle``
~~~~~~~~~~~~~

Creating and deleting a library: the paths that write device.conf.

.. automodule:: apps.libraries.services.libraries.lifecycle
   :members:
   :undoc-members:
   :show-inheritance:

``models``
~~~~~~~~~~

Library dataclasses.

.. automodule:: apps.libraries.services.libraries.models
   :members:
   :undoc-members:
   :show-inheritance:

``orphans``
~~~~~~~~~~~

Leftovers: things one part of the system believes in and another does not.

.. automodule:: apps.libraries.services.libraries.orphans
   :members:
   :undoc-members:
   :show-inheritance:

``service``
~~~~~~~~~~~

Library listing and lifecycle.

.. automodule:: apps.libraries.services.libraries.service
   :members:
   :undoc-members:
   :show-inheritance:

``spec``
~~~~~~~~

Filling in a library specification from its vendor profile.

.. automodule:: apps.libraries.services.libraries.spec
   :members:
   :undoc-members:
   :show-inheritance:

``validation``
~~~~~~~~~~~~~~

Checking a library specification before anything is written.

.. automodule:: apps.libraries.services.libraries.validation
   :members:
   :undoc-members:
   :show-inheritance:

``workflow``
~~~~~~~~~~~~

Creating a library and making it real.

.. automodule:: apps.libraries.services.libraries.workflow
   :members:
   :undoc-members:
   :show-inheritance:

``services.drives``
-------------------

Tape drive lifecycle. This role had no module before the refactor.

``models``
~~~~~~~~~~

Drive dataclasses.

.. automodule:: apps.libraries.services.drives.models
   :members:
   :undoc-members:
   :show-inheritance:

``service``
~~~~~~~~~~~

Drive CRUD - the role that had no module before the refactor.

.. automodule:: apps.libraries.services.drives.service
   :members:
   :undoc-members:
   :show-inheritance:

``services.tapes``
------------------

Tape media: barcodes, densities, and the files under the media directory.

``barcodes``
~~~~~~~~~~~~

Barcodes, prefixes and media densities.

.. automodule:: apps.libraries.services.tapes.barcodes
   :members:
   :undoc-members:
   :show-inheritance:

``compatibility``
~~~~~~~~~~~~~~~~~

Can this cartridge go in that drive?

.. automodule:: apps.libraries.services.tapes.compatibility
   :members:
   :undoc-members:
   :show-inheritance:

``media``
~~~~~~~~~

The tape files on disk, under the media directory.

.. automodule:: apps.libraries.services.tapes.media
   :members:
   :undoc-members:
   :show-inheritance:

``models``
~~~~~~~~~~

Tape dataclasses.

.. automodule:: apps.libraries.services.tapes.models
   :members:
   :undoc-members:
   :show-inheritance:

``service``
~~~~~~~~~~~

Tape create, bulk create, delete and list.

.. automodule:: apps.libraries.services.tapes.service
   :members:
   :undoc-members:
   :show-inheritance:

``services.operations``
-----------------------

Moving tapes around: mtx, mt and vtlcmd.

``mounting``
~~~~~~~~~~~~

What the mount page reads: the slot map, each drive's model, and every
loaded cartridge's verdict for every drive.

.. automodule:: apps.libraries.services.operations.mounting
   :members:
   :undoc-members:
   :show-inheritance:

``mt``
~~~~~~

The mt wrapper - drive-level status.

.. automodule:: apps.libraries.services.operations.mt
   :members:
   :undoc-members:
   :show-inheritance:

``mtx``
~~~~~~~

The mtx wrapper and its output parser.

.. automodule:: apps.libraries.services.operations.mtx
   :members:
   :undoc-members:
   :show-inheritance:

``service``
~~~~~~~~~~~

Mount, unmount, move, online, offline.

.. automodule:: apps.libraries.services.operations.service
   :members:
   :undoc-members:
   :show-inheritance:

``vtlcmd``
~~~~~~~~~~

The vtlcmd wrapper - MHVTL's message-queue control channel.

.. automodule:: apps.libraries.services.operations.vtlcmd
   :members:
   :undoc-members:
   :show-inheritance:

``services.scsi``
-----------------

SCSI discovery, and mapping a library or drive to its device node.

``lsscsi``
~~~~~~~~~~

Parsing lsscsi output.

.. automodule:: apps.libraries.services.scsi.lsscsi
   :members:
   :undoc-members:
   :show-inheritance:

``mapping``
~~~~~~~~~~~

Which /dev node belongs to which library or drive.

.. automodule:: apps.libraries.services.scsi.mapping
   :members:
   :undoc-members:
   :show-inheritance:

``models``
~~~~~~~~~~

SCSI dataclasses.

.. automodule:: apps.libraries.services.scsi.models
   :members:
   :undoc-members:
   :show-inheritance:

``services.console``
--------------------

Host and service state for the console pages.

``disk``
~~~~~~~~

Disk usage for the configuration and media directories.

.. automodule:: apps.libraries.services.console.disk
   :members:
   :undoc-members:
   :show-inheritance:

``logs``
~~~~~~~~

Reading log files and dmesg.

.. automodule:: apps.libraries.services.console.logs
   :members:
   :undoc-members:
   :show-inheritance:

``metrics``
~~~~~~~~~~~

Per-library metrics: is the daemon up, what is it costing, and where.

.. automodule:: apps.libraries.services.console.metrics
   :members:
   :undoc-members:
   :show-inheritance:

``modules``
~~~~~~~~~~~

Kernel module state.

.. automodule:: apps.libraries.services.console.modules
   :members:
   :undoc-members:
   :show-inheritance:

``service``
~~~~~~~~~~~

The facade the console pages call.

.. automodule:: apps.libraries.services.console.service
   :members:
   :undoc-members:
   :show-inheritance:

``system``
~~~~~~~~~~

Host facts: kernel, uptime, load, memory.

.. automodule:: apps.libraries.services.console.system
   :members:
   :undoc-members:
   :show-inheritance:

``units``
~~~~~~~~~

systemd state for the MHVTL units, and starting and stopping them.

.. automodule:: apps.libraries.services.console.units
   :members:
   :undoc-members:
   :show-inheritance:

``services.iscsi``
------------------

Exporting libraries and drives over iSCSI, through targetcli.

``models``
~~~~~~~~~~

iSCSI dataclasses.

.. automodule:: apps.libraries.services.iscsi.models
   :members:
   :undoc-members:
   :show-inheritance:

``parsing``
~~~~~~~~~~~

Reading LIO's configuration: saveconfig.json and `targetcli ls`.

.. automodule:: apps.libraries.services.iscsi.parsing
   :members:
   :undoc-members:
   :show-inheritance:

``remap``
~~~~~~~~~

Keeping saved pscsi backstores pointed at the right devices across reboots.

.. automodule:: apps.libraries.services.iscsi.remap
   :members:
   :undoc-members:
   :show-inheritance:

``service``
~~~~~~~~~~~

iSCSI orchestration: status, targets, LUNs, ACLs, exporting a library.

.. automodule:: apps.libraries.services.iscsi.service
   :members:
   :undoc-members:
   :show-inheritance:

``targetcli``
~~~~~~~~~~~~~

The targetcli wrapper, and the checks that belong in front of it.

.. automodule:: apps.libraries.services.iscsi.targetcli
   :members:
   :undoc-members:
   :show-inheritance:

``workflow``
~~~~~~~~~~~~

Exporting a whole library over iSCSI, in one step.

.. automodule:: apps.libraries.services.iscsi.workflow
   :members:
   :undoc-members:
   :show-inheritance:

``services.verification``
-------------------------

Proving a library holds what is written to it.

``data``
~~~~~~~~

Test data: generating it, checksumming it, and checking it came back.

.. automodule:: apps.libraries.services.verification.data
   :members:
   :undoc-members:
   :show-inheritance:

``models``
~~~~~~~~~~

What a verification run reports.

.. automodule:: apps.libraries.services.verification.models
   :members:
   :undoc-members:
   :show-inheritance:

``service``
~~~~~~~~~~~

The facade: run a verification, and report it as a ServiceResult.

.. automodule:: apps.libraries.services.verification.service
   :members:
   :undoc-members:
   :show-inheritance:

``tape_io``
~~~~~~~~~~~

Writing an archive to a tape device and reading it back.

.. automodule:: apps.libraries.services.verification.tape_io
   :members:
   :undoc-members:
   :show-inheritance:

``workflow``
~~~~~~~~~~~~

End-to-end verification: does this library actually hold data?

.. automodule:: apps.libraries.services.verification.workflow
   :members:
   :undoc-members:
   :show-inheritance:

``services.dashboard``
----------------------

Aggregated overview data for the dashboards.

``service``
~~~~~~~~~~~

Overview data for the dashboard tiles.

.. automodule:: apps.libraries.services.dashboard.service
   :members:
   :undoc-members:
   :show-inheritance:

``services.sync``
-----------------

Configuration files to database - the only package here that touches the ORM.

``service``
~~~~~~~~~~~

Configuration files to database.

.. automodule:: apps.libraries.services.sync.service
   :members:
   :undoc-members:
   :show-inheritance:
