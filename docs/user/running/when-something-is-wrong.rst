When something is wrong
=======================

The usual causes, what each looks like, and the command that settles it.

.. contents::
   :local:
   :depth: 1

Three answers that disagree
---------------------------

A drive's state can be asked three ways, and they do not always agree. This
is the single most confusing thing about MHVTL, and it is worth knowing
before it happens:

.. code-block:: console

   $ sudo mhvtl status drive 31         # through mt - the kernel's tape driver
   tape loaded       no

   $ sudo mhvtl status activity 30      # through the drive daemon
   31     holding  G03001TA  0 B written - 0.0% of the tape

   $ sudo mhvtl status library 30       # through the robot, mtx
   0      yes     G03001TA  1

That was a real drive with a real cartridge in it, which wrote and read back
perfectly. ``mt`` was simply stale.

**Believe the robot and the daemon.** The robot moved the cartridge, the
daemon is holding it, and the daemon's counters are the only ones that keep
working during a backup - the kernel gives the SCSI reservation to whoever is
writing, and ``mt`` and ``mtx`` answer *device busy* until it finishes.

"Restart the library for its robot to see this"
-----------------------------------------------

MHVTL reads its configuration **once**, when a daemon starts. Adding a tape,
adopting one, or changing the slot count edits the file, and the running
robot knows nothing about it:

.. code-block:: console

   $ sudo mhvtl tape create 50 K50003L8
   Created K50003L8 in slot 3 of library 50
   Restart the library for its robot to see this: mhvtl service restart --library 50

Until you do, ``mhvtl tape list`` shows the new cartridge and ``mhvtl status
library`` does not. The console says the same on the library page.

.. code-block:: console

   $ sudo mhvtl service restart --library 50

A drive says ``silent``
-----------------------

Its daemon is not running. Nothing is broken about the library; that one
drive is simply absent:

.. code-block:: console

   $ sudo mhvtl status activity 50
   51     holding  K50001L8  790.7 MB written - 75.7% of the tape
   52     silent   -         -

   $ sudo mhvtl status system
   drives        13/14 running
   healthy       no

   $ sudo systemctl start vtltape@52.service

A library page that answers 404
-------------------------------

A library made from the command line exists in the configuration before the
console's database hears of it. Opening the page syncs it first, so this
should be rare. If it persists:

.. code-block:: console

   $ sudo mhvtl library list        # is it really there?
   $ sudo mhvtl config sync         # tell the console to re-read the files

An export that points at the wrong device
-----------------------------------------

``/dev/sg`` numbers change when the MHVTL module reloads and when a library
is recreated. An iSCSI export remembers a node, so after either it may be
pointing at something else entirely:

.. code-block:: console

   $ sudo mhvtl iscsi remap

   BACKSTORE      SAVED      NOW        WHY
   -------------  ---------  ---------  ---------------
   lib50_drive1   /dev/sg20  /dev/sg20  already correct

This runs at boot as well. The iSCSI page shows the same per device: **Created
on**, **Device now**, and whether they agree.

Tapes that belong to no library
-------------------------------

Removing a library leaves its cartridges on disk on purpose - they are data:

.. code-block:: console

   $ sudo mhvtl library orphans

   Tapes on disk no library lists (data kept):
   BARCODE   PATH
   --------  -------------------
   K50005L8  /opt/mhvtl/K50005L8

   Put one back with: mhvtl tape adopt <library> <barcode>

See :doc:`../tapes/adopt`.

A drive that will not take a cartridge
--------------------------------------

Check that the drive writes that generation at all:

.. code-block:: console

   $ sudo mhvtl tape media 30

A drive reads older cartridges and writes recent ones, as the real hardware
does. Mounting an LTO-5 cartridge into an LTO-8 drive is allowed and reading
it works; writing to it does not.

The disk is full
----------------

Tapes are files, so a full disk fails writes in ways that look like drive
faults:

.. code-block:: console

   $ sudo mhvtl console disk

A cartridge occupies only what has been written to it, so the problem is
usually one library that has had a great deal written to it, not the number
of cartridges.

Removing a library and rebooting
--------------------------------

On this host the kernel ``ch`` driver is blacklisted, because it takes a
reference on every drive a changer reports and never releases it - the
drives' SCSI addresses then stay unusable until a reboot. If a newly created
library is missing drives, or ``rmmod mhvtl`` says the module is in use with
everything stopped, read *The kernel ch driver leak* in the *Guides*. Only a
reboot clears it.

Nothing here helped
-------------------

The logs say what the drive was actually asked to do:

.. code-block:: console

   $ sudo mhvtl console logs -n 50

and the configuration can be checked against itself:

.. code-block:: console

   $ sudo mhvtl config validate
   Configuration is consistent: 4 libraries, 14 drives
