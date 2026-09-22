"""Reading LIO's configuration: saveconfig.json and `targetcli ls`.

Moved from iscsi_service.py:_parse_targets_from_config, _parse_targets_from_ls,
_parse_backstores_from_config and _parse_backstores_from_ls. The module docstring
in service.py called this "around 400 lines"; it is closer to 150, and the delay
was for fixtures rather than for size.

Two formats, and they are not equivalent:

    /etc/target/saveconfig.json   the authority. JSON, complete, and what
                                  targetctl restores from at boot.
    targetcli ls                  a tree drawn for a human, with the fields
                                  padded out by dots. Everything it prints is
                                  in the JSON; the reverse is not true.

The JSON is read first and the tree only when it cannot be. Worth knowing that
the two can disagree about the running system: on the host these fixtures came
from, saveconfig.json describes three pscsi backstores and three LUNs while the
live tree has none. A pscsi backstore names a /dev/sgN path, those numbers are
reassigned when the SCSI modules reload, and the backstore does not come back.
So the JSON is the authority on what was configured, not on what is exported
right now.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
import json
import logging
import re
from typing import Any, Dict, List

from .models import (IscsiAcl, IscsiBackstore, IscsiLun, IscsiPortal, IscsiTarget,
                     IscsiTpg)

logger = logging.getLogger(__name__)

#: `  |   o- tpg1 ......... [gen-acls, no-auth]` - the tag is part of the name.
TPG_RE = re.compile(r'o-\s+tpg(\d+)\b')
#: `  | o- iqn.2026-04.com.mhvtl:library10 ..... [TPGs: 1]`
IQN_RE = re.compile(r'o-\s+(iqn\.[^\s]+)')
#: `  | o- block ..... [Storage Objects: 0]`
PLUGIN_RE = re.compile(r'o-\s+(pscsi|block|fileio|ramdisk)\b')
#: Any tree entry: the name is what follows ``o-`` up to the padding dots.
ENTRY_RE = re.compile(r'o-\s+(\S+)')

PLUGINS = ('pscsi', 'block', 'fileio', 'ramdisk')


# -- saveconfig.json --------------------------------------------------------

def parse_config(text: str) -> Dict[str, Any]:
    """The whole saved configuration. Returns empty lists on unreadable JSON.

    A malformed file is reported and treated as "nothing configured" rather than
    raised: this is read on a status page, and a status page that 500s tells an
    operator less than one saying the configuration cannot be read.
    """
    try:
        config = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning('parsing saveconfig.json: %s', exc)
        return {'targets': [], 'backstores': [], 'readable': False}

    return {'targets': targets_from_config(config),
            'backstores': backstores_from_config(config),
            'readable': True}


def targets_from_config(config: Dict) -> List[IscsiTarget]:
    """iSCSI targets, their TPGs, LUNs, ACLs and portals."""
    targets = []
    for data in config.get('targets') or []:
        if data.get('fabric') != 'iscsi':
            continue                                   # loopback, qla2xxx, ...
        targets.append(IscsiTarget(
            iqn=data.get('wwn', ''),
            tpgs=[_tpg_from_config(tpg) for tpg in data.get('tpgs') or []]))
    return targets


def _tpg_from_config(data: Dict) -> IscsiTpg:
    attributes = data.get('attributes') or {}
    return IscsiTpg(
        tag=int(data.get('tag', 1)),
        enabled=bool(data.get('enable', True)),
        luns=[_lun_from_config(lun) for lun in data.get('luns') or []],
        acls=[_acl_from_config(acl) for acl in data.get('node_acls') or []],
        portals=[IscsiPortal(ip_address=portal.get('ip_address', '0.0.0.0'),
                             port=int(portal.get('port', 3260)))
                 for portal in data.get('portals') or []],
        # These two decide whether the target is open to anyone, so they are
        # read as real booleans rather than left as whatever JSON held.
        generate_node_acls=bool(attributes.get('generate_node_acls', 0)),
        demo_mode_write_protect=bool(attributes.get('demo_mode_write_protect', 0)),
        authentication=bool(attributes.get('authentication', 0)),
        chap_userid=data.get('chap_userid') or None,
        chap_password=data.get('chap_password') or None,
        chap_mutual_userid=data.get('chap_mutual_userid') or None,
        chap_mutual_password=data.get('chap_mutual_password') or None)


def _lun_from_config(data: Dict) -> IscsiLun:
    """A LUN names its backstore by path: `/backstores/pscsi/lib10_drive0`."""
    path = data.get('storage_object', '') or ''
    parts = [part for part in path.split('/') if part]
    return IscsiLun(
        lun_id=int(data.get('index', 0)),
        backstore_name=parts[-1] if parts else '',
        backstore_plugin=next((part for part in parts if part in PLUGINS),
                              'unknown'),
        alias=data.get('alias'))


def _acl_from_config(data: Dict) -> IscsiAcl:
    """saveconfig.json names the CHAP fields chap_userid, chap_password,
    chap_mutual_userid and chap_mutual_password. This read userid and the
    rest, which are never there, so every ACL showed no authentication."""
    return IscsiAcl(
        initiator_iqn=data.get('node_wwn', ''),
        userid=data.get('chap_userid') or None,
        password=data.get('chap_password') or None,
        mutual_userid=data.get('chap_mutual_userid') or None,
        mutual_password=data.get('chap_mutual_password') or None,
        mapped_luns=[int(mapping.get('tpg_lun', 0))
                     for mapping in data.get('mapped_luns') or []])


def backstores_from_config(config: Dict) -> List[IscsiBackstore]:
    """Storage objects, whatever plugin backs them."""
    return [
        IscsiBackstore(
            name=obj.get('name', ''),
            plugin=obj.get('plugin', 'unknown'),
            # fileio calls it dev_path, the others dev.
            device_path=obj.get('dev') or obj.get('dev_path'),
            size=obj.get('size'),
            wwn=obj.get('wwn'))
        for obj in config.get('storage_objects') or []]


# -- targetcli ls -----------------------------------------------------------

def targets_from_ls(output: str) -> List[IscsiTarget]:
    """Targets from the printed tree, used when the JSON cannot be read.

    Two fixes over the version this replaces: the TPG tag is read from `tpg2`
    rather than assumed to be 1, and a TPG is only attached while an IQN line
    has been seen, so `[TPGs: 1]` on the target's own line no longer creates a
    phantom TPG.
    """
    targets: List[IscsiTarget] = []
    for line in output.splitlines():
        stripped = line.strip()

        iqn = IQN_RE.search(stripped)
        if iqn:
            targets.append(IscsiTarget(iqn=iqn.group(1)))
            continue

        tpg = TPG_RE.search(stripped)
        if tpg and targets:
            targets[-1].tpgs.append(IscsiTpg(
                tag=int(tpg.group(1)),
                generate_node_acls='gen-acls' in stripped
                                   and 'no-gen-acls' not in stripped))
    return targets


def backstores_from_ls(output: str) -> List[IscsiBackstore]:
    """Backstores from the printed tree.

    Only entries nested under a plugin heading count. The version this replaces
    kept the last plugin it saw for the rest of the file, so given a full `ls`
    it reported every target and portal as a backstore.
    """
    backstores: List[IscsiBackstore] = []
    plugin = None
    plugin_column = None

    for line in output.splitlines():
        if 'o-' not in line:
            continue

        # Depth is the column `o-` sits at, not the leading whitespace: the tree
        # is drawn with `|` continuation bars, so `  | o- block` and
        # `  | | o- disk0` both start with two spaces and are not siblings.
        column = line.index('o-')
        stripped = line.strip()

        heading = PLUGIN_RE.search(stripped)
        if heading:
            plugin, plugin_column = heading.group(1), column
            continue

        if plugin is None:
            continue
        if column <= plugin_column:
            plugin = plugin_column = None              # left the plugin's subtree
            continue

        entry = ENTRY_RE.search(stripped)
        if entry:
            backstores.append(IscsiBackstore(name=entry.group(1), plugin=plugin))

    return backstores
