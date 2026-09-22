"""Bar widths are the same number in every language.

Django prints numbers in the active language's format, and in Arabic 81.9 is
"81,9". Inside `style="width: 81,9%"` that is not a CSS length, so the
browser ignores it and the bar draws empty - silently, and only in Arabic.
The console is being translated, so every number printed into a style goes
through |unlocalize. These render the four templates that have bars, in
Arabic, and check every template for a style that forgets.
"""
import re
from pathlib import Path

from django.template.loader import render_to_string
from django.test import SimpleTestCase
from django.utils import translation

from apps.libraries.services.console import disk, system

TEMPLATES = Path(__file__).resolve().parents[1] / 'templates'


def arabic(name, context):
    with translation.override('ar'):
        return render_to_string(name, context)


class LocalisedWidthTests(SimpleTestCase):

    def test_arabic_formats_a_decimal_with_a_comma(self):
        """The premise: without |unlocalize this is what a width would get."""
        with translation.override('ar'):
            from django.utils.formats import localize
            self.assertEqual(localize(81.9), '81,9')

    def test_the_tape_tiles(self):
        tape = {'barcode': 'K50001L8', 'density': 'LTO8', 'density_class': 'lto8',
                'slot': 1, 'kind': 'data', 'summary': '1.2 TB free of 11.4 TB',
                'used_percent': 81.9, 'fullness': 'filling', 'drive': None}
        page = arabic('libraries/partials/_tape_tiles.html', {'tapes': [tape]})
        self.assertIn('width: 81.9%', page)

    def test_the_drive_panel(self):
        drive = {'drive_id': 51, 'state': 'writing', 'label': 'writing',
                 'detail': '', 'percent': 29.6, 'fullness': 'normal'}
        page = arabic('libraries/partials/_library_activity.html', {'drives': [drive]})
        self.assertIn('width: 29.6%', page)

    def test_the_dashboard_memory_bar(self):
        page = arabic('libraries/console/dashboard.html', {
            'system_info': system.SystemInfo(memory_percent=81.9),
            'disk_usage': [disk.DiskUsage(path='/opt/mhvtl', percent=72)]})
        self.assertIn('width: 81.9%', page)
        self.assertIn('width: 72%', page)

    def test_the_disk_page(self):
        page = arabic('libraries/console/disk_usage.html', {
            'disk_usage': [disk.DiskUsage(path='/opt/mhvtl', percent=72)]})
        self.assertIn('width: 72%', page)

    def test_no_template_prints_a_value_into_a_style_without_unlocalize(self):
        """The guard: a new bar written the old way fails here, by name."""
        pattern = re.compile(r'style="[^"]*\{\{([^}]*)\}\}')
        offenders = []
        for path in sorted(TEMPLATES.rglob('*.html')):
            for number, line in enumerate(path.read_text().splitlines(), 1):
                for value in pattern.findall(line):
                    if 'unlocalize' not in value:
                        offenders.append(f'{path.relative_to(TEMPLATES)}:{number} {{{{{value}}}}}')
        self.assertFalse(offenders, '\n'.join(offenders))
