"""Whether the kernel ch driver can reach MHVTL's changers, and what keeps it off.

The ch driver (drivers/scsi/ch.c, /dev/schN) reads a changer's element status
when it binds, and for every drive the changer reports by SCSI id it takes a
reference on that drive's scsi_device. It never gives them back: not when the
changer is removed, not when its daemon restarts, not when ch is unbound. The
drive's address is then unusable until a reboot - LIO refuses a backstore on it
("scsi_device_get() failed for 16:0:19:0") and the mhvtl module cannot be
unloaded. MHVTL personalities differ: IBM's 3573-TL reports its drives' ids and
leaks one reference per drive each time; STK's report "ID/LUN unknown" and do
not. See docs/sphinx/guides/ch-driver-leak.rst.

Nothing in this application needs ch: mtx, the pages, the iSCSI exports and the
/dev/tape/by-id changer links all use the generic /dev/sgN node
(MHVTL_CHANGER_NODE, scsi/mapping.changer_node). So the host keeps ch off with
a modprobe blacklist, and this module reports what is in force, so the pages
can say so - or say that nothing is.

It only reads: the modprobe and udev rule directories, /proc/cmdline, sysfs
and the kernel log. Adding or removing a rule is a host decision, made by hand.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import logging
import re
import uuid
from pathlib import Path
from typing import Dict, List

from ..core import ServiceResult, shell, success_result
from . import lsscsi

logger = logging.getLogger(__name__)

MODPROBE_DIRS = ('/etc/modprobe.d', '/run/modprobe.d', '/usr/local/lib/modprobe.d',
                 '/usr/lib/modprobe.d', '/lib/modprobe.d')
#: Administrators' udev rules only. The distribution's own (in /usr/lib) name
#: /dev/sch* to set its group and persistent links - they are not a policy.
UDEV_DIRS = ('/etc/udev/rules.d', '/run/udev/rules.d')
SYS_MODULE = Path('/sys/module/ch')
SCSI_DEVICES = Path('/sys/bus/scsi/devices')
CMDLINE = Path('/proc/cmdline')

#: A modprobe line about ch: blacklist ch, install ch /bin/false, options ch ...
MODPROBE_RE = re.compile(r'^\s*(blacklist|install|options|softdep)\s+ch(\s|$)')
#: A udev line that names the ch driver or its nodes.
UDEV_RE = re.compile(r'DRIVERS?==\"ch\"|/drivers/ch/|KERNEL==\"sch|scsi_changer')
#: The kernel's own switches: modprobe.blacklist=ch,..., module_blacklist=ch
CMDLINE_RE = re.compile(r'(?:modprobe\.blacklist|module_blacklist)=\S*\bch\b')

#: ch's probe log for one changer: "ch 16:0:23:0: [ch4] ID 24, LUN 0, ..."
PROBE_RE = re.compile(r'\bch (\d+:\d+:\d+:\d+): \[ch\d+\] (.*)$')


def _rule_lines(directories, pattern) -> List[Dict]:
    found = []
    seen = set()
    for directory in directories:
        base = Path(directory)
        try:
            files = sorted(base.glob('*.conf')) + sorted(base.glob('*.rules'))
        except OSError:
            continue
        for path in files:
            # A file of the same name earlier in the list overrides this one.
            if path.name in seen:
                continue
            seen.add(path.name)
            try:
                lines = path.read_text(errors='replace').splitlines()
            except OSError:
                continue
            for number, line in enumerate(lines, 1):
                if not line.lstrip().startswith('#') and pattern.search(line):
                    found.append({'file': str(path), 'line': number,
                                  'text': line.strip()})
    return found


def modprobe_rules() -> List[Dict]:
    return _rule_lines(MODPROBE_DIRS, MODPROBE_RE)


def udev_rules() -> List[Dict]:
    return _rule_lines(UDEV_DIRS, UDEV_RE)


def cmdline_rules() -> List[Dict]:
    try:
        text = CMDLINE.read_text()
    except OSError:
        return []
    return [{'file': str(CMDLINE), 'line': 1, 'text': match.group(0)}
            for match in CMDLINE_RE.finditer(text)]


def probe_reports(log: str) -> Dict[str, bool]:
    """Address -> whether ch's last probe of that changer found drives by id.

    From the kernel log, because that is the only place the probe says so. A
    new probe starts at its "type #1" line, so a changer re-created after a
    daemon restart is judged on its latest probe.
    """
    reports: Dict[str, bool] = {}
    for line in log.splitlines():
        match = PROBE_RE.search(line)
        if not match:
            continue
        address, rest = match.groups()
        if rest.startswith('type #1'):
            reports[address] = False
        elif re.match(r'ID \d+, LUN \d+', rest):
            reports[address] = True
    return reports


def bound_driver(address: str) -> str:
    try:
        return Path(str((SCSI_DEVICES / address / 'driver').resolve())).name \
            if (SCSI_DEVICES / address / 'driver').exists() else ''
    except OSError:
        return ''


def status() -> ServiceResult:
    """What keeps ch away from the changers, and which changers it has."""
    operation_id = str(uuid.uuid4())[:8]
    modprobe = modprobe_rules()
    udev = udev_rules()
    cmdline = cmdline_rules()
    loaded = SYS_MODULE.exists()
    blacklisted = any(rule['text'].split()[0] in ('blacklist', 'install')
                      for rule in modprobe) or bool(cmdline)

    log = shell.sudo(['dmesg'])
    reports = probe_reports(log.stdout if log.ok else '')
    changers = []
    for device in lsscsi.changers(lsscsi.local()):
        address = str(device.address).strip('[]') if device.address else ''
        driver = bound_driver(address) if address else ''
        names_drives = reports.get(address) if driver == 'ch' else None
        changers.append({'address': address, 'vendor': device.vendor,
                         'model': device.model, 'generic_path': device.generic_path,
                         'ch_bound': driver == 'ch',
                         'names_drives': names_drives,
                         'at_risk': driver == 'ch' and bool(names_drives)})

    at_risk = [c for c in changers if c['at_risk']]
    if blacklisted and not loaded:
        state, message = 'protected', 'The ch driver is kept off: no changer can leak'
    elif blacklisted:
        state, message = 'pending', ('ch is blacklisted but still loaded; the rule '
                                     'takes effect at the next boot')
    elif at_risk:
        state, message = 'exposed', (f'{len(at_risk)} changer(s) have ch bound and '
                                     f'report their drives: removing or restarting '
                                     f'them leaks the drives until a reboot')
    else:
        state, message = 'unprotected', 'No rule keeps ch off the changers'

    return success_result(message, {
        'state': state, 'module_loaded': loaded, 'blacklisted': blacklisted,
        'rules': {'modprobe': modprobe, 'udev': udev, 'cmdline': cmdline},
        'active': modprobe + udev + cmdline,
        'changers': changers, 'at_risk': at_risk,
    }, operation_id)
