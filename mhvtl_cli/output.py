"""Printing: human tables by default, JSON on request.

Every service result is a dataclass with .to_dict(), so this module stays small:
it decides shape and stream, not content. Nothing here knows what a library is.

Two rules that matter more than they look:

    Data goes to stdout, diagnostics to stderr. `mhvtl library list --json |
    jq` has to work, and a warning printed into the middle of the JSON breaks
    it. The exit code, not the text, says whether it worked.

    A column is never truncated. A barcode or a device path cut to fit a
    terminal is a barcode or a device path that cannot be copied and pasted,
    and the person reading it has no way to know it was shortened.

Exit codes::

    0   the operation succeeded
    1   the operation ran and failed - a library that does not exist, a drive
        that is busy. The message says which.
    2   the command line was wrong. argparse's own convention.
    3   permission denied: the caller may not do this. Separate from 1 because
        a script retrying after a sudo is a sensible response to 3 and not to 1.
"""
import json
import sys
from typing import Any, Dict, Iterable, List, Sequence

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_DENIED = 3

#: Printed where a value is absent. An em dash rather than an empty cell, so a
#: missing value and a value that is the empty string look different.
EMPTY = '-'


def fail(message: str, *details: str, code: int = EXIT_FAILED) -> int:
    """Report a failure on stderr and return the exit code to hand back."""
    print(f'mhvtl: {message}', file=sys.stderr)
    for detail in details:
        if detail:
            print(f'  {detail}', file=sys.stderr)
    return code


def note(message: str) -> None:
    """A diagnostic. Always stderr, so it never lands in piped output."""
    print(message, file=sys.stderr)


def emit_json(payload: Any) -> None:
    """Print a JSON document on stdout.

    default=str so a datetime or a Path in a result does not turn a working
    command into a TypeError at the last moment.
    """
    json.dump(payload, sys.stdout, indent=2, default=str, sort_keys=False)
    sys.stdout.write('\n')


def table(rows: Sequence[Dict[str, Any]], columns: Sequence[str],
          headers: Sequence[str] = None) -> None:
    """Print rows as an aligned table, or nothing at all if there are none.

    Nothing rather than empty headers: a caller that prints "no libraries" says
    it better than a table with a head and no body.
    """
    if not rows:
        return

    headers = list(headers or [column.replace('_', ' ') for column in columns])
    cells = [[_text(row.get(column)) for column in columns] for row in rows]

    widths = [max(len(headers[i]), *(len(row[i]) for row in cells))
              for i in range(len(columns))]

    print('  '.join(header.upper().ljust(width)
                    for header, width in zip(headers, widths)).rstrip())
    print('  '.join('-' * width for width in widths))
    for row in cells:
        print('  '.join(value.ljust(width)
                        for value, width in zip(row, widths)).rstrip())


def pairs(data: Dict[str, Any], keys: Sequence[str] = None,
          labels: Dict[str, str] = None) -> None:
    """Print one record as aligned `label: value` lines.

    For showing a single thing, where a table of one row reads worse than a
    list of its fields.
    """
    labels = labels or {}
    keys = list(keys or data.keys())
    width = max((len(labels.get(key, key.replace('_', ' '))) for key in keys),
                default=0)
    for key in keys:
        label = labels.get(key, key.replace('_', ' '))
        print(f'{label.ljust(width)}  {_text(data.get(key))}')


def result(service_result, *, as_json: bool = False, quiet: bool = False) -> int:
    """Print a ServiceResult and return the exit code for it.

    The whole reason the result contract exists: any command can hand its
    service result here and get correct output and a correct exit code without
    knowing what the command did.
    """
    if as_json:
        emit_json(service_result.to_dict())
        return EXIT_OK if service_result.success else EXIT_FAILED

    if not service_result.success:
        return fail(service_result.message, *service_result.errors)

    if not quiet:
        print(service_result.message)
    return EXIT_OK


def _text(value: Any) -> str:
    """One cell. Booleans read as words because True/False in a table of device
    paths and barcodes looks like a Python repr, which is what it is."""
    if value is None or value == '':
        return EMPTY
    if isinstance(value, bool):
        return 'yes' if value else 'no'
    if isinstance(value, (list, tuple)):
        return ', '.join(_text(item) for item in value) or EMPTY
    return str(value)
