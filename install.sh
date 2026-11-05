#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DKMS_NAME="blackctl"
DKMS_VERSION="0.2"
DKMS_SOURCE="/usr/src/${DKMS_NAME}-${DKMS_VERSION}"
ACTION="install"
TARGET_USER="${SUDO_USER:-}"

for arg in "$@"; do
    case "$arg" in
        --uninstall) ACTION="uninstall" ;;
        --user=*) TARGET_USER="${arg#--user=}" ;;
        -h|--help)
            echo "Usage: sudo $0 [--user=NAME] [--uninstall]"
            exit 0
            ;;
        *)
            echo "Unknown option: $arg" >&2
            exit 2
            ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "Run as root: sudo $ROOT/install.sh" >&2
    exit 1
fi

if [[ -z "$TARGET_USER" || "$TARGET_USER" == root ]] || ! id "$TARGET_USER" >/dev/null 2>&1; then
    echo "Could not determine the desktop user. Use --user=YOUR_USERNAME." >&2
    exit 1
fi

TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
TARGET_GROUP="$(id -gn "$TARGET_USER")"
USER_LOCAL="$TARGET_HOME/.local"
WIREPLUMBER_RULE="$TARGET_HOME/.config/wireplumber/wireplumber.conf.d/70-blackctl-blackshark-game-chat.conf"

for command in dkms install modprobe udevadm python3 gcc runuser; do
    command -v "$command" >/dev/null || {
        echo "Missing required command: $command" >&2
        exit 1
    }
done

as_user() {
    runuser -u "$TARGET_USER" -- env XDG_RUNTIME_DIR="/run/user/$(id -u "$TARGET_USER")" "$@"
}

# Remove every installed blackctl DKMS version except the one named in $1.
remove_dkms_packages() {
    local version
    for version in $(dkms status "$DKMS_NAME" 2>/dev/null | sed -E 's#^[^/]+/([^,:]+).*#\1#' | sort -u); do
        [[ "$version" == "${1:-}" ]] && continue
        dkms remove -m "$DKMS_NAME" -v "$version" --all
        rm -rf "/usr/src/${DKMS_NAME}-${version}"
    done
}

if [[ "$ACTION" == uninstall ]]; then
    as_user systemctl --user disable --now blackctl-fx.service blackctl-tray.service 2>/dev/null || true
    rmmod blackctl razer_cfg255 2>/dev/null || true
    remove_dkms_packages
    rm -f /etc/udev/rules.d/99-blackctl.rules /usr/lib/udev/blackctl-mount
    rm -f /etc/modules-load.d/blackctl.conf
    rm -rf /usr/lib/blackctl /usr/share/blackctl
    rm -f /usr/lib/systemd/user/blackctl-fx.service /usr/lib/systemd/user/blackctl-tray.service
    rm -f "$WIREPLUMBER_RULE"
    rm -f "$USER_LOCAL/bin/blackctl"
    rm -f "$USER_LOCAL/share/applications/blackctl.desktop"
    rm -f "$USER_LOCAL/share/icons/hicolor/scalable/apps/blackctl.svg"
    udevadm control --reload-rules
    echo "Removed blackctl. Settings were kept in $TARGET_HOME/.config/blackctl.json"
    exit 0
fi

python3 -c 'import gi; gi.require_version("Gtk", "4.0"); from gi.repository import Gtk' 2>/dev/null || {
    echo "GTK4/PyGObject is missing; install your distro's gtk4 and python-gobject packages." >&2
    exit 1
}
[[ -f /usr/include/ladspa.h ]] || {
    echo "ladspa.h is missing; install your distro's ladspa package (ladspa-sdk on Debian/Ubuntu)." >&2
    exit 1
}

if ! getent group plugdev >/dev/null 2>&1; then
    groupadd --system plugdev
