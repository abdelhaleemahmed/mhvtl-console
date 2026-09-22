/* The default-password warning, and the x that puts it away.
 *
 * The bar is on every page while the console still answers to the password it
 * shipped with, which on a machine administered by one person is a line they
 * have already read. Dismissing it is remembered in this browser, so it stays
 * gone; it is not a server setting, and nothing about the password changes.
 *
 * It disappears on its own, everywhere, the moment the password is changed:
 * the server stops rendering it at all.
 *
 * Loaded by templates/includes/_default_password_warning.html, immediately
 * after the bar itself rather than deferred, so a dismissed bar is never
 * painted before being taken away.
 */
(function () {
    var KEY = 'mhvtl-default-password-dismissed';
    var bar = document.querySelector('.default-password-warning');
    if (!bar) { return; }

    var dismissed = false;
    try {
        // Private windows and blocked site data throw rather than answer.
        dismissed = window.localStorage.getItem(KEY) === '1';
    } catch (error) {
        dismissed = false;
    }
    if (dismissed) {
        bar.parentNode.removeChild(bar);
        return;
    }

    var button = bar.querySelector('.alert-dismiss');
    if (!button) { return; }
    button.addEventListener('click', function () {
        try {
            window.localStorage.setItem(KEY, '1');
        } catch (error) {
            // Nowhere to remember it: this page is still rid of it.
        }
        bar.parentNode.removeChild(bar);
    });
})();
