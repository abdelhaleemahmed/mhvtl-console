"""iSCSI dataclasses.

Moved from iscsi_service.py:38-181, unchanged. The nine types compose into
IscsiStatus, which is what the iSCSI dashboard renders in one call.

Left behind deliberately: that module's own ServiceResult (iscsi_service.py:182),
one of the four rival definitions this refactor exists to collapse. Results come
from core.results now.

Rules for this layer:
    - returns a ServiceResult from core.results, never a bare dict
    - never imports django.contrib.messages and never sees a request
    - all subprocess work goes through core.shell
    - only sync/ may import apps.libraries.models
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class IscsiBackstore:
    """Represents a pscsi/block/fileio backstore"""
    name: str
    plugin: str  # 'pscsi', 'block', 'fileio', 'ramdisk'
    device_path: Optional[str] = None
    size: Optional[int] = None
    wwn: Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            'name': self.name,
            'plugin': self.plugin,
            'device_path': self.device_path,
            'size': self.size,
            'wwn': self.wwn
        }


@dataclass
class IscsiLun:
    """Represents a LUN mapping to a backstore"""
    lun_id: int
    backstore_name: str
    backstore_plugin: str
    alias: Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            'lun_id': self.lun_id,
            'backstore_name': self.backstore_name,
            'backstore_plugin': self.backstore_plugin,
            'alias': self.alias
        }


@dataclass
class IscsiAcl:
    """Represents an initiator ACL entry"""
    initiator_iqn: str
    userid: Optional[str] = None
    password: Optional[str] = None
    mutual_userid: Optional[str] = None
    mutual_password: Optional[str] = None
    mapped_luns: List[int] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            'initiator_iqn': self.initiator_iqn,
            'userid': self.userid,
            'password': '***' if self.password else None,
            'mutual_userid': self.mutual_userid,
            'mutual_password': '***' if self.mutual_password else None,
            'mapped_luns': self.mapped_luns
        }


@dataclass
class IscsiPortal:
    """Represents an IP:port binding"""
    ip_address: str
    port: int = 3260

    def to_dict(self) -> Dict:
        return {
            'ip_address': self.ip_address,
            'port': self.port
        }


@dataclass
class IscsiTpg:
    """Represents a Target Portal Group"""
    tag: int
    enabled: bool = True
    luns: List[IscsiLun] = field(default_factory=list)
    acls: List[IscsiAcl] = field(default_factory=list)
    portals: List[IscsiPortal] = field(default_factory=list)
    generate_node_acls: bool = False  # If True, allows any initiator
    demo_mode_write_protect: bool = False
    # CHAP. authentication is the TPG attribute that makes it required; the
    # credentials here are the target-wide ones, used by initiators that come
    # in through generate_node_acls (no ACL of their own).
    authentication: bool = False
    chap_userid: Optional[str] = None
    chap_password: Optional[str] = None
    chap_mutual_userid: Optional[str] = None
    chap_mutual_password: Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            'tag': self.tag,
            'enabled': self.enabled,
            'luns': [l.to_dict() for l in self.luns],
            'acls': [a.to_dict() for a in self.acls],
            'portals': [p.to_dict() for p in self.portals],
            'generate_node_acls': self.generate_node_acls,
            'demo_mode_write_protect': self.demo_mode_write_protect,
            'authentication': self.authentication,
            'chap_userid': self.chap_userid,
            'chap_password': '***' if self.chap_password else None,
            'chap_mutual_userid': self.chap_mutual_userid,
            'chap_mutual_password': '***' if self.chap_mutual_password else None,
        }


@dataclass
class IscsiTarget:
    """Represents an iSCSI target"""
    iqn: str
    tpgs: List[IscsiTpg] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            'iqn': self.iqn,
            'tpgs': [t.to_dict() for t in self.tpgs]
        }


@dataclass
class IscsiServiceStatus:
    """Service status information"""
    running: bool
    enabled: bool
    active_state: str  # 'active', 'inactive', 'failed', etc.
    sub_state: str  # 'running', 'dead', etc.

    def to_dict(self) -> Dict:
        return {
            'running': self.running,
            'enabled': self.enabled,
            'active_state': self.active_state,
            'sub_state': self.sub_state
        }


@dataclass
class IscsiStatus:
    """Overall iSCSI configuration status"""
    service: IscsiServiceStatus
    targets: List[IscsiTarget] = field(default_factory=list)
    backstores: List[IscsiBackstore] = field(default_factory=list)
    config_saved: bool = False
    last_checked: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict:
        return {
            'service': self.service.to_dict(),
            'targets': [t.to_dict() for t in self.targets],
            'backstores': [b.to_dict() for b in self.backstores],
            'config_saved': self.config_saved,
            'target_count': len(self.targets),
            'backstore_count': len(self.backstores),
            'last_checked': self.last_checked.isoformat()
        }
