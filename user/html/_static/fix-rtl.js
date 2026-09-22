/**
 * RTL post-processing for Arabic Sphinx docs.
 * Replaces RTD theme strings that have no Arabic translation.
 */
document.addEventListener('DOMContentLoaded', function () {

    /* ── Footer prev/next buttons ─────────────────────────────── */
    document.querySelectorAll('.rst-footer-buttons a').forEach(function (btn) {
        btn.childNodes.forEach(function (node) {
            if (node.nodeType === Node.TEXT_NODE) {
                var t = node.textContent.trim();
                if (t === 'Previous') node.textContent = ' السابق';
                if (t === 'Next')     node.textContent = 'التالي ';
            }
        });
    });

    /* ── Breadcrumbs / nav ────────────────────────────────────── */
    document.querySelectorAll('a, span, li').forEach(function (el) {
        if (el.childNodes.length === 1 &&
            el.childNodes[0].nodeType === Node.TEXT_NODE) {
            var t = el.childNodes[0].textContent.trim();
            var map = {
                'View page source': 'عرض المصدر',
                'About these documents': 'حول هذه الوثائق',
                'Index':    'الفهرس',
                'Search':   'بحث',
                'Copyright': 'حقوق النشر',
            };
            if (map[t]) el.childNodes[0].textContent = map[t];
        }
    });

    /* ── Search box placeholder ───────────────────────────────── */
    var sb = document.querySelector('.wy-side-nav-search input[type="text"]');
    if (sb) sb.placeholder = 'ابحث في التوثيق';

    /* ── Search results page ──────────────────────────────────── */
    var sr = document.querySelector('h2#search-results');
    if (sr) sr.textContent = 'نتائج البحث';

});
