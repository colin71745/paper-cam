#!/usr/bin/env bash
#
# Toggle the read-only (overlay) root filesystem.
#
#   ./setup/readonly.sh status
#   ./setup/readonly.sh on      # then reboot
#   ./setup/readonly.sh off     # then reboot
#
# Why: this Pi is powered through the same USB cable that carries video, so
# every unplug is an unclean shutdown. With an overlay root, all writes go
# to RAM and are discarded at reboot, so a power cut cannot corrupt the SD
# card. papercam itself needs no persistent writes - the stream flag lives
# in /run, and logs go to the journal.
#
# The cost: nothing you change survives a reboot. Turn it off (and reboot)
# before git pull, apt, editing config.py, or running calibrate.py, then
# turn it back on. /boot is deliberately left writable so this script can
# still switch the mode back, and so config.txt edits persist.
set -euo pipefail

RASPI_CONFIG="${RASPI_CONFIG:-raspi-config}"
SUDO="${SUDO-sudo}"   # tests set SUDO= to run unprivileged

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\033[33m    %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

# raspi-config's nonint getters answer through their exit status:
# 0 = enabled/read-only, 1 = disabled/writable.
overlay_now()  { $SUDO "$RASPI_CONFIG" nonint get_overlay_now;  }
overlay_conf() { $SUDO "$RASPI_CONFIG" nonint get_overlay_conf; }
bootro_now()   { $SUDO "$RASPI_CONFIG" nonint get_bootro_now;   }

status() {
    say "Read-only root filesystem"
    if overlay_now; then info "right now:   ON  (writes are going to RAM)"
    else                 info "right now:   off (writes reach the SD card)"; fi
    if overlay_conf; then info "after reboot: ON"
    else                  info "after reboot: off"; fi
    if bootro_now; then
        warn "/boot is mounted read-only; 'off' cannot rewrite cmdline.txt."
        warn "Undo with: sudo raspi-config nonint disable_bootro"
    else
        info "/boot:       writable (so this switch keeps working)"
    fi
}

turn_on() {
    if overlay_conf; then
        info "already configured read-only for next boot; nothing to do"
        return
    fi
    say "Enabling read-only root"
    $SUDO "$RASPI_CONFIG" nonint enable_overlayfs
    info "done - takes effect after a reboot"
    warn "From then on, changes do not survive a reboot. That includes"
    warn "calibration.json, apt installs, git pulls and edits to config.py."
    warn "Run '$0 off' and reboot before making changes."
    warn "Journal history is also lost on each reboot; 'journalctl -f' still"
    warn "works live, which is what papercam's diagnostics are for."
    printf '\n    Run: sudo reboot\n\n'
}

turn_off() {
    if ! overlay_conf; then
        info "already writable for next boot; nothing to do"
        return
    fi
    if bootro_now; then
        die "/boot is read-only, so cmdline.txt cannot be rewritten. Run:
       sudo raspi-config nonint disable_bootro && sudo reboot
     then try again."
    fi
    say "Disabling read-only root"
    $SUDO "$RASPI_CONFIG" nonint disable_overlayfs
    info "done - takes effect after a reboot"
    printf '\n    Run: sudo reboot\n\n'
}

command -v "$RASPI_CONFIG" >/dev/null || die "raspi-config not found - is this Raspberry Pi OS?"

case "${1:-status}" in
    status) status ;;
    on)     turn_on ;;
    off)    turn_off ;;
    -h|--help) sed -n '2,22p' "${BASH_SOURCE[0]}" ;;
    *)      die "usage: $0 [status|on|off]" ;;
esac
