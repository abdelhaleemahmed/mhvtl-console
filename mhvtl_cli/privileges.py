"""Who is allowed to run a mutating command.

The access model, which the user chose over a password prompt and which is
worth stating once:

    Reading needs read access to the configuration directory, which on this
    host means membership of the mhvtl group: /etc/mhvtl is 2750 root:mhvtl. If
    you can read it you can list libraries, and if you cannot, no amount of
    authenticating will help.

    Changing anything needs the same group, or root. Every mutation already
    goes through sudo - mktape, targetcli, systemctl, writing device.conf - and
    /etc/sudoers.d/mhvtl-operators grants the group exactly those commands, so
    the sudoers rules are the real authority. A password prompt on top would
    add ceremony rather than safety: it would protect nothing sudo does not
    already protect, and it would train the operator to type a password into a
    program that does not need one.

That means the CLI's job here is to fail early and clearly. Running eight steps
and then discovering at the sudo call that the caller is not allowed is worse
than saying so at the start, and a traceback is worse than either.
"""
import grp
import os
import pwd
from pathlib import Path
from typing import List, Optional

#: The account the web service runs as. A member of this group may change
#: things because the sudoers rules are written for it.
SERVICE_GROUP = 'mhvtl'
SERVICE_USER = 'mhvtl-gui'


class PermissionDenied(Exception):
    """The caller may not do this. Carries what would let them."""


def current_identity() -> dict:
    """Who is running this, in the terms the access model is written in."""
    uid = os.geteuid()
    try:
        name = pwd.getpwuid(uid).pw_name
    except KeyError:
        name = str(uid)
    return {'uid': uid, 'user': name, 'root': uid == 0,
            'groups': _group_names()}


def _group_names() -> List[str]:
    """Every group this process is in, by name.

    os.getgroups() plus the primary group: a user added to the mhvtl group
    since login will not have it here, which is worth knowing when someone
    reports that the CLI refuses them after they were "added to the group".
    """
    names = []
    for gid in set(os.getgroups()) | {os.getgid()}:
        try:
            names.append(grp.getgrgid(gid).gr_name)
        except KeyError:
            names.append(str(gid))
    return sorted(names)


def can_write() -> bool:
    """Is this caller allowed to change the configuration?"""
    identity = current_identity()
    return (identity['root']
            or identity['user'] == SERVICE_USER
            or SERVICE_GROUP in identity['groups'])


def require_write_access(action: str = 'this') -> None:
    """Refuse a mutating command early, saying what would allow it.

    Raises PermissionDenied rather than exiting, so main() decides the exit
    code and the message goes through the same output module as everything
    else.
    """
    if can_write():
        return

    identity = current_identity()
    raise PermissionDenied(
        f'{action} needs membership of the {SERVICE_GROUP} group; '
        f'you are {identity["user"]}, in {", ".join(identity["groups"])}. '
        f'Run "sudo usermod -aG {SERVICE_GROUP} {identity["user"]}" and log in '
        f'again, or run this under sudo.')


def can_read_config(config_dir) -> bool:
    """Can this caller read the configuration directory at all?

    Checked before a read command rather than letting each service report
    "could not read device.conf" for every library in turn.
    """
    path = Path(config_dir)
    return path.is_dir() and os.access(path, os.R_OK | os.X_OK)


def require_read_access(config_dir) -> None:
    """Refuse a read command whose answer would be "nothing", misleadingly."""
    if can_read_config(config_dir):
        return

    path = Path(config_dir)
    if not path.exists():
        raise PermissionDenied(
            f'{path} does not exist; is MHVTL installed on this host?')
    raise PermissionDenied(
        f'cannot read {path}; add yourself to the {SERVICE_GROUP} group, '
        f'or run this under sudo')
