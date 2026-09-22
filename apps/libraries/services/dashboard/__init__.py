"""Aggregated overview data for the dashboards.

Modules:
    service.py    get_dashboard_summary()

Its _section() helper is the failure-isolation pattern the rest of this layer
adopted: one failing collector degrades one tile, not the page.
"""
from .service import get_dashboard_summary

__all__ = ['get_dashboard_summary']
