"""mhvtl - command line interface to the tape library.

A consumer of apps.libraries.services, deliberately outside the Django app so
the dependency direction is obvious: the CLI imports services, never the other
way round, and it holds no business logic of its own.

    mhvtl library   list | show | create | update | delete | orphans | next-id
    mhvtl drive     list | add | remove | update
    mhvtl tape      list | create | bulk | delete | next-barcode | slots
    mhvtl op        mount | unmount | move | online | offline
    mhvtl status    library | drive | dashboard | system
    mhvtl service   start | stop | restart | status
    mhvtl config    list | show | export | sync
    mhvtl scsi      discover | devices | map
    mhvtl iscsi     status | target | lun | acl | export-library
    mhvtl console   logs | modules | disk

Access model - Unix identity, no password:

    Reading needs read access to the config directory. Changing anything needs
    root or the mhvtl-gui account, because every mutation already runs through
    sudo; a prompt on top would add ceremony, not safety.

Output: human-readable tables by default, --json for scripting. Every service
result is a dataclass with .to_dict(), so JSON needs no extra formatting code.

"""
