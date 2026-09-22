What this is
============

MHVTL is a Linux kernel module and a set of daemons that pretend to be a tape
library: a robot that moves cartridges between slots and drives, and drives
that write to files on disk. Backup software talks to it exactly as it would
to a real library, over SCSI or iSCSI.

This console is a web interface for it, and a command called ``mhvtl``. They
do the same things, because they are the same code: both call one service
layer, so neither can do something the other cannot.

What you can do with it
-----------------------

- **Create libraries** from the models MHVTL can emulate - an IBM TS3500, an
  STK SL500, a Quantum Scalar i500 - with the drives and cartridges those
  models really take.
- **Fill them with tapes**, one at a time or a barcode run at a time, and put
  a tape that still has its data back into a library.
- **Drive the robot**: mount, unmount, move a cartridge between slots.
- **Export a library over iSCSI**, so another machine can back up to it.
- **Watch a drive while it writes**, which no other tool on the host can do
  during a backup.
- **See what the host thinks**: the daemons, the kernel modules, the SCSI
  devices, the logs, the disk.

What it is not
--------------

It is not backup software. It gives you a library; what writes to that
library is ``tar``, Bacula, NetBackup, Veeam or whatever you already use.

It does not store your backups anywhere clever: a tape is a directory of
files under ``/opt/mhvtl``, and its size is the size you gave it when you
made it.

Why a virtual library
---------------------

Because tape is awkward to test against. A virtual library lets you rehearse
a restore, prove a retention policy, train someone, or develop backup
software, without a drive, without cartridges, and without waiting for a
robot to move anything.

The words used here
-------------------

**Library**
   The whole thing: a robot, some drives, and slots to hold cartridges. Has
   an id - 10, 20, 30 - which is how every command refers to it.

**Drive**
   Writes and reads a cartridge. Has its own id, and belongs to one library.
   MHVTL runs one daemon per drive.

**Slot**
   Where a cartridge sits when it is not in a drive. A library has a fixed
   number; empty ones are room for tapes added later.

**Tape**, or cartridge, or medium
   Identified by a barcode like ``E01001L8``. The last two characters say
   which generation it is - ``L8`` is LTO-8 - and that decides which drives
   can load it.

**MAP**
   The mail slot: where a real operator would put a cartridge into the
   library from outside. MHVTL emulates it.

**Changer**
   The robot, as the kernel sees it: a SCSI device of type ``mediumx``.
