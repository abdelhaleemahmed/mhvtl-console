Writing a backup to a tape
==========================

The console gives you a library; what writes to it is ``tar``, Bacula,
NetBackup, or whatever you already use. This is the smallest complete
example: put a cartridge in a drive, write to it, read it back.

Everything below was run against library 30, an STK L80 with four T10000B
drives.

.. contents::
   :local:
   :depth: 1

1. Find the drive's device
--------------------------

Backup software writes to a device node, not to a library. Ask which node a
drive is:

.. code-block:: console

   $ sudo mhvtl status drive 31

   drive             31
   device            /dev/nst5
   online            yes
   ready             yes
   tape loaded       no
   density           SDLT600, T10000A
   block size        0
   write protected   no

``/dev/nst5`` is the *non-rewinding* node, which is the one to use: closing
``/dev/st5`` rewinds the tape, so a second ``tar`` would write over the first.

``mhvtl scsi map`` lists every library and drive with its nodes at once.

2. Put a cartridge in the drive
-------------------------------

.. code-block:: console

   $ sudo mhvtl tape list 30

   BARCODE   SLOT  KIND   DENSITY  USED MB  CAPACITY MB  ON DISK
   --------  ----  -----  -------  -------  -----------  -------
   G03001TA  1     data   T10KA    0        500          yes
   G03002TA  2     data   T10KA    0        500          yes

   $ sudo mhvtl op mount 30 1 0
   Mounted G03001TA from slot 1 into drive 0

The ``0`` is the robot's drive number, counting from zero - not the drive id.
``mhvtl status library 30`` shows both.

In the console this is **The robot → Mount a tape** on the library page.

3. Write
--------

.. code-block:: console

   $ sudo tar -cf /dev/nst5 -b 128 /usr/share/doc/bash
   tar: Removing leading `/' from member names

``-b 128`` writes 64 KB records, which suits tape far better than tar's
default of 10 KB. The leading-slash warning is tar being careful, not a
problem.

While it runs, the drive can be watched - see :doc:`watching`:

.. code-block:: console

   $ sudo mhvtl status activity 30

   DRIVE  STATE    BARCODE   COUNTERS
   -----  -------  --------  ---------------------------------------------
   31     writing  G03001TA  1.2 MB written - 0.1% of the tape, 2.56x compression

4. Read it back
---------------

.. code-block:: console

   $ sudo mt -f /dev/nst5 rewind
   $ sudo tar -tf /dev/nst5 -b 128 | head -5
   usr/share/doc/bash/
   usr/share/doc/bash/FAQ
   usr/share/doc/bash/INTRO
   usr/share/doc/bash/RBASH
   usr/share/doc/bash/README

A restore is ``tar -xf`` with the same block size. Rewind first: a tape is a
position as much as a medium, and ``tar`` reads from wherever the head is.

5. Put the cartridge away
-------------------------

.. code-block:: console

   $ sudo mt -f /dev/nst5 rewind
   $ sudo mhvtl op unmount 30 0
   Unmounted G03001TA from drive 0 to slot 1

Several backups on one tape
---------------------------

Each ``tar`` writes a file mark and stops. Writing again from where the last
one ended puts a second archive on the same cartridge - which is what the
non-rewinding node is for:

.. code-block:: console

   $ sudo tar -cf /dev/nst5 -b 128 /etc/hosts      # first archive
   $ sudo tar -cf /dev/nst5 -b 128 /etc/services   # second, after it

To read the second, skip the first:

.. code-block:: console

   $ sudo mt -f /dev/nst5 rewind
   $ sudo mt -f /dev/nst5 fsf 1        # forward one file mark
   $ sudo tar -tf /dev/nst5 -b 128

Why the tape says it used no megabytes
--------------------------------------

The bash documentation is about half a megabyte, and ``USED MB`` counts whole
megabytes, so a small backup shows as ``0``. The tape's files are really
there:

.. code-block:: console

   $ sudo ls -l /opt/mhvtl/G03001TA/
   -rw-r----- 1 root mhvtl 512921 data.0
   -rw-r----- 1 root mhvtl  10752 indx.0
   -rw-r----- 1 root mhvtl   1071 mam

MHVTL compresses what it writes - this backup compressed 2.56 to 1 - so a
cartridge holds more than its size in data, and ``used`` is the compressed
figure, because that is what occupies the disk.
