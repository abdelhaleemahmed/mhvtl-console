%define name mhvtl-gui
%define version 2.1.1
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
# Create virtual environment and install dependencies
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
%config(noreplace) /etc/mhvtl-gui/env
%attr(440,root,root) /etc/sudoers.d/mhvtl-gui
%dir /var/www/mhvtl/static
%dir /var/www/mhvtl/media
%dir /var/log/mhvtl
%dir /var/lib/mhvtl-gui
%dir /var/lib/mhvtl-gui/targetcli

%changelog
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
