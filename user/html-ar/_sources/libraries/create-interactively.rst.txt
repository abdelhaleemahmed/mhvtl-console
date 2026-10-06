Creating a library by being asked
=================================

``mhvtl library create --interactive`` walks through the same choices the form
does, one question at a time, offering only what the vendor's catalogue allows
for the answers so far.

It is the terminal equivalent of the form rather than a shortcut: use it when
you do not already know which model takes which drive, or when you want to see
what the options *are* before committing to any of them.

**Nothing is written until the last answer.** Ctrl-C at any point, or ``q`` at
the end, leaves the host exactly as it was.

.. contents::
   :local:
   :depth: 1

The whole thing
---------------

.. code-block:: console

   $ sudo mhvtl library create --interactive
   Creating a library. An empty answer takes the [default];
   Ctrl-C quits and writes nothing.

     1. ADIC
     2. DELL
     3. HP
     4. IBM
     5. OVERLAND
     6. QUANTUM
     7. SONY
     8. SPECTRA
     9. STK
   Vendor profile: IBM
      1. 03584L32 <- default
      2. 03584D32
      3. 03584L52
     ...
     12. 3573-TL
     13. ULT3582-TL
   Library model [03584L32]:
     71 drive(s) at most here, limited by SCSI targets.
      1. ULT3580-TD1
      ...
      8. ULT3580-TD8 <- default
      9. ULT3580-TD9
     10. ULT3580-TDA
     11. ULT3580-HH7
     12. ULT3580-HH8
     13. ULT3580-HH9
     14. ULT3580-HHA
   Drive model [ULT3580-TD8]:
   How many drives [4]:
   Add another drive type? [y/N]:
     1. LTO8 <- default
     2. LTO7
   Cartridge density [LTO8]:
   How many cartridges [50]:
   Add another density? [y/N]:
   Empty slots [4]:
   Library id [80]:

   IBM 03584L32, id 80
     drives      4 x ULT3580-TD8
     cartridges  50 x LTO8
     slots       54 (50 full, 4 empty)
     checked: valid

   4 slot(s) are left empty for cartridges added later: mhvtl tape bulk 80 <count>

     c  create it
     s  create it and keep this configuration as a preset
     p  print the device.conf it would write
     q  quit, writing nothing
   Now what:

Every answer above was left empty except the first, so the whole library is
IBM's defaults.

Each list is only what is still possible
----------------------------------------

The questions narrow as they go, and that is the point of asking them in this
order.

Answer ``IBM`` and the **library model** list is IBM's thirteen models. Answer
``03584L32`` and the **drive model** list is the fourteen drives *that model*
takes - the LTO line. Had you chosen ``03584L22`` instead, the same question
would have offered four 3592 drives and no LTO drive at all, because that is
a 3592 library. Choose ``03584L42`` and it offers two DLT drives.

Then the **cartridge density** list is only what the drive you picked can
write. An ULT3580-TD8 offers LTO8 and LTO7; an ULT3580-TDA offers LTO10 and
LTO10P.

So a combination the vendor does not make is not refused at the end - it is
never offered. There is no way to answer these questions wrongly.

Two limits it tells you about
-----------------------------

``71 drive(s) at most here, limited by SCSI targets`` is not the model's
limit, which is 511 for an 03584L32. It is how many drives will still fit on
this host beside the libraries already configured. The number changes as you
add libraries, which is why it is printed rather than documented.

``Empty slots [4]`` defaults to four, and the summary spells out what that
means - ``slots 54 (50 full, 4 empty)`` - because a library with no empty
slots cannot take a new cartridge without being reconfigured.

More than one kind of drive
---------------------------

**Add another drive type?** and **Add another density?** are where a mixed
library comes from. Answer ``y`` and it asks the same two questions again, and
keeps asking until you say no. The order you answer in is the order the drives
are given slots, which is also their SCSI target order.

See :doc:`drives` for what a mixed library is for.

The four ways out
-----------------

================= =============================================================
Answer            What happens
================= =============================================================
``c``             Creates it. The same steps the one-shot command reports:
                  validate, create, restart, verify, media.
``s``             Creates it, then asks for a name and writes the
                  configuration to ``presets.toml`` - the same as
                  ``--save-preset NAME``. See
                  :doc:`profiles-and-presets`.
``p``             Prints the ``device.conf`` text it *would* write, and comes
                  back to this menu. Nothing is written.
``q``, or Ctrl-C  Quits. ``Nothing was written.``
================= =============================================================

``p`` is worth using the first time. It shows the library stanza and one
stanza per drive, with the serial numbers, the SCSI targets and the revision
levels the profile chose - which is the detail a backup application will
identify the hardware by.

The same thing without the questions
------------------------------------

``--dry-run`` on an ordinary create prints that same ``device.conf`` and stops,
so you can see the outcome of a command you already know how to type:

.. code-block:: console

   $ mhvtl library create --preset ibm-mixed --dry-run
   Library: 80 CHANNEL: 00 TARGET: 31 LUN: 00
    Vendor identification: IBM
    Product identification: 03584L32
    Product revision level: D.02
    Unit serial number: 80000080
    NAA: 80:22:33:44:ab:00:31:00
    Home directory: /opt/mhvtl
    PERSIST: False
    Backoff: 400
   # fifo: /var/tmp/mhvtl

   Drive: 81 CHANNEL: 00 TARGET: 32 LUN: 00
    Library ID: 80 Slot: 01
    Vendor identification: IBM
    Product identification: ULT3580-TDA
    ...

It reads the configuration but writes nothing, so it needs no privileges.

In a script, give the values
----------------------------

``--interactive`` needs a terminal, and says so rather than hanging or
reading nothing:

.. code-block:: console

   $ echo IBM | mhvtl library create --interactive
   mhvtl: --interactive needs a terminal
     stdin is not a tty, so there is nobody to answer the questions
     in a script, give the values instead: mhvtl library create --profile IBM --drives 2 --tapes 20

.. seealso::

   :doc:`create` for the form and the one-shot command, and
   :doc:`profiles-and-presets` for keeping a configuration you have proved.
