/*
 * Theme selection for the console pages.
 *
 * Themes are defined entirely in static/css/mhvtl-console.css as blocks of CSS
 * variables; this file only decides which one is applied and remembers the
 * choice. To add a theme, add a [data-theme="..."] block to the stylesheet and
 * one entry to THEMES below.
 *
 * Load it in <head>, before the page renders: the stored theme is applied to
 * <html> immediately so the page never paints in the wrong colours.
 */
(function () {
    'use strict';

    var THEMES = [
        {id: 'navy',     label: 'Console Navy'},
        {id: 'graphite', label: 'Graphite'},
        {id: 'daylight', label: 'Daylight'},
        {id: 'sepia',    label: 'Sepia'}
    ];
    var DEFAULT_THEME = 'navy';
    var STORAGE_KEY = 'mhvtl-theme';

    function stored() {
        try {
            return localStorage.getItem(STORAGE_KEY);
        } catch (e) {
            return null;                 // private window, or site data blocked
        }
    }

    function known(id) {
        return THEMES.some(function (theme) { return theme.id === id; });
    }

    function apply(id) {
        if (!known(id)) { id = DEFAULT_THEME; }
        document.documentElement.setAttribute('data-theme', id);
        return id;
    }

    var current = apply(stored() || DEFAULT_THEME);

    window.mhvtlTheme = {
        current: function () { return current; },
        themes: THEMES,
        set: function (id) {
            current = apply(id);
            try { localStorage.setItem(STORAGE_KEY, current); } catch (e) { /* ignore */ }
            return current;
        }
    };

    // Fill in the picker once the DOM exists. A page without one just keeps
    // the stored theme.
    document.addEventListener('DOMContentLoaded', function () {
        var picker = document.getElementById('theme-picker');
        if (!picker) { return; }

        THEMES.forEach(function (theme) {
            var option = document.createElement('option');
            option.value = theme.id;
            option.textContent = theme.label;
            if (theme.id === current) { option.selected = true; }
            picker.appendChild(option);
        });

        picker.addEventListener('change', function () {
            window.mhvtlTheme.set(picker.value);
        });
    });
})();
