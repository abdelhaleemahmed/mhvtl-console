MHVTL GUI — API Reference
=========================

The Python API, generated from the docstrings in the source. Nothing here is
written twice: if a paragraph is wrong, it is wrong in the module it
documents.

The layer to start from is ``apps.libraries.services``. Every feature of the
console and of the ``mhvtl`` command is a call into it, and it is the only
place where a decision about MHVTL is made.

.. toctree::
   :maxdepth: 2

   services
   libraries
   authentication
   cli

Reading these pages
-------------------

``ServiceResult``
   What every service method returns: ``success``, ``message``, ``data``,
   ``errors`` and an operation id. A caller checks ``success`` and prints
   ``message``; it does not catch exceptions for ordinary failures.

Module docstrings
   Most modules open with why they exist, and often with what was wrong
   before them - which host it was found on, and what it looked like. That is
   deliberate. The behaviour is in the code; the reason is only here.

``[source]``
   Every entry links to the source. When the prose and the code disagree,
   the code is right and the prose is a bug.

Where else to look
------------------

- **User Guide** — what the console does, from the operator's side.
