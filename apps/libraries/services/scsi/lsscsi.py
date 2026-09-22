"""Parsing lsscsi output.

Moved from tape_operations_service.py:457 (_parse_lsscsi_line), folding in the
device-path detection from iscsi_service.py:1239, which was the most robust of
the three: it scans the trailing columns for anything starting with /dev rather
than assuming fixed positions. console_service.py:535 - a third copy that built
a different dataclass and never used sudo - goes away with it.

The original split each line on whitespace and read fixed indices, which breaks
on a model name containing a space:

    [5:0:1:0]  tape  HP  Ultrium 6-SCSI  1.00  /dev/st9  /dev/sg20

There the device path lands in the revision column and every later field shifts.
Vendor and model are taken from the fixed columns lsscsi pads to, and the /dev
nodes are found by looking for them, so a spaced model no longer matters.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
import re
from pathlib import Path
from typing import List, Optional

from ..core import QUICK, shell
from .models import ScsiAddress, ScsiDevice

logger = logging.getLogger(__name__)

#: `lsscsi -g` prints: [h:c:t:l] type vendor(8) model(16) rev(4) dev gen
LINE_RE = re.compile(r'^\s*(\[\d+:\d+:\d+:\d+\])\s+(\S+)\s+(.*)$')
DEV_RE = re.compile(r'(/dev/\S+)')


def parse_line(line: str) -> Optional[ScsiDevice]:
    """Parse one lsscsi line, or return None if it is not one."""
    match = LINE_RE.match(line)
    if not match:
        return None

    address = ScsiAddress.parse(match.group(1))
    device_type = match.group(2)
    rest = match.group(3)

    # Pull the /dev nodes off the end first, so whatever is left is purely
    # vendor, model and revision - however many spaces they contain.
    nodes = DEV_RE.findall(rest)
    described = DEV_RE.sub('', rest).strip()

    # A device with no primary node gets a bare "-" in that column, which is
    # not part of the model or the revision: a changer reported as
    #     [16:0:0:0] mediumx STK L700 0108 - /dev/sg6
    # was read as model "L700 0108", revision "-".
    fields = [field for field in described.split() if field != '-']
    vendor = fields[0] if fields else ''
    revision = fields[-1] if len(fields) > 1 else ''
    model = ' '.join(fields[1:-1]) if len(fields) > 2 else ''

    device_path = nodes[0] if nodes else ''
    generic_path = ''
    for node in nodes:
        if node.startswith('/dev/sg'):
            generic_path = node
            if node == device_path and len(nodes) > 1:
                device_path = next(n for n in nodes if n != node)
            break

    return ScsiDevice(address=address, device_type=device_type, vendor=vendor,
                      model=model, revision=revision,
                      device_path=device_path, generic_path=generic_path)


def parse(text: str) -> List[ScsiDevice]:
    """Parse whole lsscsi output, skipping anything unrecognisable."""
    devices = []
    for line in text.splitlines():
        if not line.strip():
            continue
        device = parse_line(line)
        if device is None:
            logger.debug('unparsed lsscsi line: %s', line.strip()[:120])
            continue
        devices.append(device)
    return devices


def discover() -> List[ScsiDevice]:
    """Ask the host what SCSI devices exist.

    Runs through sudo: the generic nodes are root-only on most installs, and
    lsscsi reports them as '-' otherwise, which is how a library ends up with no
    device to talk to.
    """
    result = shell.sudo(['lsscsi', '-g'], timeout=QUICK)
    if not result.ok:
        logger.warning('lsscsi failed: %s', result.output.strip()[:200])
        return []
    return parse(result.stdout)


def changers(devices: List[ScsiDevice] = None) -> List[ScsiDevice]:
    return [d for d in (devices if devices is not None else discover()) if d.is_changer]


def tapes(devices: List[ScsiDevice] = None) -> List[ScsiDevice]:
    return [d for d in (devices if devices is not None else discover()) if d.is_tape]


#: SCSI host drivers that attach remote devices - an iSCSI initiator's. A tape
#: library exported over iSCSI and logged into on this host appears a second
#: time under one of these, at the same channel:target:lun as the original.
INITIATOR_DRIVERS = frozenset({'iscsi_tcp', 'ib_iser', 'bnx2i', 'cxgb3i', 'cxgb4i',
                               'be2iscsi', 'qla4xxx', 'qedi'})
SCSI_HOSTS = Path('/sys/class/scsi_host')


def host_driver(host: int) -> Optional[str]:
    try:
        return (SCSI_HOSTS / f'host{int(host)}' / 'proc_name').read_text().strip()
    except OSError:
        return None


def local(devices: List[ScsiDevice] = None) -> List[ScsiDevice]:
    """The devices on this machine, without those reached through an initiator.

    Everything that finds a library's device matches on channel:target:lun,
    because device.conf does not name the host. An initiator logged into a
    library exported from this host puts that library's changer at 0:0:0 of
    its own host - library 10's address - so without this, whichever lsscsi
    listed first was the one that got driven, and a map keyed on the address
    kept the remote one.
    """
    devices = devices if devices is not None else discover()
    drivers = {}
    kept = []
    for device in devices:
        host = device.address.host if device.address else None
        if host is not None and host not in drivers:
            drivers[host] = host_driver(host)
        if drivers.get(host) in INITIATOR_DRIVERS:
            continue
        kept.append(device)
    return kept
