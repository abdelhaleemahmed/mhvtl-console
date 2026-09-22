/* Panels that re-fetch themselves on a timer.
 *
 * An element carrying data-refresh-url asks that URL for a fragment of HTML
 * every data-refresh-seconds and puts it inside itself. That is all this
 * console ever used htmx for - two panels on the library monitor - and htmx
 * was being fetched from unpkg.com, so on a host without internet the panels
 * simply never refreshed and every page paid a DNS timeout for a library it
 * did not use.
 *
 * While a fetch is in flight <html> carries .is-refreshing, which is how the
 * pulse beside "Auto-refresh" shows itself.
 */
(function () {
    var MINIMUM = 5;

    function refresh(box) {
        document.documentElement.classList.add('is-refreshing');
        fetch(box.dataset.refreshUrl, {
            headers: {'X-Requested-With': 'XMLHttpRequest'},
            credentials: 'same-origin',
        })
            .then(function (response) {
                // A session that has expired answers with the login page, and
                // pasting that into a panel would be worse than leaving the
                // last good reading in place.
                return response.ok ? response.text() : null;
            })
            .then(function (html) {
                if (html !== null) { box.innerHTML = html; }
            })
            .catch(function () {
                // A stopped service answers nothing; the panel keeps what it
                // last showed rather than blanking.
            })
            .then(function () {
                document.documentElement.classList.remove('is-refreshing');
            });
    }

    document.querySelectorAll('[data-refresh-url]').forEach(function (box) {
        var seconds = parseInt(box.dataset.refreshSeconds, 10) || 30;
        setInterval(function () { refresh(box); },
                    Math.max(seconds, MINIMUM) * 1000);
    });
})();
