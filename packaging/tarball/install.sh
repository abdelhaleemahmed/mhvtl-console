#!/bin/bash
#
# MHVTL GUI Installation Script
# For Debian, Ubuntu, and other Linux distributions
#
# Developed by Ahmed Abdelhaleem Ahmed
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
INSTALL_DIR="/opt/mhvtl-gui"
CONFIG_DIR="/etc/mhvtl-gui"
STATIC_DIR="/var/www/mhvtl/static"
MEDIA_DIR="/var/www/mhvtl/media"
LOG_DIR="/var/log/mhvtl"
DATA_DIR="/var/lib/mhvtl-gui"
SERVICE_USER="mhvtl-gui"
SERVICE_GROUP="mhvtl"

# Print banner
print_banner() {
    echo -e "${BLUE}"
    echo "======================================================"
    echo "        MHVTL GUI Installation Script"
    echo "   Linux Virtual Tape Library Management System"
    echo "======================================================"
    echo -e "${NC}"
}

# Print step
print_step() {
    echo -e "${GREEN}[*]${NC} $1"
}

# Print warning
print_warning() {
    echo -e "${YELLOW}[!]${NC} $1"
}

# Print error
print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if running as root
check_root() {
    if [ "$EUID" -ne 0 ]; then
        print_error "Please run as root (sudo ./install.sh)"
        exit 1
    fi
}

# Detect OS
detect_os() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        OS=$NAME
        VER=$VERSION_ID
    elif [ -f /etc/redhat-release ]; then
        OS="Red Hat"
        VER=$(cat /etc/redhat-release | grep -oP '\d+' | head -1)
    else
        OS=$(uname -s)
        VER=$(uname -r)
    fi
    print_step "Detected OS: $OS $VER"
}

