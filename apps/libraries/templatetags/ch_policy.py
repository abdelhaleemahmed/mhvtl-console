"""{% ch_policy_box %}: what keeps the kernel ch driver off the changers.

Shown at the top of the service and iSCSI pages, because the leak it guards
against surfaces there - an export that fails on a drive, a module that will
not unload - long after the removal that caused it.
"""
import logging

from django import template

from ..services.scsi import ch_policy

register = template.Library()
logger = logging.getLogger(__name__)


@register.inclusion_tag('libraries/partials/ch_policy_box.html')
def ch_policy_box():
    try:
        result = ch_policy.status()
        return {'policy': result.data, 'message': result.message, 'error': None}
    except Exception as exc:                           # noqa: BLE001 - shown
        logger.warning('reading the ch policy: %s', exc)
        return {'policy': None, 'message': '', 'error': str(exc)}
