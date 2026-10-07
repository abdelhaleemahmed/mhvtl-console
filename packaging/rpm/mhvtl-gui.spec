# The package name stays mhvtl-gui: it is the upgrade path for every host
# that already has one, along with /opt/mhvtl-gui, the mhvtl-gui account and
# mhvtl-gui.service below. The product is called mhvtl-console from 3.3.0,
# which is a displayed name and lives in services/about/service.py.
%define name mhvtl-gui
%define version 3.4.0
%define release 1%{?dist}
%define installdir /opt/mhvtl-gui
%define servicename mhvtl-gui

Name:           %{name}
Version:        %{version}
Release:        %{release}
Summary:        Web-based GUI for MHVTL (Virtual Tape Library)
License:        GPL-2.0-only
Packager:       Ahmed Abdelhaleem Ahmed <ahmedhal@gmail.com>
URL:            https://github.com/abdelhaleemahmed/mhvtl-console
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch
# Django 5.2 (the LTS this app targets) needs Python 3.10 or newer, so the
# distribution's default python3 - 3.9 on EL9 - cannot run it.
BuildRequires:  python3.12-devel
Requires:       python3.12
Requires:       python3.12-pip
Requires:       mhvtl >= 1.7
Requires:       sqlite

%description
MHVTL GUI is a Django-based web interface for managing MHVTL
(Linux Virtual Tape Library). It provides an easy-to-use interface
for creating and managing virtual tape libraries, drives, and media.

Developed by Ahmed Abdelhaleem Ahmed.

%prep
%setup -q

%build
# Nothing to build - Python application

%install
rm -rf %{buildroot}

# Create directories
mkdir -p %{buildroot}%{installdir}
mkdir -p %{buildroot}/etc/mhvtl-gui
mkdir -p %{buildroot}/var/www/mhvtl/static
mkdir -p %{buildroot}/var/www/mhvtl/media
mkdir -p %{buildroot}/var/log/mhvtl
mkdir -p %{buildroot}/var/lib/mhvtl-gui
mkdir -p %{buildroot}/var/lib/mhvtl-gui/targetcli
mkdir -p %{buildroot}%{installdir}/.gunicorn
mkdir -p %{buildroot}%{_unitdir}
mkdir -p %{buildroot}/etc/sudoers.d

# Copy application files
cp -r apps %{buildroot}%{installdir}/
cp -r mhvtl_system %{buildroot}%{installdir}/
cp -r mhvtl_cli %{buildroot}%{installdir}/

# The mhvtl command itself, on PATH, running the CLI from the installed tree
mkdir -p %{buildroot}%{_bindir}
install -m 755 packaging/bin/mhvtl %{buildroot}%{_bindir}/mhvtl
cp -r templates %{buildroot}%{installdir}/
cp -r static %{buildroot}%{installdir}/
cp manage.py %{buildroot}%{installdir}/
cp pyproject.toml %{buildroot}%{installdir}/

# Install systemd service
install -m 644 packaging/rpm/mhvtl-gui.service %{buildroot}%{_unitdir}/

# Install nginx config as sample (optional)
install -m 644 packaging/rpm/mhvtl-gui-nginx.conf %{buildroot}%{installdir}/nginx.conf.sample

# Install environment template
install -m 640 packaging/rpm/env.template %{buildroot}/etc/mhvtl-gui/env

# The commented example of presets.toml. Installed as .example and never as
# presets.toml: the live file is the operator's, written by `mhvtl library
# preset set`, and an upgrade must not replace it. 644 because `mhvtl library
# preset list` names this path in its output and anyone may run that.
install -m 644 packaging/presets.toml.example \
    %{buildroot}/etc/mhvtl-gui/presets.toml.example

# The commented example of settings.toml, on exactly the same terms: the live
# file is the operator's, written by `mhvtl settings set` and by the Settings
# page, and there is none until something is saved.
install -m 644 packaging/settings.toml.example \
    %{buildroot}/etc/mhvtl-gui/settings.toml.example