fi
if ! id -nG "$TARGET_USER" | tr ' ' '\n' | grep -qx plugdev; then
    usermod -aG plugdev "$TARGET_USER"
    ADDED_TO_GROUP=1
else
    ADDED_TO_GROUP=0
fi

# Kernel modules: razer_cfg255 (enumeration fix) and blackctl (control driver).
# Build the new version before removing older ones, so a failed build changes nothing.
if dkms status -m "$DKMS_NAME" -v "$DKMS_VERSION" 2>/dev/null | grep -q .; then
    dkms remove -m "$DKMS_NAME" -v "$DKMS_VERSION" --all
fi
rm -rf "$DKMS_SOURCE"
install -d "$DKMS_SOURCE/kernel"
install -m 0644 "$ROOT/Makefile" "$ROOT/dkms.conf" "$DKMS_SOURCE/"
install -m 0644 "$ROOT"/kernel/*.c "$DKMS_SOURCE/kernel/"
dkms add -m "$DKMS_NAME" -v "$DKMS_VERSION"
dkms build -m "$DKMS_NAME" -v "$DKMS_VERSION"
remove_dkms_packages "$DKMS_VERSION"
dkms install -m "$DKMS_NAME" -v "$DKMS_VERSION"

install -m 0644 "$ROOT/data/99-blackctl.rules" /etc/udev/rules.d/99-blackctl.rules
install -m 0755 "$ROOT/data/blackctl-mount" /usr/lib/udev/blackctl-mount
printf '%s\n' razer_cfg255 blackctl > /etc/modules-load.d/blackctl.conf

# Audio effects: dynamics plugin, HRIR for virtual surround, and the user service.
install -d /usr/lib/blackctl /usr/share/blackctl
gcc -O2 -shared -fPIC -o /usr/lib/blackctl/razer_dyn.so "$ROOT/dsp/razer_dyn.c" -lm
install -m 0644 "$ROOT/dsp/kemar.wav" /usr/share/blackctl/kemar.wav
install -m 0644 "$ROOT/data/blackctl-fx.service" "$ROOT/data/blackctl-tray.service" /usr/lib/systemd/user/

# GUI runs straight from this source tree.
install -d -o "$TARGET_USER" -g "$TARGET_GROUP" "$USER_LOCAL/bin" \
    "$USER_LOCAL/share/applications" "$USER_LOCAL/share/icons/hicolor/scalable/apps" \
    "$(dirname "$WIREPLUMBER_RULE")"
ln -sfn "$ROOT/bin/blackctl" "$USER_LOCAL/bin/blackctl"
chown -h "$TARGET_USER:$TARGET_GROUP" "$USER_LOCAL/bin/blackctl"
install -o "$TARGET_USER" -g "$TARGET_GROUP" -m 0644 "$ROOT/data/blackctl.desktop" "$USER_LOCAL/share/applications/blackctl.desktop"
install -o "$TARGET_USER" -g "$TARGET_GROUP" -m 0644 "$ROOT/data/blackctl.svg" "$USER_LOCAL/share/icons/hicolor/scalable/apps/blackctl.svg"
install -o "$TARGET_USER" -g "$TARGET_GROUP" -m 0644 "$ROOT/data/70-blackctl-blackshark-game-chat.conf" "$WIREPLUMBER_RULE"
as_user systemctl --user daemon-reload 2>/dev/null || true
# Tray icon with the battery, started with the graphical session.
as_user systemctl --user enable --now blackctl-tray.service 2>/dev/null || true

udevadm control --reload-rules
modprobe -r blackctl 2>/dev/null || true
modprobe razer_cfg255
modprobe blackctl
udevadm trigger --action=add --subsystem-match=hid

echo "Installed blackctl from $ROOT"
echo "Unplug the dongle for a few seconds and plug it back in, then launch: blackctl"
if [[ "$ADDED_TO_GROUP" == 1 ]]; then
    echo "Log out and back in once so the new plugdev membership takes effect."
fi
