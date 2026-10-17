#!/bin/bash
# Load a6xx_uv and hand the two knobs the plugin drives to the desktop user.
#
# Ships inside the plugin and is run by adreno-uv.service, so the module, this
# loader and the UI are one directory. Nothing is copied elsewhere: the unit
# file is the only thing this plugin puts outside its own folder, because Decky
# runs plugin backends as uid 1000 and they cannot insmod.
#
# Paths are derived, not baked in: the module sits next to this script, and the
# GPU is whichever device the adreno driver bound.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
KO="$HERE/a6xx_uv.ko"
PARAMS=/sys/module/a6xx_uv/parameters

USER_NAME="${USER_NAME:-$(stat -c %U "$HERE" 2>/dev/null)}"
# The plugin directory is root-owned, so fall back to whoever owns the homebrew
# tree Decky installs into - that is the desktop user.
if [ "$USER_NAME" = root ] || [ -z "$USER_NAME" ]; then
    USER_NAME="$(stat -c %U /home/*/homebrew 2>/dev/null | head -1)"
fi
USER_NAME="${USER_NAME:-deck}"

[ -f "$KO" ] || { echo "no module at $KO" >&2; exit 1; }
[ -d /sys/module/a6xx_uv ] || insmod "$KO"

# Only the shift itself. The introspection parameters stay root-owned and
# read-only, so the user gets control of the undervolt and nothing else.
chown "$USER_NAME" "$PARAMS/shift"
chmod 0644 "$PARAMS/shift"

# And the GPU's autosuspend delay, which is how the plugin makes a new shift
# take effect immediately: dropping it below the frame interval lets the GPU
# idle between two frames, and the hook reprograms the vote table on the way
# back up. Ordinary runtime PM - no reset, nothing lost.
for gpupm in /sys/bus/platform/drivers/adreno/*/power /sys/bus/platform/devices/*.gpu/power; do
    [ -e "$gpupm/autosuspend_delay_ms" ] || continue
    chown "$USER_NAME" "$gpupm/autosuspend_delay_ms"
    break
done
