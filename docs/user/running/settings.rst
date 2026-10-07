How big a new cartridge is
==========================

A new cartridge is **1,000 MB**, whatever generation it is. A real LTO-8 holds
12 TB, and the console will make you one that does — but not by default, and
this page is why, and how to change it.

.. contents::
   :local:
   :depth: 1

Why not the real capacity
-------------------------

It costs nothing in disk. MHVTL's media files are sparse: a cartridge occupies
only what has been written to it, so a 12 TB tape and a 1 GB tape both start at
a few kilobytes.

It costs time, and that is the problem.

.. list-table:: How long it takes to fill one, at about 55 MB/s
   :header-rows: 1
   :widths: 40 60

   * - Cartridge
     - Time to fill
   * - 1,000 MB — the default
     - 18 seconds
   * - LTO-6 at its real 2.5 TB
     - 13 hours
   * - LTO-8 at its real 12 TB
     - **63 hours**

The things a virtual tape library is usually *for* are what happens at the end
of a tape: does the backup span to the next cartridge, does it ask for one,
does the drive report end of medium. On a cartridge nobody can fill, none of
that can be reached. The fullness bar never moves either.

So the console makes a small cartridge and lets you say when you want a real
one.

.. note::

   This decides how big the console *makes* a cartridge. It does not change
   what the hardware holds. An LTO-8 holds 12 TB whatever is set here, and
   that figure is shown beside every setting — the **THE CARTRIDGE HOLDS**
   column below, and a column on the Settings page.

Changing it
-----------

In the console
~~~~~~~~~~~~~~

**System Console → Settings**. A row per cartridge type, with what it will be
made at, where that value came from, and what the cartridge really holds —
click that capacity to put it in the field.

A row left empty takes the default. The field accepts ``2000GB`` and ``12TB``
as well as a number of megabytes.

From the terminal
~~~~~~~~~~~~~~~~~

There are thirty-three cartridge types, so ``list`` takes a section, a family
or one key — and the case never matters:

.. code-block:: console

   $ mhvtl settings list tape.size.lto
   SETTING           VALUE            FROM                 THE CARTRIDGE HOLDS
   ----------------  ---------------  -------------------  ---------------------
   tape.size.LTO6    1 GB (1,000 MB)  the shipped default  2.5 TB (2,500,000 MB)
   tape.size.LTO7    1 GB (1,000 MB)  the shipped default  6 TB (6,000,000 MB)
   tape.size.LTO8    1 GB (1,000 MB)  the shipped default  12 TB (12,000,000 MB)
   tape.size.LTO9    1 GB (1,000 MB)  the shipped default  18 TB (18,000,000 MB)
   tape.size.LTO10   1 GB (1,000 MB)  the shipped default  30 TB (30,000,000 MB)

   11 setting(s), all at the shipped default
   No settings file yet; it is written when you set something: /etc/mhvtl-gui/settings.toml

=========================================  ============================
``mhvtl settings list``                    all thirty-three
``mhvtl settings list tape.size``          the section
``mhvtl settings list tape.size.lto``      every LTO generation
``mhvtl settings list tape.size.3592``     J1A, E05, E06, E07
``mhvtl settings list tape.size.LTO8``     one
=========================================  ============================

A family is not always the start of the name: the 3592 cartridges are called
after the drive generation that writes them, so ``tape.size.3592`` finds four
cartridges that share no spelling with each other.

One of them on its own:

.. code-block:: console

   $ mhvtl settings get tape.size.LTO8
   value                1 GB (1,000 MB)
   from                 the shipped default
   the cartridge holds  12 TB (12,000,000 MB)

Changing one, and putting it back:

.. code-block:: console

   $ sudo mhvtl settings set tape.size.LTO8 12TB
   tape.size.LTO8 is now 12 TB (12,000,000 MB)
   Tapes created from now on use this; the ones you already have keep the size they were made with.

   $ sudo mhvtl settings reset tape.size.LTO8
   tape.size.LTO8 is back to 1 GB (1,000 MB) (the shipped default)

