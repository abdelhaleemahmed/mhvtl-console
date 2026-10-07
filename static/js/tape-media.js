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

    // What a cartridge of this density really holds - a fact about hardware.
    // Not what one is made at: that is defaultMb. Both come from the service,
    // which is the only thing that knows either.
    function nativeMb(density) {
        return info.native_mb[density] || null;
    }

    // How big one will be made - tapes.service.size_for, which walks the
    // shipped default, the settings file's default, and the settings file's
    // entry for this density. Nothing here derives it.
    //
    // These were one number until 6 October 2026, and the view computed it as
    // `gb * 1000`: the form suggested the native capacity and the service
    // created the native capacity, so the two agreed by coincidence. The
    // moment they became separate questions, a page deriving its own answer
    // would have filled in 12 TB while `mhvtl` made 1 GB - a cartridge's size
    // depending on which front end you used, which is what 500 and 500000
    // already were once.
    function defaultMb(density) {
        return info.default_mb[density] || info.unknown_size_mb;
    }

    const UNKNOWN_SIZE_MB = info.unknown_size_mb;

    // {mb, text}: the size to put in the field, and why - naming what the
    // cartridge really holds, so a full-size one is a choice rather than
    // something to go and look up.
    function suggestedSize(density) {
        const mb = defaultMb(density);
        const native = nativeMb(density);
        if (native && native !== mb) {
            return {mb: mb,
                    text: `${mb.toLocaleString()} MB. A real ${density} holds `
                          + `${native.toLocaleString()} MB - type that for a `
                          + 'full-size cartridge, or set a default in Settings'};
        }
        if (native) {
            return {mb: mb, text: `${mb.toLocaleString()} MB is ${density}'s native capacity`};
        }
        return {mb: mb,
                text: `MHVTL has no native capacity for ${density}; `
                      + `${mb.toLocaleString()} MB is suggested, `
                      + 'change it as needed'};
    }

    global.TapeMedia = {info, library, choices, suffixFor, fill, describe, nativeMb,
                        defaultMb, suggestedSize, UNKNOWN_SIZE_MB};
})(window);