# Install sudoers file for passwordless sudo on MHVTL commands
install -m 440 packaging/rpm/mhvtl-gui.sudoers %{buildroot}/etc/sudoers.d/mhvtl-gui

%pre
# Create mhvtl-gui user if it doesn't exist
getent group mhvtl >/dev/null || groupadd -r mhvtl
getent passwd mhvtl-gui >/dev/null || \
    useradd -r -g mhvtl -d %{installdir} -s /sbin/nologin \
    -c "MHVTL GUI Service Account" mhvtl-gui
exit 0

%post
# Create virtual environment and install dependencies.
#
# The directory is REMOVED FIRST. `python3.12 -m venv` over an existing
# virtualenv leaves the older bin/python3 symlink pointing where it did and
# both site-packages trees in place, so upgrading a host first installed in the
# 3.9 era left one virtualenv holding Python 3.9 with Django 4.2 beside Python
# 3.12 with Django 5.2 - 117 MB of the wrong one, and /usr/bin/mhvtl running it
# while the service ran the other. It worked, which is why it went unnoticed
# until a command needed tomllib and found a 3.9 standard library.
#
# Nothing in here is worth keeping: it is rebuilt from the requirements every
# time. The database, the configuration and the logs live outside it.
rm -rf %{installdir}/venv
python3.12 -m venv %{installdir}/venv
%{installdir}/venv/bin/pip install --upgrade pip
cd %{installdir}
%{installdir}/venv/bin/pip install .

# Generate secret key if not set
# env.template ships SECRET_KEY=CHANGE_ME_TO_A_RANDOM_STRING, so testing for the
# key's presence always succeeded and every install ran on the placeholder.
# Generate one whenever the value is missing or still the placeholder.
if grep -qE "^SECRET_KEY=(CHANGE_ME.*)?$" /etc/mhvtl-gui/env 2>/dev/null \
   || ! grep -q "^SECRET_KEY=" /etc/mhvtl-gui/env 2>/dev/null; then
    SECRET_KEY=$(python3.12 -c 'import secrets; print(secrets.token_urlsafe(50))')
    # '#' as the sed delimiter: a generated key can contain '/'.
    sed -i "s#^SECRET_KEY=.*#SECRET_KEY=$SECRET_KEY#" /etc/mhvtl-gui/env
fi

# Copy database to persistent location
if [ ! -f /var/lib/mhvtl-gui/db.sqlite3 ]; then
    touch /var/lib/mhvtl-gui/db.sqlite3
fi

# Create symlink for database
ln -sf /var/lib/mhvtl-gui/db.sqlite3 %{installdir}/db.sqlite3

# Run migrations
cd %{installdir}
export DJANGO_SETTINGS_MODULE=mhvtl_system.settings.production
%{installdir}/venv/bin/python manage.py migrate --noinput

# Populate initial brand and model data
%{installdir}/venv/bin/python manage.py populate_library_data --noinput 2>/dev/null || \
    %{installdir}/venv/bin/python manage.py populate_library_data 2>/dev/null || true

# Sync existing MHVTL libraries into Django DB
%{installdir}/venv/bin/python manage.py sync_config --quiet 2>/dev/null || true

# Collect static files
%{installdir}/venv/bin/python manage.py collectstatic --noinput

# Set permissions AFTER migrate and collectstatic so that log files,
# static files, and database files created during those steps are also
# owned by the service user (not root).
chown -R mhvtl-gui:mhvtl %{installdir}
chown -R mhvtl-gui:mhvtl /var/www/mhvtl
chown -R mhvtl-gui:mhvtl /var/log/mhvtl
chown -R mhvtl-gui:mhvtl /var/lib/mhvtl-gui/targetcli
chown mhvtl-gui:mhvtl %{installdir}/.gunicorn
chown -R mhvtl-gui:mhvtl /var/lib/mhvtl-gui
chmod 640 /etc/mhvtl-gui/env