``tape.size.default`` changes every generation at once:

.. code-block:: console

   $ sudo mhvtl settings set tape.size.default 5000

``reset`` means *back to the shipped default*, not back to the real capacity.
The real capacity is a value you choose, like any other.

The **FROM** column names which of them answered — ``the shipped default``,
``the file default``, or ``set for this density``.

Just this one cartridge
~~~~~~~~~~~~~~~~~~~~~~~

Nothing above is needed to make a single full-size tape. Every creation takes
a size, and it beats whatever the settings say:

.. code-block:: console

   $ sudo mhvtl tape create 50 K50001L8 --size-mb 12TB
   $ sudo mhvtl tape bulk 50 10 --size-mb 2000GB
   $ sudo mhvtl library create --profile IBM --size-mb 12TB --id 90

A library can hold two kinds of cartridge, and then one size for all of them
is the wrong answer. ``--media-size`` gives each kind its own, and a kind not
named takes its setting:

.. code-block:: console

   $ sudo mhvtl library create --profile IBM --id 90 \
       --media LTO8:20 --media LTO6:10 \
       --media-size LTO8:12TB --media-size LTO6:2500GB

The creation wizard has the same field on each cartridge row, and the tape
form has one for the single tape.

Which setting wins
------------------

Four answers, each narrower than the last. The last one that applies is the
one used:

.. list-table::
   :header-rows: 1
   :widths: 10 40 50

   * -
     - Where
     - What it covers
   * - 1
     - the console itself
     - 1,000 MB, every generation
   * - 2
     - ``tape.size.default``
     - every generation, on this host
   * - 3
     - ``tape.size.LTO8``
     - one generation, on this host
   * - 4
     - ``--size-mb``, or the form
     - one cartridge, once

``mhvtl settings list`` prints which of them answered, in the **FROM** column,
because "why is this cartridge 1 GB" is the question worth being able to
answer.

Units
-----

Everywhere a size is given — the setting, the two flags, and both forms —
takes the same three spellings, and ``MB`` is assumed when there is no unit:

.. code-block:: console

   1000          2000GB          12TB

They are **decimal**: 1 GB is 1,000 MB. That is how tape capacity is quoted —
an LTO-8 is sold as 12 TB and that is 12,000,000 MB.

``MiB``, ``GiB`` and ``TiB`` are refused rather than converted:

.. code-block:: console

   $ sudo mhvtl tape create 50 K50001L8 --size-mb 12TiB
   mhvtl tape create: error: argument --size-mb: 12TiB: sizes here are decimal - MB, GB or TB. A tape is quoted decimal, so an LTO-8 holds 12 TB and not 12 TiB

12 TB and 12 TiB are ten per cent apart, and a cartridge ten per cent off the
one you meant is a difference you would only find later.

Sizes are stored in MB, whatever you type.

The file
--------

``/etc/mhvtl-gui/settings.toml``. There is no such file until you save
something; without one, every cartridge is 1,000 MB.

.. code-block:: toml

   [tape.size]
   default = 1000

   LTO8 = 12000000      # native 12,000,000 MB

It is safe to edit by hand, and each generation's real capacity is written
beside it as a comment, so a value can be put back without looking it up. The
comments are rewritten every time the console saves, so they cannot go stale.

``/etc/mhvtl-gui/settings.toml.example`` beside it lists every generation with
its real capacity. **An upgrade replaces the example**, so do not keep settings
there — copy a line into ``settings.toml`` instead.

Deleting ``settings.toml`` puts every cartridge back to 1,000 MB.

What it does not touch
----------------------

**Cartridges you already have.** They keep the size they were made with;
``mhvtl tape list <library>`` shows it. A setting applies to tapes created
afterwards.

**What the hardware holds.** The capacities come from MHVTL's own tables and
are not editable anywhere in the console. The *the cartridge holds* column
reads those.

.. seealso::

   :doc:`../tapes/create` for making cartridges, and
   :doc:`../libraries/create` for a library's own.
