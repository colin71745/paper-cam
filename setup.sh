#!/usr/bin/env bash
#
# papercam installer. Run on the Pi, from a clone of this repo:
#
#   git clone https://github.com/colin71745/paper-cam.git papercam
#   cd papercam && ./setup.sh
#
# Safe to re-run: every step checks its own state first. Pass --reboot to
# reboot automatically at the end (the USB gadget and v4l2loopback need it).
#
# Paths can be overridden by environment variable, which is also how the
# unit tests exercise this script without touching a real system.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOOT_CONFIG="${BOOT_CONFIG:-/boot/firmware/config.txt}"
UVC_SRC="${UVC_SRC:-$HOME/uvc-gadget}"
UVC_REPO="${UVC_REPO:-https://gitlab.freedesktop.org/camera/uvc-gadget.git}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
MODPROBE_DIR="${MODPROBE_DIR:-/etc/modprobe.d}"
MODULES_LOAD_DIR="${MODULES_LOAD_DIR:-/etc/modules-load.d}"
SERVICE_USER="${SERVICE_USER:-$(id -un)}"

PACKAGES=(
    python3-opencv python3-picamera2 v4l2loopback-dkms
    git meson ninja-build build-essential pkg-config
    v4l-utils           # v4l2-ctl, for diagnosing the loopback
)
UNITS=(papercam-gadget.service papercam.service papercam-uvc.service)
PATCHES=(uvc-gadget-cpu-copy.patch uvc-gadget-stream-flag.patch)

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\033[33m    warning: %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

preflight() {
    say "Checking prerequisites"
    [ "$(id -u)" -ne 0 ] || die "run as your normal user, not root (the script uses sudo where needed)"
    command -v sudo >/dev/null || die "sudo not found"
    for f in setup/usb-gadget.sh setup/v4l2loopback.conf webcam.py; do
        [ -f "$PROJECT_DIR/$f" ] || die "$f missing - run this from a full checkout"
    done
    for p in "${PATCHES[@]}"; do
        [ -f "$PROJECT_DIR/setup/$p" ] || die "setup/$p missing"
    done
    info "project: $PROJECT_DIR"
    info "service user: $SERVICE_USER"
}

enable_dwc2() {
    say "Enabling USB gadget mode"
    [ -f "$BOOT_CONFIG" ] || die "$BOOT_CONFIG not found - is this Raspberry Pi OS?"
    # A dwc2 line with dr_mode=host is the opposite of what we need, so it
    # does not count as "already enabled".
    if grep -qE '^[[:space:]]*dtoverlay=dwc2([[:space:]]*$|,dr_mode=(otg|peripheral))' "$BOOT_CONFIG"; then
        info "dtoverlay=dwc2 already present"
    else
        # Append an explicit [all] header: appending a bare line could land
        # inside a preceding model-specific section such as [pi4].
        printf '\n[all]\ndtoverlay=dwc2\n' | sudo tee -a "$BOOT_CONFIG" >/dev/null
        info "added dtoverlay=dwc2"
        NEEDS_REBOOT=1
    fi
    if grep -qE '^[[:space:]]*dtoverlay=dwc2,dr_mode=host' "$BOOT_CONFIG"; then
        warn "$BOOT_CONFIG also sets dwc2 dr_mode=host, which forces host mode."
        warn "If the computer never sees the camera, comment that line out."
    fi
}

install_packages() {
    say "Installing packages"
    sudo apt-get update
    sudo apt-get install -y "${PACKAGES[@]}"
}

build_uvc_gadget() {
    say "Building uvc-gadget"
    if [ -d "$UVC_SRC/.git" ]; then
        info "re-using $UVC_SRC (discarding previous patches)"
        git -C "$UVC_SRC" checkout -- .
    else
        git clone "$UVC_REPO" "$UVC_SRC"
    fi
    for p in "${PATCHES[@]}"; do
        info "applying $p"
        git -C "$UVC_SRC" apply "$PROJECT_DIR/setup/$p"
    done
    (
        cd "$UVC_SRC"
        if [ -d build ]; then meson setup --wipe build; else meson setup build; fi
        ninja -C build
        sudo ninja -C build install
    )
    # Without ldconfig the freshly installed libuvcgadget.so is not in the
    # linker cache; uvc-gadget then exits instantly and, because the kernel
    # keeps the UVC function deactivated until it runs, the host sees no USB
    # device at all - which looks exactly like a broken cable.
    sudo ldconfig
}

install_loopback() {
    say "Configuring v4l2loopback"
    sudo cp "$PROJECT_DIR/setup/v4l2loopback.conf" \
            "$MODPROBE_DIR/papercam-v4l2loopback.conf"
    echo v4l2loopback | sudo tee "$MODULES_LOAD_DIR/papercam.conf" >/dev/null
    info "module will load at boot as /dev/video10"
}

install_services() {
    say "Installing systemd units"
    chmod +x "$PROJECT_DIR/setup/usb-gadget.sh"
    for unit in "${UNITS[@]}"; do
        # The shipped units assume /home/pi/papercam and User=pi; rewrite
        # both to wherever this checkout actually lives and whoever is
        # running the install.
        sed -e "s|/home/pi/papercam|$PROJECT_DIR|g" \
            -e "s|^User=pi$|User=$SERVICE_USER|" \
            "$PROJECT_DIR/setup/$unit" | sudo tee "$SYSTEMD_DIR/$unit" >/dev/null
        info "$unit"
    done
    sudo systemctl daemon-reload
    sudo systemctl enable "${UNITS[@]}"
}

summary() {
    say "Done"
    cat <<EOF
    Reboot, then connect the Pi to the computer using its USB data port -
    the middle micro-USB socket labelled "USB", not "PWR IN". One cable
    carries both power and video.

    After reboot:
      systemctl status papercam papercam-uvc
      journalctl -u papercam -f        # paper locks, exposure, frame rate

    The camera only runs while a host app is streaming; between calls it
    idles. Check with: ls /run/papercam-streaming

    Focus and aperture:  sudo systemctl stop papercam && python3 focus.py
EOF
    if [ "${NEEDS_REBOOT:-0}" = "1" ]; then
        warn "config.txt changed - a reboot is REQUIRED before this will work."
    fi
}

main() {
    local do_reboot=0
    for arg in "$@"; do
        case "$arg" in
            --reboot) do_reboot=1 ;;
            -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}"; exit 0 ;;
            *) die "unknown option: $arg" ;;
        esac
    done

    preflight
    enable_dwc2
    install_packages
    build_uvc_gadget
    install_loopback
    install_services
    summary

    if [ "$do_reboot" = "1" ]; then
        say "Rebooting"
        sudo reboot
    else
        printf '\n    Run: sudo reboot\n\n'
    fi
}

# Allow the tests to source this file for its functions without running it.
if [ "${PAPERCAM_SETUP_LIB:-0}" != "1" ]; then
    main "$@"
fi
