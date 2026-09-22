MHVTL GUI — User Guide
======================

A web console for MHVTL, the Linux virtual tape library. It creates
libraries, fills them with tapes, drives the robot, exports them over iSCSI,
and shows what a drive is doing while a backup writes to it.

Everything here can also be done from the terminal. Each task shows both,
side by side, because they are the same code underneath: the console and the
``mhvtl`` command call one service, so neither can do something the other
cannot.

.. code-block:: console

   $ sudo mhvtl status activity 50

   Library 50: 1 of 2 drive(s) hold a tape, 1 working

   DRIVE  STATE    BARCODE   COUNTERS
   -----  -------  --------  --------------------------------------------
   51     writing  K50001L8  300.0 MB written - 29.6% of the tape
   52     empty    -         -

.. toctree::
   :maxdepth: 2
   :caption: Getting started

   start/what-it-is
   start/first-look
   start/the-console

.. toctree::
   :maxdepth: 2
   :caption: Libraries

   libraries/create
   libraries/the-library-page
   libraries/drives
   libraries/remove

.. toctree::
   :maxdepth: 2
   :caption: Tapes

   tapes/create
   tapes/inventory
   tapes/move
   tapes/adopt

.. toctree::
   :maxdepth: 2
   :caption: Using a library

   using/backup
   using/watching
   using/iscsi

.. toctree::
   :maxdepth: 2
   :caption: Keeping it running

   running/console
   running/password
   running/when-something-is-wrong

Where else to look
------------------

- **API Reference** — the Python services, from their docstrings.
