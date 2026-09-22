"""Vendor and model reference data. Pure, no I/O.

Modules:
    data.py           the profile tables and their lookups: which libraries a
                      vendor makes, which drives each can carry, which media each
                      drive accepts
    personalities.py  what MHVTL 1.8 will actually emulate for a vendor and
                      product string, and the element-address limits that come
                      with it - with a reference into the MHVTL source for each

data.py is what an operator may choose; personalities.py is what MHVTL will do
with the choice. libraries/validation checks a specification against both.
"""
from . import personalities
from .data import PROFILES, get_profile, list_profiles

__all__ = ['PROFILES', 'get_profile', 'list_profiles', 'personalities']
