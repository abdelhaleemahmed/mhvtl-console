/**
 * lang-switch.js — Language switcher for bilingual Sphinx builds.
 *
 * Detects the current build language from <html lang="...">, then injects
 * a switcher button into the RTD sidebar that takes the user to the same
 * page in the other language build.
 *
 * Assumes the two builds sit next to each other as siblings:
 *   _build/html/          ← English
 *   _build/html-ar/       ← Arabic
 *
 * Works with both file:// and http:// serving because it uses relative URLs
 * derived from Sphinx's own data-content_root attribute.
 */
document.addEventListener('DOMContentLoaded', function () {

    var lang        = document.documentElement.lang || 'en';       // 'ar' or 'en'
    var contentRoot = document.documentElement.getAttribute('data-content_root') || './';

    /*
     * contentRoot is the relative path from the current page back to the
     * docs root (e.g. './' for root pages, '../' for one level deep,
     * '../../' for two levels).
     *
     * From the docs root we go up one more directory to reach _build/,
     * then down into the sibling build directory, then back to the
     * current page path relative to the docs root.
     *
     * current page (relative to docs root) = strip contentRoot from href
     */
    var href     = window.location.href;
    var rootUrl  = resolveUrl(href, contentRoot);       // abs URL of docs root
    var pageRel  = href.slice(rootUrl.length) || 'index.html';  // page path from root

    // The sibling build directory is one level above the docs root
    var buildRoot   = resolveUrl(rootUrl, '../');
    var otherBuild  = lang === 'ar' ? 'html/' : 'html-ar/';
    var otherUrl    = buildRoot + otherBuild + pageRel;

    var linkText = lang === 'ar' ? 'English' : 'العربية';
    var label    = lang === 'ar' ? 'Switch to English' : 'التبديل إلى العربية';

    /* Build the switcher element */
    var switcher = document.createElement('div');
    switcher.className = 'lang-switcher';
    switcher.innerHTML =
        '<a href="' + otherUrl + '" title="' + label + '">' +
        '<span class="lang-switcher-icon">🌐</span> ' + linkText +
        '</a>';

    /* Inject below the project title in the RTD sidebar */
    var sideSearch = document.querySelector('.wy-side-nav-search');
    if (sideSearch) {
        sideSearch.appendChild(switcher);
    }
});

/**
 * Resolve a relative URL against a base URL (minimal implementation).
 * e.g. resolveUrl('http://x/a/b/c.html', '../') → 'http://x/a/'
 */
function resolveUrl(base, relative) {
    var a = document.createElement('a');
    a.href = base;
    var b = document.createElement('a');
    b.href = a.href;          // normalise
    /* Navigate relative to the base */
    var parts = b.href.split('/');
    parts.pop();              // remove filename
    relative.split('/').forEach(function (part) {
        if (part === '..') { parts.pop(); }
        else if (part && part !== '.') { parts.push(part); }
    });
    return parts.join('/') + '/';
}
