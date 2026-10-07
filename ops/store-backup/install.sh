#!/bin/sh
# Install the daily store-backup and monthly restore-drill units as systemd USER units. Needs no root.
# The interpreter is resolved here and written into ExecStart as an absolute
# path, because the user manager's PATH has no venv and often no `python`.
# Safe to rerun: a file is written only when its content differs, and
# daemon-reload runs only after a write.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
unit_dir=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
service=coxswain-store-backup.service
timer=coxswain-store-backup.timer
drill_service=coxswain-store-drill.service
drill_timer=coxswain-store-drill.timer

die() {
    printf 'install.sh: %s\n' "$1" >&2
    exit 1
}

# The checkout venv wins; python3 on the installer's PATH is the fallback.
if [ -x "$root/.venv/bin/python" ]; then
    python=$root/.venv/bin/python
else
    python=$(command -v python3) || die "no .venv/bin/python under $root and no python3 on PATH"
fi

# An interpreter that cannot import the package would fail every daily run.
(cd "$root" && "$python" -c 'import agent_tools') || die "$python cannot import agent_tools from $root"

# systemd splits on whitespace and expands % \ and $; refuse such values, as agent_tools/chair_service.py does.
for value in "$root" "$python"; do
    case $value in
        *[!A-Za-z0-9._@+:,/=-]*) die "path is unsafe in a unit file: $value" ;;
        *) ;;
    esac
done

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

changed=0

# Copy $1 to $2 only when $2 is missing or differs.
install_if_changed() {
    if [ -f "$2" ] && cmp -s "$1" "$2"; then
        return 0
    fi
    mkdir -p "$(dirname "$2")"
    cp "$1" "$2"
    chmod 644 "$2"
    changed=1
}

# Both values passed the safe-set check above, so neither holds a sed metacharacter.
sed -e "s|@REPO_ROOT@|$root|" -e "s|@PYTHON@|$python|" "$here/$service" >"$work/$service"
sed -e "s|@REPO_ROOT@|$root|" -e "s|@PYTHON@|$python|" "$here/$drill_service" >"$work/$drill_service"

install_if_changed "$work/$service" "$unit_dir/$service"
install_if_changed "$here/$timer" "$unit_dir/$timer"
install_if_changed "$work/$drill_service" "$unit_dir/$drill_service"
install_if_changed "$here/$drill_timer" "$unit_dir/$drill_timer"

if [ "$changed" -eq 1 ]; then
    systemctl --user daemon-reload
fi
# --now starts the timer today, not at the next login; both are no-ops when already done.
systemctl --user enable --now "$timer"
systemctl --user enable --now "$drill_timer"

# A user timer stops at logout unless the user lingers.
linger=$(loginctl show-user "$(id -un)" --property=Linger --value 2>/dev/null || true)
if [ "$linger" != "yes" ]; then
    printf 'install.sh: linger is off, so the timers stop at logout. Run: loginctl enable-linger %s\n' "$(id -un)" >&2
fi
