#!/bin/bash
#
# MHVTL GUI Package Build Script
# Creates both RPM and Tarball packages
#

set -e

# Configuration
NAME="mhvtl-gui"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# One version for the packages, the footer and the docs: mhvtl_system/__init__.py
VERSION="$(sed -n "s/^__version__ = '\(.*\)'$/\1/p" "$PROJECT_DIR/mhvtl_system/__init__.py")"
[ -n "$VERSION" ] || { echo "could not read __version__ from mhvtl_system/__init__.py" >&2; exit 1; }
BUILD_DIR="$SCRIPT_DIR/build"
DIST_DIR="$SCRIPT_DIR/dist"

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

print_step() {
    echo -e "${GREEN}[*]${NC} $1"
}

print_header() {
    echo -e "${BLUE}"
    echo "======================================================"
    echo "  MHVTL GUI Package Builder"
    echo "  Version: $VERSION"
    echo "======================================================"
    echo -e "${NC}"
}

# Clean previous builds
clean() {
    print_step "Cleaning previous builds..."
    rm -rf "$BUILD_DIR"
    rm -rf "$DIST_DIR"
    mkdir -p "$BUILD_DIR"
    mkdir -p "$DIST_DIR"
}

# Build tarball
build_tarball() {
    print_step "Building tarball package..."

    local TARBALL_DIR="$BUILD_DIR/$NAME-$VERSION"
    mkdir -p "$TARBALL_DIR"

    # Copy application files
    cp -r "$PROJECT_DIR/apps" "$TARBALL_DIR/"
    cp -r "$PROJECT_DIR/mhvtl_system" "$TARBALL_DIR/"
    cp -r "$PROJECT_DIR/mhvtl_cli" "$TARBALL_DIR/"        # the mhvtl command
    mkdir -p "$TARBALL_DIR/packaging/bin"
    cp "$SCRIPT_DIR/bin/mhvtl" "$TARBALL_DIR/packaging/bin/"
    cp -r "$PROJECT_DIR/templates" "$TARBALL_DIR/"
    cp -r "$PROJECT_DIR/static" "$TARBALL_DIR/"
    cp "$PROJECT_DIR/manage.py" "$TARBALL_DIR/"
    cp "$PROJECT_DIR/pyproject.toml" "$TARBALL_DIR/"

    # Copy install script
    cp "$SCRIPT_DIR/tarball/install.sh" "$TARBALL_DIR/"
    chmod +x "$TARBALL_DIR/install.sh"

    # Remove __pycache__ and .pyc files
    find "$TARBALL_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    find "$TARBALL_DIR" -type f -name "*.pyc" -delete 2>/dev/null || true
    find "$TARBALL_DIR" -type f -name "*.pyo" -delete 2>/dev/null || true

    # Remove development files
    rm -rf "$TARBALL_DIR"/apps/*/migrations/__pycache__ 2>/dev/null || true
    rm -f "$TARBALL_DIR/db.sqlite3" 2>/dev/null || true

    # Create tarball
    cd "$BUILD_DIR"
    tar -czvf "$DIST_DIR/$NAME-$VERSION.tar.gz" "$NAME-$VERSION"

    print_step "Tarball created: $DIST_DIR/$NAME-$VERSION.tar.gz"
}

# Build RPM source tarball
build_rpm_source() {
    print_step "Building RPM source package..."

    local RPM_DIR="$BUILD_DIR/rpmbuild"
    mkdir -p "$RPM_DIR"/{BUILD,RPMS,SOURCES,SPECS,SRPMS}

    # Copy source tarball
    cp "$DIST_DIR/$NAME-$VERSION.tar.gz" "$RPM_DIR/SOURCES/"

    # Copy the spec, stamping in the one version from
    # mhvtl_system/__init__.py so there is nothing to forget to edit.
    sed "s/^%define version .*/%define version $VERSION/" \
        "$SCRIPT_DIR/rpm/mhvtl-gui.spec" > "$RPM_DIR/SPECS/mhvtl-gui.spec"

    # Copy additional files for RPM
    mkdir -p "$BUILD_DIR/$NAME-$VERSION/packaging/rpm"
    cp "$SCRIPT_DIR/rpm/mhvtl-gui.service" "$BUILD_DIR/$NAME-$VERSION/packaging/rpm/"
    cp "$SCRIPT_DIR/rpm/mhvtl-gui-nginx.conf" "$BUILD_DIR/$NAME-$VERSION/packaging/rpm/"
    cp "$SCRIPT_DIR/rpm/env.template" "$BUILD_DIR/$NAME-$VERSION/packaging/rpm/"
    cp "$SCRIPT_DIR/rpm/mhvtl-gui.sudoers" "$BUILD_DIR/$NAME-$VERSION/packaging/rpm/"

    # Recreate tarball with RPM files
    cd "$BUILD_DIR"
    tar -czvf "$RPM_DIR/SOURCES/$NAME-$VERSION.tar.gz" "$NAME-$VERSION"

    print_step "RPM source prepared in $RPM_DIR"
    echo ""
    echo "To build RPM, run:"
    echo "  rpmbuild -ba $RPM_DIR/SPECS/mhvtl-gui.spec --define \"_topdir $RPM_DIR\""
}

# Build RPM (if rpmbuild available)
build_rpm() {
    if command -v rpmbuild &> /dev/null; then
        print_step "Building RPM package..."

        local RPM_DIR="$BUILD_DIR/rpmbuild"
        rpmbuild -ba "$RPM_DIR/SPECS/mhvtl-gui.spec" --define "_topdir $RPM_DIR"

        # Copy RPM to dist
        cp "$RPM_DIR"/RPMS/noarch/*.rpm "$DIST_DIR/" 2>/dev/null || true
        cp "$RPM_DIR"/SRPMS/*.rpm "$DIST_DIR/" 2>/dev/null || true

        print_step "RPM packages created in $DIST_DIR"
    else
        print_step "rpmbuild not found - skipping RPM build"
        print_step "RPM source is prepared for building on a system with rpmbuild"
    fi
}

# Create checksums
create_checksums() {
    print_step "Creating checksums..."
    cd "$DIST_DIR"
    sha256sum * > SHA256SUMS
    print_step "Checksums created: $DIST_DIR/SHA256SUMS"
}

# Show summary
show_summary() {
    echo ""
    echo -e "${BLUE}======================================================"
    echo "  Build Complete!"
    echo "======================================================${NC}"
    echo ""
    echo "  Output directory: $DIST_DIR"
    echo ""
    echo "  Packages:"
    ls -lh "$DIST_DIR"
    echo ""
    echo "  Tarball Installation:"
    echo "    tar -xzf $NAME-$VERSION.tar.gz"
    echo "    cd $NAME-$VERSION"
    echo "    sudo ./install.sh"
    echo ""
    if ls "$DIST_DIR"/*.rpm 1>/dev/null 2>&1; then
        echo "  RPM Installation:"
        echo "    sudo dnf install $NAME-$VERSION*.rpm"
        echo ""
    fi
}

# Main
main() {
    print_header
    clean
    build_tarball
    build_rpm_source
    build_rpm
    create_checksums
    show_summary
}

main "$@"