# Grant mhvtl group write access to MHVTL config directory so the
# service can create/update device.conf and library_contents files
if [ -d /etc/mhvtl ]; then
    chown -R root:mhvtl /etc/mhvtl
    chmod 775 /etc/mhvtl
    chmod 664 /etc/mhvtl/* 2>/dev/null || true
fi

# Grant mhvtl group write access to MHVTL data directory
if [ -d /opt/mhvtl ]; then
    chown root:mhvtl /opt/mhvtl
    chmod 775 /opt/mhvtl
fi

# Fix MHVTL script permissions (update_device.conf ships as 700)
chmod 755 /usr/bin/update_device.conf 2>/dev/null || true

# A certificate for nginx: the settings serve over HTTPS by default, so one
# has to exist. A self-signed pair is made only when there is none - a real
# certificate dropped in these two paths is never touched.
CERT=/etc/pki/tls/certs/mhvtl-gui.crt
KEY=/etc/pki/tls/private/mhvtl-gui.key
if [ ! -f "$CERT" ] || [ ! -f "$KEY" ]; then
    if command -v openssl >/dev/null 2>&1; then
        openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
            -keyout "$KEY" -out "$CERT" \
            -subj "/CN=$(hostname -f 2>/dev/null || hostname)" \
            -addext "subjectAltName=DNS:$(hostname -f 2>/dev/null || hostname),DNS:localhost,IP:127.0.0.1" \
            >/dev/null 2>&1 && chmod 600 "$KEY" && chmod 644 "$CERT"
        echo "A self-signed certificate was created for nginx: $CERT"
    else
        echo "openssl is not installed: put a certificate in $CERT and its key in $KEY,"
        echo "or set HTTPS=0 in /etc/mhvtl-gui/env to serve over plain HTTP."
    fi
fi

# Enable and start service
systemctl daemon-reload
systemctl enable %{servicename}

# On an upgrade, restart it: the workers hold the code they were started
# with, so an upgraded console keeps serving the previous version until
# somebody notices - the footer said 2.0.0 for twenty minutes after 2.1.0
# was installed. try-restart does nothing if the service is not running,
# which is what a first install wants.
if [ $1 -gt 1 ]; then
    systemctl try-restart %{servicename} >/dev/null 2>&1 || :
fi

echo ""
echo "======================================================"
echo "  MHVTL GUI Installation Complete"
echo "======================================================"
echo ""
echo "  1. Edit configuration: /etc/mhvtl-gui/env"
echo "     - Set ALLOWED_HOSTS to your server IP/hostname"
echo "  2. Put the nginx configuration in place, for HTTPS:"
echo "       cp %{installdir}/nginx.conf.sample /etc/nginx/conf.d/mhvtl-gui.conf"
echo "       systemctl enable --now nginx"
echo "  3. Start service: systemctl start mhvtl-gui    (an upgrade restarts it)"
echo "  4. Access: https://your-server/   (http://your-server/ redirects to it)"
echo ""
echo "  The certificate is self-signed, so the browser will ask once."
echo "  Replace /etc/pki/tls/certs/mhvtl-gui.crt and its key with your own,"
echo "  or set HTTPS=0 in /etc/mhvtl-gui/env to serve over plain HTTP."
echo ""
echo "======================================================"

%preun
if [ $1 -eq 0 ]; then
    systemctl stop %{servicename} >/dev/null 2>&1 || :
    systemctl disable %{servicename} >/dev/null 2>&1 || :
fi

%postun
if [ $1 -eq 0 ]; then
    systemctl daemon-reload
fi

%files
%defattr(-,root,root,-)
%{installdir}
%{_bindir}/mhvtl
%{_unitdir}/mhvtl-gui.service
%dir /etc/mhvtl-gui
%config(noreplace) /etc/mhvtl-gui/env
# Not %config: they are documentation, so an upgrade should replace them. The
# live presets.toml and settings.toml are not packaged at all - each exists
# only once somebody saves something, which is why nothing here could ever
# overwrite one.
/etc/mhvtl-gui/presets.toml.example
/etc/mhvtl-gui/settings.toml.example
%attr(440,root,root) /etc/sudoers.d/mhvtl-gui
%dir /var/www/mhvtl/static
%dir /var/www/mhvtl/media
%dir /var/log/mhvtl
%dir /var/lib/mhvtl-gui
%dir /var/lib/mhvtl-gui/targetcli

%changelog
* Wed Oct 07 2026 Ahmed Abdelhaleem Ahmed <ahmedhal@gmail.com> - 3.4.0-1
- A new cartridge is 1 GB, not its native capacity. An LTO-8 holds 12 TB and
  this made one that big, on a system whose purpose is testing: the media
  files are sparse so the size costs no disk, but at the 55 MB/s a typical
  host writes, filling one takes 63 hours - which put end of tape, multi-
  volume spanning and the fullness bar out of reach at the default. Tapes you
  already have keep the size they were made with.
- Anyone who wants a realistic cartridge says so, in /etc/mhvtl-gui/
  settings.toml. Four levels, each narrower than the last: the shipped 1 GB,
  then `tape.size.default` for every density, then `tape.size.<density>` for
  one, then the size given to a single create. Nothing else holds a capacity.
- A Settings page in the System Console, and `mhvtl settings` with list, get,
  set and reset. Both show where each value came from, and what the cartridge
  really holds beside it, so choosing a full-size tape needs no lookup. Sizes
  are written 1000, 2000GB or 12TB; decimal, as tape capacity is quoted, and
  MiB/GiB are refused because 12 TB and 12 TiB differ by ten per cent.
- `mhvtl settings list` narrows by section, family or key, and case never
  matters: `tape.size`, `tape.size.lto`, `tape.size.3592`, `tape.size.LTO8`.
  Thirty-three rows is not a list anybody reads.
- A size per kind of cartridge, not one per library. A library holding LTO-8
  and DLT-4 holds two capacities, so the size travels on the media run - a
  field on each row of the creation wizard, `--media-size DENSITY:SIZE` on
  the command line, and `size_mb` in a preset.
- /etc/mhvtl-gui/settings.toml.example lists every density commented out with
  what that cartridge really holds beside it. It is never installed as the
  live file, so an upgrade cannot overwrite a setting.
- Fixed: the creation drop-downs said "LTO8 (12 TB)" about a tape that would
  be made at 1 GB - out by a factor of twelve thousand, in the one place an
  operator is choosing. They now name the size the create will produce, and
  follow the setting.
- Fixed: creating a tape sent you to the tape inventory with no library
  chosen, asking you to pick the one you had just put a tape in. Every form's
  error path had the same hole, where it cost more - the library was cleared
  along with the typing. Nine views: create, bulk create, delete, library
  online and offline, add and remove drive, adopt, and LTFS, mount and move.
* Tue Oct 06 2026 Ahmed Abdelhaleem Ahmed <ahmedhal@gmail.com> - 3.3.1-1
- Test setup only; the application is byte for byte 3.3.0. Eight tests of the
  `mhvtl preset` write verbs passed on a developer's host and failed on a
  build runner, because they are gated on membership of the mhvtl group - a
  fact about the machine, not about the verb - and the test class never
  granted it. `mhvtl preset` arrived in 3.2.0 and 3.2.0 was never published,
  so they reached a runner for the first time with 3.3.0. They now grant it
  the way every other mutating command's tests do, and the gate itself is
  still tested: can_write for root, for a group member and for neither.
* Tue Oct 06 2026 Ahmed Abdelhaleem Ahmed <ahmedhal@gmail.com> - 3.3.0-1
- The product is now called mhvtl-console. `mhvtl --version`, `mhvtl status
  system` and the About page say it; `mhvtl-gui` was also the name of an
  older, unrelated PHP interface to MHVTL, so a bug report quoting a version
  did not say which program it was about. The package, /opt/mhvtl-gui, the
  mhvtl-gui account and mhvtl-gui.service keep their names: those are the
  upgrade path, not a label.
- One page frame for all fifty pages. There were five, no two of whose
  navigations agreed, so the header, the theme picker, who is logged in and
  the footer with the version reached some pages and not others. One
  base.html now, extended by a thin wrapper per section.
- One meaning for each URL, and a way in from anywhere. "Libraries" led to
  two different pages depending on which header you clicked it from, the
  create-a-library form was unreachable from most of the console, and three
  pages had no inbound link at all.
- The landing page counts what the host holds. Its five numbers had never
  been anything but zero: the context keys were never set, the poller read
  keys the endpoint did not return, and both counted database rows instead of
  device.conf. It reads device.conf, reports what it cannot read rather than
  calling it zero, and says it in one sentence.
- A library can hold more than one generation of drive and tape. The form
  takes several drive types and several media types in one library, and the
  web can no longer write an IBM LTO-8 drive into a StorageTek library -
  the form asks the profile service what the vendor makes instead of
  deciding for itself.
- Adopting a loose tape offers only the libraries whose drives could load
  it. Every library was offered for every tape while the service refused the
  ones that cannot, so the form offered a choice it knew would fail and said
  so only afterwards. The CLI names where the tape could go when it refuses.
- Presets say what they build, in one line, on the page and in the terminal;
  `preset rename` guards the name the file is written with; the preset in use
  is marked on its whole card rather than by one word.
- Type and contrast, measured rather than judged: 14px is the smallest type
  anywhere, forms are 15px, the navigation is 17px and stays legible in all
  four themes, and the header carries its own colours because the bar is not
  the page.
- Fixed: a stray brace in base.css closed a max-width:768px block four rules
  early, so the console rendered as a phone at every width. A field was
  focused on every page load. The database sync reported a failure as a
  result instead of raising.
- /etc/mhvtl-gui/presets.toml.example ships a library for every vendor, at
  the newest tape that vendor's drives write: eleven tables covering all nine
  profiles, where it had covered two and stopped at LTO8. ADIC, Dell, IBM,
  Overland, Spectra and Quantum reach LTO10, HP's own catalogue ends at LTO8,
  Sony is AIT4, and StorageTek gets two - its own T10000C and an SL500
  carrying an IBM LTO drive. Quantum is the one to read: its profile default
  is an SDLT600, so `--profile QUANTUM` alone builds an SDLT library.
- Removed two AJAX endpoints no commit ever called, and renamed the tape
  filter `?media=` to `?wanted_media=` - the rows on that page also use
  `media`, and QueryDict.get returns the last value, so the filter read a
  row's value instead of its own.
* Sun Oct 04 2026 Ahmed Abdelhaleem Ahmed <ahmedhal@gmail.com> - 3.2.0-1
- Two nouns for what a library is built from. `mhvtl profile` is a vendor's
  catalogue - which library models it makes, which drives each takes, which
  densities each drive writes - and `mhvtl preset` is a configuration you
  built from one and named. Both are enumerated by `list` and explained by
  `show`; a profile has no write verbs at all, because nothing may edit what
  a vendor makes.
- `mhvtl library create --interactive` asks for each choice, offering only
  what the catalogue holds for the answers so far, and ends with create,
  create-and-keep-as-a-preset, preview the device.conf, or quit. Nothing is
  written until the last answer, so quitting or Ctrl-C leaves the host
  untouched.
- Presets: build one up a piece at a time with `preset set`, keep the
  configuration a create has just proved with `--save-preset NAME`, use it
  with `create --preset NAME`. `preset show` reports what a preset fixes AND
  what it leaves to its profile, so a preset naming only a vendor still
  describes a whole library. /etc/mhvtl-gui/presets.toml.example ships as a
  commented example of every key.
- The setup wizard offers the same presets as the terminal, through the same
  service: a vendor's form lists that vendor's presets, ?preset=NAME fills
  the form in, and a name at the bottom keeps the configuration after a
  successful create.
- A new cartridge is now as big as its density - an LTO-8 is 12 TB, an LTO-6
  2.5 TB - taken from the capacities MHVTL itself uses. Six places had a
  number of their own and they disagreed: a library's own tapes were 500 MB
  while a tape added to it afterwards was 500 GB. The media files are sparse,
  so the capacity costs nothing until something writes to it; --size-mb and
  the forms' size field still override it.
- The vendor page, the setup form and the terminal now read one catalogue
  instead of composing it three times. Two rules the page's script had worked
  out for itself were wrong: it offered a drive that can only READ the chosen
  tape as a way to write it, and for LTO-9 it chose a half-height ULT3580-HH9
  where the rule gives a ULT3580-TD9.
- Removed, and never released: `mhvtl drive models`, which put the vendor
  summary under the drive noun and left nothing able to list a profile's
  library models - it is `mhvtl profile show` now - and `mhvtl library preset
  ...`, which is `mhvtl preset ...`.
- Fixed: a closed pipe (`mhvtl ... | head`) reported a Python error instead
  of exiting quietly; --profile's help named a command that was never built;
  `preset set --serial` parsed and did nothing; a preset could hold a
  `size_mb` that nothing read; and two leftover .orig files were being
  packaged.

* Fri Oct 02 2026 Ahmed Abdelhaleem Ahmed <ahmedhal@gmail.com> - 3.1.0-1
- An About page, linked from the header of every page: the version, the
  author, the licence, links to the published documentation, and what a bug
  report needs about this host - python, django, the kernel, whether
  mhvtl.target is up.
- mhvtl --version, which answered "unrecognized arguments" before this, and
  the console's version in mhvtl status system - an upgrade that did not
  restart gunicorn is invisible otherwise.
- Every page behind a login says which version it is. It used to appear on 8
  pages of about 46, and on none of the operator pages.
- The identity is decided once in mhvtl_system and composed by
  services/about, so the page and the terminal cannot disagree.
- Fixed: <meta name="author"> said "MHVTL Community"; test_sudoers.py read a
  file from outside the repository and so could only pass on one machine; no
  template can leak a multi-line {# #} comment into a page any more.

* Fri Oct 02 2026 Ahmed Abdelhaleem Ahmed <ahmedhal@gmail.com> - 3.0.0-1
- LTFS: a cartridge can be opened as a filesystem from the console or the
  command line. Which drives LTFS will open and which cartridges are volumes,
  mounting and unmounting one, checking it, and provisioning a library that
  has no capable drive - adding one LTFS accepts, and creating media its
  drives can WRITE rather than merely load. Its own page and its own group on
  the operator dashboard.
- LTFS: a drive whose filesystem is mounted cannot be unloaded, from the page
  or the command line. Pulling the cartridge out from under a live mount left
  ltfs holding a device with no medium; a daemon restart did exactly that
  during development.
- Tape generations are drawn in the real LTO cartridge shell colours, tailored
  so that generations the shell colours give the same colour can still be told
  apart - LTO-6 and LTO-8 are both "Dark red", and a library can hold both.
  All ten generations, where there were five; LTO-4 no longer borrows LTO-5's
  colour and LTO-10 is no longer drawn as "unknown".
- The rule that decides it lives in one place, services/tapes/palette.py, and
  is rendered twice: by the stylesheet for a browser and by mhvtl_cli/colour.py
  for a terminal. The mount page used to carry two more copies of it, which
  disagreed with each other.
- Mounting and unmounting a tape are one page. Click a tape and an empty drive
  to mount; click a loaded drive to unload it. The destination slot defaults to
  the one the cartridge came from and can be changed. /operator/unmount/
  redirects.
- New commands: `mhvtl op layout` draws a library in the terminal - drives,
  slots, the import/export port and a legend - and `mhvtl op palette` prints
  the generations and their colours.
- Unexporting a library finds its target by the backstores it exports rather
  than by a name generated from today's date. A library exported in one month
  and unexported in the next was not found, the miss was reported as success,
  and its backstores were deleted anyway.
- An LTFS-capable drive added to a library now has its own daemon started. It
  was written to device.conf and left with no device node at all.
- BREAKING: the tape JSON field `density_class` is now `generation_token`, and
  services.tapes.barcodes.generation_class() and GENERATION_CLASS are removed.
- 1582 tests.

* Tue Sep 22 2026 Ahmed Abdelhaleem Ahmed <ahmedhal@gmail.com> - 2.1.1-1
- The disk usage page shows the disks again. It asked the disk service for
  fields it never had, so on every host it drew an empty bar, a bare "%" and
  " MB" for the sizes. It now shows the percentage, the sizes df reports, and
  the same normal/warning/danger level as the dashboard's disk bar - which
  moves its warning from above 70% to 75%, matching the dashboard
- The memory level on the dashboard is decided by the system service rather
  than by the page, with the same thresholds as before (warning above 60%,
  danger above 80%), and `mhvtl console system` now prints it too
- Bars keep their width when the page is shown in Arabic. Numbers inside a
  style were printed in the page's language, and in Arabic 81.9 is "81,9",
  which a browser ignores - the memory, drive and tape bars would have drawn
  empty
- Licensed under the GNU GPL version 2, the same licence as MHVTL, with a
  LICENSE file in the source (the package said GPLv3 before)
- Documentation: a beginner tutorial that builds a small console from an
  empty directory, chapter by chapter, with every chapter's code checked to
  run and to match what the chapter prints

* Mon Sep 21 2026 Ahmed Abdelhaleem Ahmed <ahmedhal@gmail.com> - 2.1.0-1
- The library page is where one library is worked on: everything that can be
  done to it, with the library already chosen, and what each of its drives is
  doing right now
- Live drive state, from `vtlcmd <drive> stats` - our addition to MHVTL - so a
  drive being written to can be watched during the backup, which mt and mtx
  cannot do while the kernel holds the SCSI reservation for the initiator. The
  same answer on the terminal: mhvtl status activity <library>
- The library page and the monitor page both show it, with a bar for how full
  the tape is; the panel is rendered by the server and swapped in, so nothing
  the console can do is missing from the command line
- Tapes are shown as tiles with how full each one is. Capacity now comes from
  the tape's own MAM file, so a tape in a slot has a size at all - the
  CAPACITY MB column of `mhvtl tape list` was printing "-" for every tape not
  in a drive
- The console pages share one frame and one navigation: nothing linked to the
  kernel modules or the disk usage before, both were reachable only by typing
  the address. The SCSI devices page joins them, and prints the SCSI address
  and device type it had been leaving blank since the service layer renamed
  those fields
- Every page follows the theme, including the ones that were still drawing
  themselves in fixed colours - a white card with unreadable labels on any
  dark theme
- The default-password warning can be dismissed, and is remembered
- htmx removed. It was fetched from unpkg.com on every page for two panels on
  one, so on a host with no route out those panels never refreshed
- Static files are no longer served as immutable: with no hash in their names,
  an upgraded console kept handing the previous release's stylesheet and
  scripts to anyone who had visited before
- 1163 tests

* Sun Sep 20 2026 Ahmed Abdelhaleem Ahmed <ahmedhal@gmail.com> - 2.0.0-1
- Command line: mhvtl(8) covers libraries, drives, tapes, operations, status,
  services, configuration, SCSI mapping, iSCSI and the host console
- iSCSI: exports survive a reboot - bindings are recorded per daemon start,
  devices withheld at boot are remapped, and stale bindings are reported and
  rebound from the dashboard; CHAP per ACL and target-wide, with the
  target's own credentials for mutual CHAP
- Tapes: a new library keeps four empty slots, so a tape can be added to it
  without reconfiguring; tape media, tape adopt (a tape whose data is on disk
  put back into a library) and library slots (add or set the empty ones)
- Every command that edits library_contents says the library must be
  restarted before its robot reports the change, and the operator's Library
  Status page warns when the two disagree
- library orphans reports tapes on disk that no library lists, apart from the
  rest and never cleaned, because they are data
- Web interface: tapes on the library page, a tape inventory that adopts
  loose tapes, empty-slot control, a working Stop button, and the console
  showing the host facts it was already reading
- HTTPS by default: nginx terminates TLS on 8443 with a certificate the
  installer creates, port 8080 redirects to it, and the session and CSRF
  cookies are secure. HTTPS=0 in the env file serves over plain HTTP
- Library configuration page disabled: its Save rebuilt the library without
  saying so; it is being redesigned
- Kernel ch driver blacklisted to stop it leaking references to the
  libraries' devices; a patch for it is in the repository
- Documentation: two tutorials built from the recorded videos (exporting with
  Linux tools, and tapes), the known-issues list and the phase-two plan
- 1060 tests

* Sun Apr 05 2026 Ahmed Abdelhaleem Ahmed <ahmedhal@gmail.com> - 1.1.1-1
- Fix dashboard sync to auto-create Django DB records for MHVTL libraries
  discovered in device.conf (previously only toggled active/inactive)
- Fix discovery AJAX endpoints to use MHVTLLibraryService instead of
  non-existent DiscoveryService; Refresh/Full Scan/Sync buttons now work
- Fix decorator leak on _sync_mhvtl_to_django helper causing request errors
- Fix RPM post-install: run populate_library_data for initial brand/model data
- Fix RPM post-install: set file ownership after migrate and collectstatic
  to prevent log file permission errors on service start
- Fix RPM post-install: set /etc/mhvtl group-writable for config file updates
- Fix systemd unit: move /etc/mhvtl from ReadOnlyPaths to ReadWritePaths
- Fix Library.save() crash when channel/target are None (NAA format guard)
- Add sudoers file for passwordless sudo on MHVTL management commands
  (mktape, vtlcmd, mtx, mt, systemctl, targetcli, etc.)
- Add testing.py settings for in-memory SQLite test runner
- Fix all 48 unit tests: missing imports, wrong model names, missing
  required fields, placeholder code
- Add Sphinx documentation: testing guide, packaging guide, release process
- Add Sphinx documentation: iSCSI setup, troubleshooting section
- Update installation guide with post-install details, sudoers, and fixes

* Thu Mar 05 2026 Ahmed Abdelhaleem Ahmed <ahmedhal@gmail.com> - 1.1.0-1
- All operator pages now use live MHVTL config for library names/vendor/product
  instead of stale database values
- Fix library detail, configure, and status pages to use live slot and device data
- Fix tape creation forms: live library names, correct barcode prefixes, slot usage stats
- Fix barcode suffix detection and bulk tape create numbering
- Fix slot validation order and no-slot-available feedback
- Add tape capacity and actual written data size (Used) columns to tape inventory
- Fix MHVTL status page: broken template structure, missing service method,
  wrong script name (vtllib -> vtllibrary), added vtltape check
- Fix 'In Slots' stats counter on tape inventory (Django template scope bug);
  add 'In Drives' counter
- Remove non-functional Validate/Preview Config buttons from library configure page
- Fix drive list AJAX endpoint to accept library_id from query param
- Fix drive removal page to show drives when library is pre-selected
- Fix cleanup orphaned page to use live MHVTL config instead of empty discovery result

* Fri Jan 30 2026 Ahmed Abdelhaleem Ahmed <ahmedhal@gmail.com> - 1.0.0-1
- Initial RPM release
- Django-based web interface for MHVTL
- Support for library creation, monitoring, and management
