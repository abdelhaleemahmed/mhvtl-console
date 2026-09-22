/* Sync one library's database row with the configuration files.
 *
 * The button existed on three pages; the function was defined on two of them,
 * so every click on the library detail page was a ReferenceError and the page
 * sat there showing a sync time from months ago. It lives here now, and the
 * pages load it.
 */
(function () {
    function csrfToken() {
        var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]*)/);
        if (match) { return decodeURIComponent(match[1]); }
        var field = document.querySelector('input[name="csrfmiddlewaretoken"]');
        return field ? field.value : '';
    }

    function say(message, kind) {
        // The pages that have their own notifications use them; the rest get
        // the browser's, rather than nothing at all.
        if (typeof window.showNotification === 'function') {
            window.showNotification(message, kind || 'info');
        } else if (kind === 'error') {
            window.alert(message);
        }
    }

    window.syncLibrary = function (libraryId, options) {
        var reloadWhenDone = !(options && options.reload === false);
        say('Syncing library ' + libraryId + '...', 'info');

        fetch('/libraries/ajax/sync-library/' + libraryId + '/', {
            method: 'POST',
            headers: {
                'X-CSRFToken': csrfToken(),
                'Content-Type': 'application/json'
            }
        })
            .then(function (response) { return response.json(); })
            .then(function (data) {
                if (!data.success) {
                    say('Sync failed: ' + (data.error || 'unknown error'), 'error');
                    return;
                }
                say('Library ' + libraryId + ' synced', 'success');
                if (typeof window.updateLibraryCard === 'function') {
                    window.updateLibraryCard(libraryId, data);
                } else if (reloadWhenDone) {
                    // The figures on this page come from the row just updated.
                    window.location.reload();
                }
            })
            .catch(function () { say('Network error during sync', 'error'); });
    };
})();