# Check dependencies
check_dependencies() {
    print_step "Checking dependencies..."

    local missing=()

    # Check Python. Django 5.2 needs 3.10 or newer, so the distribution's
    # default python3 (3.9 on EL9) will not do; find an interpreter that will.
    PYTHON_BIN=""
    for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
        command -v "$candidate" &> /dev/null || continue
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)' 2>/dev/null; then
            PYTHON_BIN="$candidate"
            break
        fi
    done

    if [ -z "$PYTHON_BIN" ]; then
        missing+=("python3.12 (or any Python >= 3.10)")
    else
        PYTHON_VERSION=$($PYTHON_BIN -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
        print_step "  Python: $PYTHON_BIN ($PYTHON_VERSION)"
    fi

    # Check pip
    if [ -n "$PYTHON_BIN" ] && ! $PYTHON_BIN -m pip --version &> /dev/null; then
        missing+=("pip for $PYTHON_BIN")
    fi

    # Check venv module
    if [ -n "$PYTHON_BIN" ] && ! $PYTHON_BIN -c "import venv" &> /dev/null; then
        missing+=("venv module for $PYTHON_BIN")
    fi

    # Check if MHVTL is installed
    if [ ! -d "/etc/mhvtl" ]; then
        print_warning "MHVTL does not appear to be installed (/etc/mhvtl not found)"
        print_warning "Please install MHVTL first for full functionality"
    fi

    if [ ${#missing[@]} -ne 0 ]; then
        print_error "Missing dependencies: ${missing[*]}"
        echo ""
        echo "Install them with:"
        if [[ "$OS" == *"Ubuntu"* ]] || [[ "$OS" == *"Debian"* ]]; then
            echo "  apt install ${missing[*]}"
        elif [[ "$OS" == *"Red Hat"* ]] || [[ "$OS" == *"CentOS"* ]] || [[ "$OS" == *"Rocky"* ]] || [[ "$OS" == *"Alma"* ]]; then
            echo "  dnf install ${missing[*]}"
        else
            echo "  Use your package manager to install: ${missing[*]}"
        fi
        exit 1
    fi

    print_step "All dependencies satisfied"
}

# Create user and group
create_user() {
    print_step "Creating service user and group..."

    # Create group if it doesn't exist
    if ! getent group $SERVICE_GROUP > /dev/null 2>&1; then
        groupadd -r $SERVICE_GROUP
        print_step "  Created group: $SERVICE_GROUP"
    fi

    # Create user if it doesn't exist
    if ! getent passwd $SERVICE_USER > /dev/null 2>&1; then
        useradd -r -g $SERVICE_GROUP -d $INSTALL_DIR -s /sbin/nologin \
            -c "MHVTL GUI Service Account" $SERVICE_USER
        print_step "  Created user: $SERVICE_USER"
    fi
}

# Create directories
create_directories() {
    print_step "Creating directories..."

    mkdir -p $INSTALL_DIR
    mkdir -p $CONFIG_DIR
    mkdir -p $STATIC_DIR
    mkdir -p $MEDIA_DIR
    mkdir -p $LOG_DIR
    mkdir -p $DATA_DIR

    print_step "  $INSTALL_DIR"
    print_step "  $CONFIG_DIR"
    print_step "  $STATIC_DIR"
    print_step "  $MEDIA_DIR"
    print_step "  $LOG_DIR"
    print_step "  $DATA_DIR"
}

# Install application
install_app() {
    print_step "Installing application files..."

    # Get script directory
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

    # Copy application files
    cp -r "$SCRIPT_DIR/apps" $INSTALL_DIR/
    cp -r "$SCRIPT_DIR/mhvtl_system" $INSTALL_DIR/
    cp -r "$SCRIPT_DIR/mhvtl_cli" $INSTALL_DIR/
    cp -r "$SCRIPT_DIR/templates" $INSTALL_DIR/
    cp -r "$SCRIPT_DIR/static" $INSTALL_DIR/
    cp "$SCRIPT_DIR/manage.py" $INSTALL_DIR/
    cp "$SCRIPT_DIR/pyproject.toml" $INSTALL_DIR/

    # The mhvtl command, on PATH, running the CLI from $INSTALL_DIR
    if [ -f "$SCRIPT_DIR/packaging/bin/mhvtl" ]; then
        install -m 755 "$SCRIPT_DIR/packaging/bin/mhvtl" /usr/bin/mhvtl
        print_step "Installed the mhvtl command: /usr/bin/mhvtl"
    fi

    print_step "Application files copied to $INSTALL_DIR"
}

# Setup Python virtual environment
setup_venv() {
    print_step "Setting up Python virtual environment..."

    ${PYTHON_BIN:-python3} -m venv $INSTALL_DIR/venv
    $INSTALL_DIR/venv/bin/pip install --upgrade pip
    cd $INSTALL_DIR
    $INSTALL_DIR/venv/bin/pip install .

    print_step "Virtual environment created"
}

# Create configuration
create_config() {
    print_step "Creating configuration..."

    # Generate secret key
    SECRET_KEY=$($INSTALL_DIR/venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(50))')

    # Create environment file
    cat > $CONFIG_DIR/env << EOF
# MHVTL GUI Environment Configuration
# Generated on $(date)

# Django Settings
SECRET_KEY=$SECRET_KEY
DEBUG=False
ALLOWED_HOSTS=localhost,127.0.0.1

# Static and Media Files
STATIC_ROOT=$STATIC_DIR/
MEDIA_ROOT=$MEDIA_DIR/

# Logging
LOG_DIR=$LOG_DIR/

# MHVTL Configuration
MHVTL_PRODUCTION_MODE=True
MHVTL_CONFIG_DIR=/etc/mhvtl/
MHVTL_HOME_DIR=/opt/mhvtl/
MHVTL_ENABLE_SERVICE_CONTROL=True

# HTTPS
#
# On: the site redirects to https, and the session and CSRF cookies are sent
# over https only. nginx holds the certificate (nginx.conf.sample).
#
# Serving over plain HTTP instead? Set HTTPS=0 - with it on, the cookies are
# never sent and nobody can log in.
HTTPS=1

# Each part can still be set on its own:
# SECURE_SSL_REDIRECT=True
# CSRF_COOKIE_SECURE=True
# SESSION_COOKIE_SECURE=True
# HTTPS_PORT=8443                    # only when nginx is not on 443
# CSRF_TRUSTED_ORIGINS=https://mhvtl.example.com
EOF

    chmod 640 $CONFIG_DIR/env
    chown root:$SERVICE_GROUP $CONFIG_DIR/env

    print_step "Configuration created at $CONFIG_DIR/env"
}

# Setup database
setup_database() {
    print_step "Setting up database..."

    # Create database file
    touch $DATA_DIR/db.sqlite3

    # Create symlink
    ln -sf $DATA_DIR/db.sqlite3 $INSTALL_DIR/db.sqlite3

    # Run migrations
    cd $INSTALL_DIR
    export DJANGO_SETTINGS_MODULE=mhvtl_system.settings.production
    $INSTALL_DIR/venv/bin/python manage.py migrate --noinput

    # Populate initial brand and model data
    $INSTALL_DIR/venv/bin/python manage.py populate_library_data 2>/dev/null || true

    print_step "Database initialized"
}

# Collect static files
collect_static() {
    print_step "Collecting static files..."

    cd $INSTALL_DIR
    export DJANGO_SETTINGS_MODULE=mhvtl_system.settings.production
    $INSTALL_DIR/venv/bin/python manage.py collectstatic --noinput

    print_step "Static files collected to $STATIC_DIR"
}

# Set permissions
set_permissions() {
    print_step "Setting permissions..."

    chown -R $SERVICE_USER:$SERVICE_GROUP $INSTALL_DIR
    chown -R $SERVICE_USER:$SERVICE_GROUP $STATIC_DIR
    chown -R $SERVICE_USER:$SERVICE_GROUP $MEDIA_DIR
    chown -R $SERVICE_USER:$SERVICE_GROUP $LOG_DIR
    chown -R $SERVICE_USER:$SERVICE_GROUP $DATA_DIR

    # Make manage.py executable
    chmod +x $INSTALL_DIR/manage.py

    # Grant mhvtl group write access to MHVTL config directory
    if [ -d /etc/mhvtl ]; then
        chown -R root:$SERVICE_GROUP /etc/mhvtl
        chmod 775 /etc/mhvtl
        chmod 664 /etc/mhvtl/* 2>/dev/null || true
        print_step "  /etc/mhvtl (group writable for $SERVICE_GROUP)"
    fi

    print_step "Permissions set"
}

# Install sudoers file
install_sudoers() {
    print_step "Installing sudoers file..."

    cat > /etc/sudoers.d/$SERVICE_USER << 'SUDOEOF'
# Passwordless sudo for mhvtl-gui service account
# Required for MHVTL library management operations

mhvtl-gui ALL=(ALL) NOPASSWD: /usr/bin/mktape, \
    /usr/bin/vtlcmd, \
    /usr/sbin/mtx, \
    /usr/bin/mt, \
    /usr/bin/lsscsi, \
    /usr/bin/targetcli, \
    /usr/bin/generate_library_contents, \
    /usr/bin/systemctl start mhvtl*, \
    /usr/bin/systemctl stop mhvtl*, \
    /usr/bin/systemctl restart mhvtl*, \
    /usr/bin/systemctl status mhvtl*, \
    /usr/bin/systemctl start vtl*, \
    /usr/bin/systemctl stop vtl*, \
    /usr/bin/systemctl restart vtl*, \
    /usr/bin/systemctl status vtl*, \
    /usr/bin/systemctl start target*, \
    /usr/bin/systemctl stop target*, \
    /usr/bin/systemctl restart target*, \
    /usr/bin/systemctl status target*, \
    /usr/bin/systemctl daemon-reload, \
    /usr/bin/dmesg, \
    /usr/bin/dd, \
    /usr/bin/stat, \
    /usr/bin/tar, \
    /usr/bin/tee, \
    /usr/bin/touch, \
    /usr/bin/cp, \
    /usr/bin/rm, \
    /usr/bin/chown, \
    /usr/bin/cat, \
    /usr/bin/tail, \
    /usr/bin/test
SUDOEOF

    chmod 440 /etc/sudoers.d/$SERVICE_USER
    print_step "Sudoers file installed at /etc/sudoers.d/$SERVICE_USER"
}

# Install systemd service
install_service() {
    print_step "Installing systemd service..."

    cat > /etc/systemd/system/mhvtl-gui.service << 'EOF'
[Unit]
Description=MHVTL GUI Web Interface
Documentation=https://github.com/abdelhaleemahmed/mhvtl-console
After=network.target mhvtl.target
Wants=mhvtl.target

[Service]
Type=notify
User=mhvtl-gui
Group=mhvtl
WorkingDirectory=/opt/mhvtl-gui
EnvironmentFile=/etc/mhvtl-gui/env
Environment="DJANGO_SETTINGS_MODULE=mhvtl_system.settings.production"

ExecStart=/opt/mhvtl-gui/venv/bin/gunicorn \
    --bind 127.0.0.1:8000 \
    --workers 3 \
    --timeout 120 \
    --access-logfile /var/log/mhvtl/gunicorn-access.log \
    --error-logfile /var/log/mhvtl/gunicorn-error.log \
    --capture-output \
    mhvtl_system.wsgi:application

ExecReload=/bin/kill -s HUP $MAINPID
KillMode=mixed
TimeoutStopSec=30
Restart=on-failure
RestartSec=5

# NoNewPrivileges must be false — the service uses sudo for MHVTL commands
NoNewPrivileges=false
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/www/mhvtl /var/log/mhvtl /var/lib/mhvtl-gui /opt/mhvtl /etc/mhvtl

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable mhvtl-gui

    print_step "Systemd service installed"
}

# Create uninstall script
create_uninstall() {
    print_step "Creating uninstall script..."

    cat > $INSTALL_DIR/uninstall.sh << 'EOF'
#!/bin/bash
# MHVTL GUI Uninstall Script

echo "Stopping service..."
systemctl stop mhvtl-gui 2>/dev/null || true
systemctl disable mhvtl-gui 2>/dev/null || true

echo "Removing files..."
rm -f /etc/systemd/system/mhvtl-gui.service
rm -f /usr/bin/mhvtl
rm -rf /opt/mhvtl-gui
rm -rf /etc/mhvtl-gui
rm -rf /var/www/mhvtl
rm -rf /var/log/mhvtl

# Optionally remove database
read -p "Remove database? (y/N) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    rm -rf /var/lib/mhvtl-gui
fi

# Optionally remove user
read -p "Remove service user? (y/N) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    userdel mhvtl-gui 2>/dev/null || true
fi

systemctl daemon-reload
echo "Uninstall complete"
EOF

    chmod +x $INSTALL_DIR/uninstall.sh
    print_step "Uninstall script created at $INSTALL_DIR/uninstall.sh"
}

# Print completion message
print_complete() {
    echo ""
    echo -e "${GREEN}======================================================"
    echo "        Installation Complete!"
    echo "======================================================${NC}"
    echo ""
    echo "  Configuration: $CONFIG_DIR/env"
    echo "  Application:   $INSTALL_DIR"
    echo "  Logs:          $LOG_DIR"
    echo ""
    echo "  Next steps:"
    echo "  1. Edit $CONFIG_DIR/env if needed"
    echo "  2. Start the service:"
    echo "     ${BLUE}systemctl start mhvtl-gui${NC}"
    echo "  3. Check status:"
    echo "     ${BLUE}systemctl status mhvtl-gui${NC}"
    echo "  4. Put the nginx configuration in place, for HTTPS:"
    echo "     ${BLUE}cp $INSTALL_DIR/nginx.conf.sample /etc/nginx/conf.d/mhvtl-gui.conf${NC}"
    echo "     ${BLUE}systemctl enable --now nginx${NC}"
    echo "  5. Access the GUI:"
    echo "     ${BLUE}https://your-server/${NC}  (http://your-server/ redirects to it)"
    echo ""
    echo "  The certificate is self-signed, so the browser asks once. Replace"
    echo "  /etc/pki/tls/certs/mhvtl-gui.crt and its key with your own, or set"
    echo "  HTTPS=0 in the env file to serve over plain HTTP."
    echo ""
    echo "  To uninstall:"
    echo "     ${BLUE}sudo $INSTALL_DIR/uninstall.sh${NC}"
    echo ""
}

# Create nginx config (HTTPS) and a certificate for it
create_nginx_sample() {
    cat > $INSTALL_DIR/nginx.conf.sample << 'EOF'
# MHVTL GUI nginx configuration
# Copy to /etc/nginx/conf.d/mhvtl-gui.conf (Red Hat) or
# /etc/nginx/sites-available/mhvtl-gui and enable it (Debian), then reload.

upstream mhvtl_gui {
    server 127.0.0.1:8000;
}

# The standard ports. If this host already runs something on 80 or 443,
# nginx will not start beside it: change these two `listen` lines and the
# redirect to a free pair - 8080 and 8443 are the usual choice - and set
# HTTPS_PORT to match in /etc/mhvtl-gui/env, so the CSRF origins agree.
server {
    listen 80;
    server_name _;
    access_log /var/log/nginx/mhvtl-gui-access.log;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name _;
    # HTTP/2: `http2 on;` on nginx 1.25 and later, `listen 443 ssl http2;`
    # before it. Left out so the file works on both.

    ssl_certificate     /etc/pki/tls/certs/mhvtl-gui.crt;
    ssl_certificate_key /etc/pki/tls/private/mhvtl-gui.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    access_log /var/log/nginx/mhvtl-gui-access.log;
    error_log  /var/log/nginx/mhvtl-gui-error.log;

    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    client_max_body_size 100M;

    location /static/ {
        alias /var/www/mhvtl/static/;
        expires 30d;
    }

    location /media/ {
        alias /var/www/mhvtl/media/;
        expires 7d;
    }

    location / {
        proxy_pass http://mhvtl_gui;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 60s;
        proxy_send_timeout 120s;
        proxy_read_timeout 120s;
    }
}
EOF
}

# A certificate for nginx, so the HTTPS the settings expect exists from the
# start. Only when there is none: a real certificate in these paths is kept.
create_certificate() {
    local cert=/etc/pki/tls/certs/mhvtl-gui.crt
    local key=/etc/pki/tls/private/mhvtl-gui.key

    if [ -f "$cert" ] && [ -f "$key" ]; then
        print_step "Certificate: keeping the one already in $cert"
        return
    fi
    if ! command -v openssl >/dev/null 2>&1; then
        print_warning "openssl is not installed; no certificate was made."
        print_warning "Put one in $cert with its key in $key, or set HTTPS=0 in the env file."
        return
    fi

    mkdir -p "$(dirname "$cert")" "$(dirname "$key")"
    local name
    name=$(hostname -f 2>/dev/null || hostname)
    print_step "Creating a self-signed certificate for $name"
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -keyout "$key" -out "$cert" \
        -subj "/CN=$name" \
        -addext "subjectAltName=DNS:$name,DNS:localhost,IP:127.0.0.1" >/dev/null 2>&1
    chmod 600 "$key"
    chmod 644 "$cert"
}

# Main installation
main() {
    print_banner
    check_root
    detect_os
    check_dependencies
    create_user
    create_directories
    install_app
    setup_venv
    create_config
    setup_database
    collect_static
    set_permissions
    install_sudoers
    install_service
    create_nginx_sample
    create_certificate
    create_uninstall
    print_complete
}

# Run main
main "$@"
