A tape as a filesystem: LTFS
============================

LTFS puts a filesystem on a cartridge, so you can copy files on and off it
with ``cp`` and ``ls`` instead of a backup program. It is a separate project
from MHVTL, used mostly for archives — a tape you will read again in ten
years, by someone who no longer has the software that wrote it.

The console does not make every tape an LTFS volume. It is an extra, and it
has to be asked for.

Everything below was run against library 80, a StorageTek SL500 created for
the purpose and deleted at the end. The same sequence is the recording in
``recordings/ltfs-on-a-cartridge``.

.. contents::
   :local:
   :depth: 1

Two different things are called "mount"
---------------------------------------

This is the thing to keep straight before anything else.

.. list-table::
   :header-rows: 1
   :widths: 22 39 39

   * -
     - **Mount a tape**
     - **Mount LTFS**
   * - What happens
     - the robot moves a cartridge from a slot into a drive
     - ``ltfs`` opens the cartridge *already in a drive* as a filesystem
   * - Where
     - Operator → Mount and Unmount, ``mhvtl op mount``
     - the LTFS page, ``mhvtl ltfs mount``
   * - Needs
     - a free drive
     - a cartridge in a drive, a drive LTFS will open, and an LTFS volume

You always do the first before the second.

1. Can this library do LTFS at all?
------------------------------------

Ask before anything else. Most libraries cannot, and the answer costs nothing:

.. code-block:: console

   $ mhvtl ltfs provisioning 80

   Library 80 has no drive LTFS can open, so its cartridges were not read

   DRIVE  MODEL        FIRMWARE  LTFS CAN OPEN  WHY NOT
   -----  -----------  --------  -------------  ---------------------------------------
   81     STK T10000C  0016      no             LTFS does not know the vendor id 'STK'.
   82     STK T10000C  0016      no             LTFS does not know the vendor id 'STK'.

   Drive slots    2 used of 500
   Storage slots  3 full, 4 empty, 7 total
   LTFS volumes   not read - no drive here can open LTFS

   It needs a drive LTFS can open. It can be given:
   VENDOR  MODEL        FIRMWARE AT LEAST
   ------  -----------  -----------------
   IBM     ULT3580-TD7  -

   mhvtl ltfs add-drive 80

Read the last line of the table: **not read**, not "none". No cartridge in
this library was looked at, because no drive here could have opened one. A
cartridge nobody read may still be an LTFS volume written somewhere else, and
the console will not say otherwise.

LTFS refuses these drives for their **vendor id**, not their model. It knows
IBM and HP and does not know ``STK``.

2. Give it a drive LTFS will open
----------------------------------

The drives are emulated, so the vendor id a drive reports is yours to choose.
The library's own profile decides which models it will take; LTFS decides which
vendor ids it will open. Picking one that satisfies both is all this does:

.. code-block:: console

   $ mhvtl ltfs add-drive 80 --model ULT3580-TD7

   ok   validate  LTFS opens IBM ULT3580-TD7 at revision 0016 - reported as IBM
                  rather than STK, which LTFS does not know
   ok   slots     4 empty slot(s)
   ok   drive     Drive 83 added to library 80 (revision 0016)
   ok   restart   Restarted vtllibrary@80.service
   ok   record    Library 80 and its 3 drives recorded in the database
   ok   verify    LTFS can open drive 83: IBM ULT3580-TD7 rev 0016

Nothing existing is changed. **A drive is only ever added** — no drive is
overwritten, no personality is altered, and the drives that were there keep
reporting exactly what they reported before. Removing a drive is a separate
job on the drives pages.

.. code-block:: console

   $ mhvtl ltfs drives 80

   DRIVE  ID  MODEL        NODE       LTFS CAN OPEN  CARTRIDGE  STATE  MOUNTED AT
   -----  --  -----------  ---------  -------------  ---------  -----  ----------
   0      81  T10000C      /dev/sg32  no             -          -      -
   1      82  T10000C      /dev/sg33  no             -          -      -
   2      83  ULT3580-TD7  /dev/sg34  yes            -          -      -

The **NODE** column is the one to check. A drive with no device node is not
usable, whatever the other columns say.

Some drives also need a minimum firmware: IBM LTO-5 wants B170, LTO-8 wants
HB81. ``--drive-revision`` sets it, and the refusal tells you when you need it.

3. A cartridge it can write
----------------------------

Being partitionable is not enough. ``mkltfs`` **writes**, so the cartridge has
to be a density the drive can write, not merely load — an LTO-7 drive loads
LTO-5 read-only, so an LTO-5 cartridge in it can never become a volume:

.. code-block:: console

   $ mhvtl ltfs add-media 80 --count 1

   ok   validate  drive 83 (ULT3580-TD7) can format LTO7 - the newest density it writes
   ok   media     Created 1 of 1 tapes in library 80
   ok   verify    1 LTO7 cartridge(s) in slots - blank media, not LTFS volumes:
                  mkltfs formats one, and erases it

   $ mhvtl tape list 80

   BARCODE   SLOT  KIND  DENSITY  USED MB  CAPACITY MB  ON DISK
   --------  ----  ----  -------  -------  -----------  -------
   K80001TC  1     data  T10KC    0        500          yes
   K80002TC  2     data  T10KC    0        500          yes
   K80003TC  3     data  T10KC    0        500          yes
   K80004L7  4     data  LTO7     0        500          yes

