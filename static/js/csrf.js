/*
 * CSRF token handling for every request this console makes.
 *
 * Django rejects an unsafe request whose CSRF token is missing, which is what
 * stops another site from making your browser act on this console using your
 * session. The views used to be decorated with @csrf_exempt to avoid that
 * rejection; instead, send the token.
 *
 * Load this before any script that calls fetch() or XMLHttpRequest. It patches
 * both, so individual call sites need no change.
 */
(function () {
    'use strict';

    var UNSAFE = /^(POST|PUT|PATCH|DELETE)$/i;

    function readCookie(name) {
        var prefix = name + '=';
        var parts = (document.cookie || '').split(';');
        for (var i = 0; i < parts.length; i++) {
            var part = parts[i].trim();
            if (part.indexOf(prefix) === 0) {
                return decodeURIComponent(part.substring(prefix.length));
            }
        }
        return null;
    }

    function token() {
        // The cookie is the live value; the meta tag is a fallback for a page
        // served before the cookie was set.
        var meta = document.querySelector('meta[name="csrf-token"]');
        return readCookie('csrftoken') || (meta && meta.getAttribute('content')) || '';
    }

    /* Only same-origin requests get the token: sending it elsewhere would hand
     * it to whoever is on the other end. */
    function isSameOrigin(url) {
        if (!url) {
            return true;                       // fetch(undefined) means this page
        }
        try {
            return new URL(url, window.location.href).origin === window.location.origin;
        } catch (e) {
            return true;                       // a relative URL that URL() refused
        }
    }

    window.csrfToken = token();
    window.getCsrfToken = token;

    var originalFetch = window.fetch;
    if (originalFetch) {
        window.fetch = function (input, init) {
            init = init || {};
            var url = (typeof input === 'string') ? input : (input && input.url);
            var method = init.method || (input && input.method) || 'GET';

            if (UNSAFE.test(method) && isSameOrigin(url)) {
                var value = token();
                if (init.headers instanceof Headers) {
                    if (!init.headers.has('X-CSRFToken')) {
                        init.headers.set('X-CSRFToken', value);
                    }
                } else {
                    init.headers = init.headers || {};
                    if (!init.headers['X-CSRFToken']) {
                        init.headers['X-CSRFToken'] = value;
                    }
                }
                if (!init.credentials) {
                    init.credentials = 'same-origin';
                }
            }
            return originalFetch.call(this, input, init);
        };
    }

    var open = XMLHttpRequest.prototype.open;
    var send = XMLHttpRequest.prototype.send;

    XMLHttpRequest.prototype.open = function (method, url) {
        this._csrfNeeded = UNSAFE.test(method || '') && isSameOrigin(url);
        return open.apply(this, arguments);
    };

    XMLHttpRequest.prototype.send = function () {
        if (this._csrfNeeded) {
            try {
                this.setRequestHeader('X-CSRFToken', token());
            } catch (e) {
                /* header already set, or send() called in an odd state */
            }
        }
        return send.apply(this, arguments);
    };

    // htmx issues its own XHRs; this covers the ones it sends before the
    // prototype patch above would apply.
    document.addEventListener('htmx:configRequest', function (event) {
        if (UNSAFE.test(event.detail.verb || '')) {
            event.detail.headers['X-CSRFToken'] = token();
        }
    });
})();
