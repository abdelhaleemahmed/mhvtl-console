"""The operator pages that pair a form with a map are the same shape.

Mount/unmount and move both put a form on the left, a map of the library on
the right and the library's totals across the top. They drifted: the move page
kept a fixed 400px second column and its own slot sizing, so the same library
looked like two different libraries depending on which page you were on.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

PAIRED = ('/libraries/operator/mount/', '/libraries/operator/move/')


class PageShapeTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user('shape', password='x')
        self.client.force_login(user)
        session = self.client.session
        session['mhvtl_logged_in'] = True
        session.save()

    def page(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, url)
        return response.content.decode('utf-8', 'replace')

    def test_both_size_their_columns_the_same_way(self):
        """Ranges, not one fixed track: the map takes the room where there is
        room and gives it back rather than stacking."""
        for url in PAIRED:
            page = self.page(url)
            self.assertIn('minmax(320px, 1fr) minmax(340px, 700px)', page, url)
            self.assertNotIn('1fr 400px', page, url)

    def test_both_stack_at_the_same_width(self):
        for url in PAIRED:
            self.assertIn('@media (max-width: 1000px)', self.page(url))

    def test_both_put_the_totals_across_the_top(self):
        for url in PAIRED:
            page = self.page(url)
            self.assertIn('id="summary-bar"', page, url)
            self.assertLess(page.index('id="summary-bar"'),
                            page.index('class="main-grid"'), url)

    def test_both_draw_a_slot_the_same_size(self):
        """A slot is a slot; the same library should not look like two."""
        for url in PAIRED:
            page = self.page(url)
            self.assertIn('repeat(auto-fill, minmax(80px, 1fr))', page, url)
            self.assertIn('max-height: 300px', page, url)


class PalettePagesAgreeTests(TestCase):
    """The pages that draw a slot map draw it from the same table.

    Which generation a cartridge is was decided once, in
    services/tapes/palette.py. A page that fetches the endpoint without
    generations cannot render that decision, so it invents one - which is how
    the move page came to paint every cartridge a single blue while the mount
    page coloured them by generation.
    """

    def setUp(self):
        user = get_user_model().objects.create_user('palette', password='x')
        self.client.force_login(user)
        session = self.client.session
        session['mhvtl_logged_in'] = True
        session.save()

    def page(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, url)
        return response.content.decode('utf-8', 'replace')

    def test_both_maps_fetch_the_endpoint_that_carries_generations(self):
        for url in PAIRED:
            page = self.page(url)
            self.assertIn('/libraries/ajax/library-status-lto/', page, url)

    def test_neither_map_still_fetches_the_one_without(self):
        for url in PAIRED:
            page = self.page(url)
            self.assertNotIn("ajax/library-status/${libraryId}", page, url)

    def test_both_draw_a_tile_from_the_token(self):
        for url in PAIRED:
            self.assertIn('slot.generation_token', self.page(url), url)

    def test_full_is_a_state_on_both_and_not_a_colour(self):
        """The generation's own class provides the fill. `full` carried
        var(--info-bg) on the move page, which was one blue for every tape."""
        page = self.page('/libraries/operator/move/')
        self.assertNotIn('.slot-box.full { background:', page)

    def test_both_show_the_generations_this_library_holds(self):
        for url in PAIRED:
            page = self.page(url)
            self.assertIn('id="generation-legend"', page, url)
            self.assertIn('generations_present', page, url)

    def test_the_drive_removal_page_is_left_on_the_plain_endpoint(self):
        """Deliberate, not an oversight. It asks one question - does this
        drive hold a tape - and needs no generation and no slot. The LTO
        endpoint reads every cartridge's LTFS state to answer it, which is a
        lot of work for a yes or no."""
        page = self.page('/libraries/operator/drives/remove/')
        self.assertIn('/libraries/ajax/library-status/', page)
        self.assertNotIn('library-status-lto', page)
