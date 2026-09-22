/*
 * Which tapes a library can take, for the create-tape pages.
 *
 * The page embeds media_info (tape_operations_views.tape_media_context) with
 * {{ media_info|json_script:"media-info" }}:
 *
 *   libraries   {library_id: {drives, known, default,
 *                             media: [{density, suffix, writable,
 *                                      writable_in, read_only_in}]}}
 *   suffix      {density: data barcode suffix}
 *   worm_suffix {LTO density: WORM barcode suffix}
 *   labels      {density: "LTO8 (12 TB)"}
 *   native_mb   {density: native capacity in MB}
 *
 * Everything here comes from services/profiles/personalities.py, which is
 * transcribed from MHVTL and checked against it. The density list is rebuilt
 * for the chosen library so only media its drives load can be picked; the
 * service refuses anything else, so this is a convenience, not the check.
 */
(function (global) {
    'use strict';

    function readInfo() {
        const node = document.getElementById('media-info');
        try {
            return node ? JSON.parse(node.textContent) : null;
        } catch (err) {
            console.error('media-info is not valid JSON', err);
            return null;
        }
    }

    const info = readInfo() || {libraries: {}, suffix: {}, worm_suffix: {},
                                labels: {}, native_mb: {}};

    function library(libraryId) {
        return info.libraries[String(libraryId)] || null;
    }

    // The densities to offer: the library's own when its drives are known,
    // otherwise every density that can be created.
    function choices(libraryId) {
        const lib = library(libraryId);
        if (lib && lib.known) {
            return lib.media.map(m => ({density: m.density, writable: m.writable,
                                        note: m.writable ? '' : 'read-only'}));
        }
        return Object.keys(info.suffix).map(d => ({density: d, writable: true, note: ''}));
    }

    function suffixFor(density, tapeType) {
        if (tapeType === 'WORM' && info.worm_suffix[density]) {
            return info.worm_suffix[density];
        }
        return info.suffix[density] || '';
    }

    // Rebuild the <select>, keeping the current choice when it is still
    // offered, else the library's default. Returns the density selected.
    function fill(select, libraryId) {
        const previous = select.value;
        const lib = library(libraryId);
        const offered = choices(libraryId);
        select.innerHTML = '';
        offered.forEach(choice => {
            const option = document.createElement('option');
            option.value = choice.density;
            option.textContent = (info.labels[choice.density] || choice.density) +
                (choice.note ? ` - ${choice.note} in this library's drives` : '');
            select.appendChild(option);
        });
        const names = offered.map(c => c.density);
        const wanted = names.includes(previous) ? previous
            : (lib && lib.default && names.includes(lib.default)) ? lib.default
            : names[0] || '';
        select.value = wanted;
        return wanted;
    }

    // A sentence about the library's drives and the chosen density.
    function describe(libraryId, density) {
        const lib = library(libraryId);
        if (!libraryId) return 'Select a library to see the media its drives load.';
        if (!lib || !lib.drives.length) return 'This library has no drives in device.conf.';
        const models = [...new Set(lib.drives)].join(', ');
        if (!lib.known) {
            return `Drives: ${models}. MHVTL gives these drives no media list; every density is offered.`;
        }
        const entry = lib.media.find(m => m.density === density);
        if (!entry) return `Drives: ${models}.`;
        if (!entry.writable) {
            return `Drives: ${models}. ${density} is read-only in ${entry.read_only_in.join(', ')}: ` +
                   'tapes can be restored from but not written.';
        }
        const readOnly = entry.read_only_in.length
            ? ` (read-only in ${entry.read_only_in.join(', ')})` : '';
        return `Drives: ${models}. ${density} reads and writes in ${entry.writable_in.join(', ')}${readOnly}.`;
    }

    function nativeMb(density) {
        return info.native_mb[density] || null;
    }

    // MHVTL has no native capacity for some media (9840, 9940) and gives
    // them 1 GB; the forms suggest the same, and the operator can change it.
    const UNKNOWN_SIZE_MB = 1000;

    // {mb, text}: the size to suggest for a density, and why.
    function suggestedSize(density) {
        const native = nativeMb(density);
        if (native) {
            return {mb: native, text: `${native.toLocaleString()} MB is ${density}'s native capacity`};
        }
        return {mb: UNKNOWN_SIZE_MB,
                text: `MHVTL has no native capacity for ${density}; 1 GB is suggested, change it as needed`};
    }

    global.TapeMedia = {info, library, choices, suffixFor, fill, describe, nativeMb,
                        suggestedSize, UNKNOWN_SIZE_MB};
})(window);