The three T10000 cartridges came with the library and the new drive cannot
write any of them. ``K80004L7`` is the one that can become a volume.

4. Put it in the drive
-----------------------

The robot, not the filesystem — the first sense of "mount":

.. code-block:: console

   $ mhvtl op mount 80 4 2
   Mounted K80004L7 from slot 4 into drive 2

   $ mhvtl op layout 80

   Drives
     Drive 0   T10000C      empty  not an LTO drive
     Drive 1   T10000C      empty  not an LTO drive
     Drive 2   ULT3580-TD7  K80004L7  (LTO-7)

``op layout`` draws the whole library — drives, slots, the import/export port
and a legend — coloured by tape generation. On the web it is
**Operator → Mount and Unmount**.

5. Format it — which the console will not do for you
-----------------------------------------------------

.. warning::

   ``mkltfs`` partitions and erases the cartridge. Everything on it is gone,
   and there is no undo.

The console refuses, twice over — a setting it checks, and a ``sudoers`` rule
the service account was never granted:

.. code-block:: console

   $ mhvtl ltfs format 80 2

   mhvtl: Formatting a cartridge is disabled on this host
     mkltfs partitions and erases the cartridge; there is no undo.
     Set MHVTL_GUI_ALLOW_LTFS_FORMAT to enable it, and note the packaged
     sudoers does not grant mkltfs to the service account.

That is deliberate, and it is not an obstacle to work around. Formatting a
cartridge stays an act a person takes at a terminal, with root, having decided
to:

.. code-block:: console

   $ sudo mkltfs -d /dev/sg34 -f

   LTFS15024I Medium formatted successfully.

The device is the **NODE** from ``ltfs drives``.

6. Mount it, write to it, read it back
---------------------------------------

Now the second sense of "mount":

.. code-block:: console

   $ mhvtl ltfs mount 80 2
   K80004L7 mounted at /var/lib/ltfs/mnt/library80-drive2

It is a directory. Write to it with anything:

.. code-block:: console

   $ sudo sh -c "echo 'hello' > /var/lib/ltfs/mnt/library80-drive2/hello.txt"
   $ sudo ls -l /var/lib/ltfs/mnt/library80-drive2

   -rwxrwxrwx. 1 root root     25 Oct  2 04:27 hello.txt
   -rwxrwxrwx. 1 root root 262144 Oct  2 04:27 payload.bin

And the proof that it is on the tape rather than in a cache — unmount, mount
again, and check:

.. code-block:: console

   $ mhvtl ltfs unmount 80 2
   /var/lib/ltfs/mnt/library80-drive2 unmounted; the index is on the cartridge

   $ mhvtl ltfs mount 80 2
   K80004L7 mounted at /var/lib/ltfs/mnt/library80-drive2

   $ sudo sh -c "cd /var/lib/ltfs/mnt/library80-drive2 && sha256sum -c checksums.txt"
   hello.txt: OK
   payload.bin: OK

7. Taking it out again, in the right order
-------------------------------------------

The filesystem first, then the robot. If you try it the other way the console
stops you:

.. code-block:: console

   $ mhvtl op unmount 80 2

   mhvtl: Drive 2 cannot be emptied: an LTFS filesystem is mounted on it,
          at /var/lib/ltfs/mnt/library80-drive2
     Unmount the filesystem first - the LTFS page, or `mhvtl ltfs unmount 80 2`
     - and then unload the tape.
     Pulling the cartridge out from under a mounted filesystem gives whatever
     is using it I/O errors rather than a clean end.

This is a refusal and not a warning. Taking the cartridge out leaves ``ltfs``
holding a device with no medium; whatever was reading the mount gets errors
instead of an ending. On the mount page such a drive is marked and cannot be
clicked, and ``op layout`` marks it **MOUNTED**.

So:

.. code-block:: console

   $ mhvtl ltfs unmount 80 2
   /var/lib/ltfs/mnt/library80-drive2 unmounted; the index is on the cartridge

   $ mhvtl op unmount 80 2
   Unmounted K80004L7 from drive 2 to slot 4

Which cartridges are volumes
-----------------------------

.. code-block:: console

   $ mhvtl ltfs tapes 80

   BARCODE   PARTS  LAYOUT  STATE  WHAT THAT MEANS
   --------  -----  ------  -----  ------------------
   K80001TC  1      1.8     plain  not LTFS
   K80004L7  2      1.8     ltfs   LTFS, 2 partitions

   1 of 4 cartridge(s) are LTFS volumes.

Four states, and the difference between the last two matters:

.. list-table::
   :header-rows: 1
   :widths: 18 82

   * - State
     - Means
   * - ``ltfs``
     - read, and it is an LTFS volume
   * - ``plain``
     - read, and it is not
   * - ``unknown``
     - read, and the cartridge could not say
   * - *(blank)*
     - **not read at all** — no drive here could open it

A blank is never reported as ``plain``. "We did not look" is not "there is
nothing there".
