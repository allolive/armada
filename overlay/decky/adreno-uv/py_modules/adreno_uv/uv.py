"""Talks to the a6xx_uv kernel module and owns this plugin's config.

Everything here stays inside the plugin's own directories and the module's own
sysfs node. Nothing is read from or written to Armada's namespace, so the two
can be installed together without either one standing on the other.
"""

import json
import os
import subprocess
import time
from pathlib import Path

MODULE = "a6xx_uv"
PARAMS = Path(f"/sys/module/{MODULE}/parameters")
SHIFT = PARAMS / "shift"
STOCK_VOTES = PARAMS / "stock_votes"
APPLIED_VOTES = PARAMS / "applied_votes"
GPU_FREQS = PARAMS / "gpu_freqs"
HITS = PARAMS / "hits"
STATUS = PARAMS / "status"
def _find_gpu_pm():
    """The runtime-PM directory of whichever device the adreno driver bound.

    Nothing here is specific to one SoC: the module hooks the msm driver's GMU
    path, which is shared by every Adreno using a6xx-style DVFS. Hardcoding
    3d00000.gpu would be the only thing tying this to one machine, and that
    address is a convention rather than a guarantee.
    """
    bound = Path("/sys/bus/platform/drivers/adreno")
    if bound.is_dir():
        for dev in sorted(bound.iterdir()):
            pm = dev / "power"
            if (pm / "autosuspend_delay_ms").exists():
                return pm
    for dev in sorted(Path("/sys/bus/platform/devices").glob("*.gpu")):
        pm = dev / "power"
        if (pm / "autosuspend_delay_ms").exists():
            return pm
    # Keep a usable path so callers can still report a sensible error.
    return Path("/sys/bus/platform/devices/3d00000.gpu/power")


GPU_PM = _find_gpu_pm()
GPU_RUNTIME_STATUS = GPU_PM / "runtime_status"
GPU_AUTOSUSPEND = GPU_PM / "autosuspend_delay_ms"

# Matches UV_MAX_SHIFT in a6xx_uv.c. The module rejects anything larger, so
# clamping here only decides what the UI can offer, never what is safe.
MAX_SHIFT = 8
PROFILES = 3
# Numbered, not named: the names implied a severity ordering that the profiles
# do not actually have once they are edited.
PROFILE_NAMES = ["Profile 1", "Profile 2", "Profile 3"]
# active == NO_PROFILE means stock. It is the default so that enabling the
# master switch alone never changes a voltage.
NO_PROFILE = -1

PLUGIN_DIR = Path(os.environ.get("DECKY_PLUGIN_DIR", Path(__file__).resolve().parents[2]))
SETTINGS_DIR = Path(os.environ.get("DECKY_PLUGIN_SETTINGS_DIR", "/tmp"))
CONFIG = SETTINGS_DIR / "profiles.json"
BUNDLED_KO = PLUGIN_DIR / "bin" / f"{MODULE}.ko"
LOADER = PLUGIN_DIR / "bin" / "load.sh"
AUTOTUNE = PLUGIN_DIR / "bin" / "autotune.py"
STRESS = PLUGIN_DIR / "bin" / "gpustress"
TUNE_STATE = SETTINGS_DIR / "autotune.json"
TUNE_STOP = SETTINGS_DIR / "autotune.stop"
TUNE_UNIT = "adreno-uv-autotune"
# Liveness is read from the unit's cgroup, not from `systemctl is-active`.
# Spawning systemctl on a polling path was a mistake: each call blocks on
# systemd rather than burning CPU, so with the panel and the editor both
# polling there was permanently one in flight, and plugin RPCs queued behind
# it until the quick access menu appeared to hang for a minute.
TUNE_CGROUP = Path(f"/sys/fs/cgroup/system.slice/{TUNE_UNIT}.service/cgroup.procs")
UNIT_NAME = "adreno-uv.service"
UNIT = Path("/etc/systemd/system") / UNIT_NAME

UNIT_TEXT = f"""[Unit]
Description=Adreno GPU undervolt module
# The kprobe target lives in the msm driver, so the module cannot load before
# the GPU driver is up.
After=multi-user.target
# The plugin now removes this unit on unload, but keep the condition: it makes
# a stale unit (left by an interrupted removal, or a plugin directory moved out
# from under it) skip quietly instead of failing every boot.
ConditionPathExists={BUNDLED_KO}

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart={LOADER}
# A module that will not load is a rebuild after a kernel update, not a
# transient failure - do not retry into a boot loop.
Restart=no

[Install]
WantedBy=multi-user.target
"""

# Repairing means talking to systemd, and the panel polls get_state every 10s.
# Spawning systemctl on a polling path is the mistake that made the quick
# access menu appear to hang - each call blocks on systemd, so with the panel
# and the editor both polling there was permanently one in flight and plugin
# RPCs queued behind it. So a repair attempt is rate-limited, and its outcome is
# remembered so the UI can explain itself without asking again.
REPAIR_INTERVAL = 60.0
_repair = {"at": 0.0, "attempts": 0, "error": ""}

# Armada's own undervolt writes here. It targets a kernel patch that does not
# boot, so it is inert today, but if it ever comes alive both would be driving
# the same vote table and the last writer would win. Surfaced to the UI rather
# than worked around: two owners of one knob is a thing to fix, not to paper over.
ARMADA_UV = Path("/sys/module/msm/parameters/gpu_volt_deltas")


def _read(path):
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _read_list(path, cast=int):
    text = _read(path)
    if not text:
        return []
    out = []
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            out.append(cast(item, 0) if cast is int and item.startswith("0x") else cast(item))
        except (ValueError, TypeError):
            return []
    return out


def module_loaded():
    return PARAMS.is_dir()


def shift_writable():
    return SHIFT.exists() and os.access(SHIFT, os.W_OK)


def _clean_env():
    """Environment for child processes, without Decky's bundled libraries.

    The plugin host is a PyInstaller binary: it exports LD_LIBRARY_PATH pointing
    at its unpacked bundle, so a subprocess like systemctl or insmod loads that
    libcrypto instead of the system one and dies on a version mismatch.
    PyInstaller stashes the original in LD_LIBRARY_PATH_ORIG when there was one.
    """
    env = dict(os.environ)
    original = env.pop("LD_LIBRARY_PATH_ORIG", None)
    if original is not None:
        env["LD_LIBRARY_PATH"] = original
    else:
        env.pop("LD_LIBRARY_PATH", None)
    return env


def _systemctl(*args):
    try:
        proc = subprocess.run(("systemctl",) + args, capture_output=True,
                              text=True, timeout=30, env=_clean_env())
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    return proc.returncode == 0, (proc.stderr or proc.stdout or "").strip()


def ensure_service():
    """Install and start the unit that loads the module.

    Decky runs this backend as root but without CAP_SYS_MODULE, so it can write
    /etc and drive systemd yet cannot insmod. The unit exists purely to do that
    one thing; everything else lives in the plugin directory.

    Rewritten whenever it differs from what we want, so moving or reinstalling
    the plugin fixes the paths instead of leaving a unit pointing at nothing.
    """
    if not BUNDLED_KO.is_file() or not LOADER.is_file():
        return False, "plugin is missing bin/a6xx_uv.ko or bin/load.sh"
    try:
        LOADER.chmod(0o755)
        if not UNIT.exists() or UNIT.read_text(encoding="utf-8") != UNIT_TEXT:
            UNIT.write_text(UNIT_TEXT, encoding="utf-8")
            _systemctl("daemon-reload")
    except OSError as exc:
        return False, f"could not install {UNIT_NAME}: {exc}"
    ok, err = _systemctl("enable", "--now", UNIT_NAME)
    if not ok:
        return False, f"{UNIT_NAME} failed to start: {err}"
    return True, ""


def remove_service():
    """Take the unit back out.

    Called on _unload (plugin disabled or Decky restarting) and on _uninstall,
    so the unit's lifetime matches the plugin's. Stopping it does not unload the
    module - the unit is Type=oneshot with no ExecStop - so a live undervolt and
    a running tuner are unaffected; what changes is that the module no longer
    loads at boot while the plugin is disabled.
    """
    _systemctl("disable", "--now", UNIT_NAME)
    try:
        UNIT.unlink()
    except OSError:
        pass
    _systemctl("daemon-reload")


# Prefixes systemd puts in a unit's journal on its own behalf.
_SYSTEMD_NOISE = (f"{UNIT_NAME}:", "Failed to start", "Started", "Starting",
                  "Stopping", "Stopped", "Deactivated", "Reached target")


def _service_error_text():
    """The loader unit's own last words.

    insmod's reason - a vermagic mismatch after a kernel update, most often -
    goes to the unit's journal and nowhere else. Without it the UI can only say
    the service failed, which is the least useful half of the story. Read only
    on the failure path, which is already rate-limited.
    """
    try:
        proc = subprocess.run(
            ["journalctl", "-u", UNIT_NAME, "-n", "10", "--no-pager", "-o", "cat"],
            capture_output=True, text=True, timeout=10, env=_clean_env())
    except (OSError, subprocess.SubprocessError):
        return ""
    lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    # systemd logs its own epitaph after the failure, so the LAST line is always
    # "Failed to start adreno-uv.service" - true and useless. The line before it
    # is the one that matters:
    #   insmod: ERROR: could not insert module ...: Invalid module format
    # which is what a kernel update looks like from here. Walk back to the first
    # line systemd did not write itself.
    for line in reversed(lines):
        if line.startswith(_SYSTEMD_NOISE):
            continue
        return line
    return lines[-1] if lines else ""


def _repaired():
    """Clear the repair record after the module comes up.

    Resetting the attempt count matters as much as the error string: the
    cooldown is meant to space out CONSECUTIVE failures, not to punish the next
    genuine one. Leaving it armed after a success meant a module that vanished
    again a minute later waited out the interval before anything was tried.
    """
    _repair["attempts"] = 0
    _repair["error"] = ""
    return True, ""


def repair_service(force=False):
    """Put the loader unit back when the module is not loaded.

    Everything that loads the module goes through systemd, because this backend
    runs as root but with CAP_SYS_MODULE dropped. So "the module is missing" and
    "the unit is missing, stopped or failed" are the same condition with the
    same repair, and it is worth attempting rather than sitting on the
    not-loaded screen until someone reboots.

    Reasons the unit can be gone while the plugin is up: _unload removes it (so
    a disabled plugin does not load a module at boot) and a crash or a Decky
    restart can leave that half-done; an OS update replaces the kernel and the
    module then refuses to load at boot; or someone rmmod'd it by hand.

    Not a retry loop. Attempts are spaced by REPAIR_INTERVAL and a module that
    will not load is a rebuild, not a transient failure - the point of retrying
    at all is that a rebuilt .ko dropped into bin/ should be picked up without
    a reboot.
    """
    if module_loaded():
        return _repaired()
    now = time.monotonic()
    if not force and _repair["attempts"] and now - _repair["at"] < REPAIR_INTERVAL:
        return False, _repair["error"] or "waiting before retrying adreno-uv.service"
    _repair["at"] = now
    _repair["attempts"] += 1

    ok, err = ensure_service()
    if module_loaded():
        return _repaired()

    # `enable --now` does nothing when the unit is already active, and it stays
    # active: it is Type=oneshot with RemainAfterExit, so systemd still calls it
    # started long after an rmmod. Clear any corpse and run ExecStart again.
    _systemctl("reset-failed", UNIT_NAME)
    ok, err2 = _systemctl("restart", UNIT_NAME)
    if module_loaded():
        return _repaired()

    # systemctl's own "Job for adreno-uv.service failed ... see journalctl" says
    # nothing anyone can act on, and it is two lines of it. The loader's journal
    # names the actual reason - after a kernel update, the vermagic mismatch -
    # so prefer that and keep systemctl as the fallback. One line either way,
    # because this ends up in a panel row.
    detail = _service_error_text() or (err2 or err or "").strip()
    detail = detail.splitlines()[0].strip() if detail else ""
    _repair["error"] = detail or f"{UNIT_NAME} ran but {MODULE} is still not loaded"
    return False, _repair["error"]


def module_status():
    """The module's own verdict on the kernel it landed in.

    "waiting"      hooked, but the GMU has not booted since it loaded
    "ok"           it validated the GMU's vote table and is live
    "incompatible" the table did not validate - the module is inert

    Empty when the module is not loaded, or when it predates the parameter;
    an older .ko is treated as compatible rather than as broken.
    """
    return _read(STATUS).strip() if module_loaded() else ""


def kernel_incompatible():
    """The module loaded but refuses to touch this kernel's GMU.

    vermagic cannot distinguish our kernel build from Armada's stock one, so
    insmod succeeds after an OS update and the module looks healthy. It is not:
    it validated the GMU table, found something that is not this build's
    layout, and went inert. Saying "loaded" here would be a lie the user acts
    on - they would set a profile and see nothing happen.
    """
    return module_status() == "incompatible"


def module_error():
    """Why the undervolt is unavailable, as far as we know. Empty when fine."""
    if not module_loaded():
        return _repair["error"]
    if kernel_incompatible():
        return ("incompatible kernel: the module was built for a different "
                "kernel than the one running - rebuild it and reboot")
    return ""


def ensure_loaded():
    """Get the module up, through systemd first and insmod only as a fallback.

    Decky runs this backend as root but with CAP_SYS_MODULE dropped, so insmod
    from here fails with EPERM even as uid 0 - which is the whole reason the
    unit exists. Trying the service first means the ordinary case works and the
    error the user sees is the loader's, not a misleading EPERM.

    The .ko is built against one exact kernel, so a kernel update makes loading
    fail. That is reported rather than retried into a boot loop.
    """
    if module_loaded():
        return True, ""
    if not BUNDLED_KO.is_file():
        return False, f"no bundled module at {BUNDLED_KO}"

    ok, service_err = repair_service(force=True)
    if ok:
        return True, ""

    # Only reachable with CAP_SYS_MODULE - the backend run by hand, or a future
    # Decky that keeps the capability. Under Decky today this is EPERM and the
    # service error above is the one worth reporting.
    try:
        proc = subprocess.run(
            ["insmod", str(BUNDLED_KO)],
            capture_output=True, text=True, timeout=30, env=_clean_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, service_err or str(exc)
    if proc.returncode != 0 or not module_loaded():
        err = (proc.stderr or proc.stdout or "").strip()
        return False, service_err or err or f"insmod exited {proc.returncode}"
    return _repaired()


def gpu_freqs():
    """Operating points, one per shift index. Empty until the GPU has booted
    once, because the module snapshots them from the GMU on its first hook."""
    return _read_list(GPU_FREQS)


def clean_curve(curve, count):
    """Bad entries become 0 (stock) rather than being dropped.

    Dropping one would slide every later entry onto an operating point it was
    not tuned for, which is how a conservative curve turns into an aggressive
    one silently.
    """
    if not isinstance(curve, list):
        curve = []
    out = []
    for i in range(count):
        value = curve[i] if i < len(curve) else 0
        if isinstance(value, bool) or not isinstance(value, int):
            value = 0
        # Never store a shift deeper than the one that still changes the corner:
        # the module floors the source at the lowest real OPP, so anything
        # beyond i-1 is indistinguishable from i-1 and only makes the editor
        # feel unresponsive on the way back up.
        out.append(max(0, min(MAX_SHIFT, i - 1, value)))
    if out:
        # Index 0 is the rail-off entry. Shifting it is meaningless and the
        # module would floor it anyway; keep the stored curve honest.
        out[0] = 0
    return out


def default_profiles(count):
    # Seeded as uniform one/two/three-step borrows so the profiles do something
    # sensible before anyone edits them.
    return [clean_curve([0] + [n + 1] * max(0, count - 1), count) for n in range(PROFILES)]


def load_config():
    count = len(gpu_freqs())
    try:
        with CONFIG.open(encoding="utf-8") as f:
            loaded = json.load(f)
    except (OSError, ValueError):
        loaded = None
    loaded = loaded if isinstance(loaded, dict) else {}

    stored = loaded.get("profiles")
    stored = stored if isinstance(stored, list) else []
    if count and not stored:
        profiles = default_profiles(count)
    else:
        profiles = [
            clean_curve(stored[i] if i < len(stored) else [], count)
            for i in range(PROFILES)
        ]

    active = loaded.get("active")
    if isinstance(active, bool) or not isinstance(active, int):
        active = NO_PROFILE

    return {
        # There is no separate on/off switch: "no profile selected" is off, and
        # a second gate for the same thing only created states where a profile
        # was chosen but silently inert.
        # Whether the destructive-action warning has been acknowledged once.
        "warned": loaded.get("warned") is True,
        # Apply automatically when a game is launched - never at boot. The
        # desktop has no use for the undervolt, and something that came back by
        # itself after a reboot would be the hardest thing to suspect when the
        # device started misbehaving. "applyOnBoot" is the old key for the same
        # switch, read so an existing config is not silently turned off.
        "autoApply": (loaded.get("autoApply") is True
                      or loaded.get("applyOnBoot") is True),
        "active": max(NO_PROFILE, min(PROFILES - 1, active)),
        "profiles": profiles,
    }


def save_config(config):
    """Persist only. Editing a profile must not change the voltage under a
    running game; that happens on Apply or at the next launch."""
    config = config if isinstance(config, dict) else {}
    count = len(gpu_freqs())
    stored = config.get("profiles")
    stored = stored if isinstance(stored, list) else []
    active = config.get("active")
    if isinstance(active, bool) or not isinstance(active, int):
        active = NO_PROFILE
    clean = {
        "warned": config.get("warned") is True,
        "autoApply": config.get("autoApply") is True,
        "active": max(NO_PROFILE, min(PROFILES - 1, active)),
        "profiles": [
            clean_curve(stored[i] if i < len(stored) else [], count)
            for i in range(PROFILES)
        ],
    }
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(clean, indent=1) + "\n", encoding="utf-8")
    return clean


def write_shift(curve):
    """Ask the module to use this curve. The GMU rebuilds its vote table on its
    next boot (a GPU idle), so this is a request, not an immediate change."""
    if not module_loaded():
        return False, "module not loaded"
    if tune_running():
        return False, "a tuning run is in progress"
    if not shift_writable():
        return False, "shift is not writable - is adreno-uv.service running?"
    freqs = gpu_freqs()
    if not freqs:
        return False, "no operating points yet (GPU has not booted)"
    curve = clean_curve(curve, len(freqs))
    if not any(curve):
        # An all-zero curve is meaningful: it is how an undervolt is undone.
        pass
    try:
        SHIFT.write_text(",".join(str(v) for v in curve) + "\n")
    except OSError as exc:
        return False, str(exc)
    return True, ""


def gpu_active():
    return _read(GPU_RUNTIME_STATUS) == "active"


def can_apply_now():
    return GPU_AUTOSUSPEND.exists() and os.access(GPU_AUTOSUSPEND, os.W_OK)


def apply_now(timeout=0.5):
    """Make the current shift take effect immediately.

    The vote table is handed to the GMU once, at GMU boot, so a shift does
    nothing until the GPU next cycles. Under gamescope it never does on its
    own: at 60fps the gaps between submits are ~16ms and the autosuspend delay
    is 66ms, so the GPU sits active ~99% of the time.

    So we shorten the delay below the frame gap and let ordinary runtime PM do
    it - the GPU suspends between two frames and resumes, and the hook rewrites
    the table on the way back up. This is not a reset: no submits are lost, no
    fences are stranded, and the display never notices.

    The delay is restored as soon as one cycle has happened, because leaving it
    at zero makes the GPU suspend and resume between every frame.
    """
    if not gpu_active():
        # Nothing to force: a suspended GPU rebuilds its table on the next
        # resume and picks the new votes up for free.
        return True, "GPU idle - applies on its next wake"
    if not can_apply_now():
        return False, "cannot set autosuspend delay - re-run install.sh"

    original = _read(GPU_AUTOSUSPEND) or "66"
    before = _read(HITS)
    try:
        GPU_AUTOSUSPEND.write_text("0\n")
        # Every millisecond at delay 0 is a millisecond in which the GPU may
        # suspend and resume repeatedly. That is harmless on an idle desktop and
        # not something to leave running while a game is starting, so callers on
        # the launch path pass a short timeout and accept that a busy GPU will
        # instead pick the shift up at its next natural cycle.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _read(HITS) != before:
                return True, "applied live"
            time.sleep(0.01)
    except OSError as exc:
        return False, str(exc)
    finally:
        # Always put it back, including when the GPU never cycled: leaving the
        # delay at zero would cost a suspend/resume every frame.
        try:
            GPU_AUTOSUSPEND.write_text(f"{original}\n")
        except OSError:
            pass
    return False, "GPU did not idle in time - will apply when it next does"


def tune_start(step_seconds=60, verify_seconds=300, resume=True, mode="sweep"):
    """Launch the tuner as a transient unit, not as a child of this plugin.

    A step that hangs the GPU can take gamescope down, and Decky with it. The
    results are the entire point of the run, so the process recording them has
    to outlive the session it might break.

    resume=True (the default) continues from the curve in the journal instead of
    starting at stock. The tuner refuses to resume by itself if the OPP table
    has changed, so this is safe to leave on across a kernel change.
    """
    if not AUTOTUNE.is_file() or not STRESS.is_file():
        return False, "plugin is missing bin/autotune.py or bin/gpustress"
    if tune_status().get("running"):
        return False, "a tuning run is already in progress"
    try:
        TUNE_STOP.unlink()
    except OSError:
        pass
    ok, err = _systemctl(
        "reset-failed", TUNE_UNIT)  # clear any corpse from a previous run
    # Hold sleep off for the duration. The device suspends on its own - 12 times
    # on 2026-08-23 alone - and a suspend lands mid-step: the GPU is powered
    # down under a measurement that is still running, so whatever the step
    # reports afterwards is meaningless. A soak runs until stopped, so it would
    # hit the idle timer every single time.
    inhibit = []
    if Path("/usr/bin/systemd-inhibit").is_file():
        inhibit = ["/usr/bin/systemd-inhibit", "--what=sleep:idle",
                   "--why=GPU undervolt tuning", "--mode=block"]
    try:
        proc = subprocess.run(
            ["systemd-run", "--unit", TUNE_UNIT, "--collect",
             "--setenv", f"ADRENO_UV_SETTINGS={SETTINGS_DIR}"]
            + inhibit
            + ["/usr/bin/python3", str(AUTOTUNE),
               str(int(step_seconds)), str(int(verify_seconds)),
               "1" if resume else "0", mode],
            capture_output=True, text=True, timeout=30, env=_clean_env())
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "").strip()[-200:]
    return True, ""


def tune_stop():
    """Ask the tuner to stop between steps, keeping the curve it has built."""
    try:
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        TUNE_STOP.write_text("stop\n", encoding="utf-8")
    except OSError as exc:
        return False, str(exc)
    return True, ""


def tune_running():
    """Is a tuning run in progress?

    Guards everything in the plugin that touches the shift. The tuner owns the
    GPU for the length of a run, and a stray write from here - the Apply button,
    the bootstrap cycle, or _unload restoring stock on a Decky restart - would
    land in the middle of a measurement and turn a failing setting into a false
    pass. That is worse than no tuner, because the curve would be trusted.
    """
    try:
        return bool(TUNE_CGROUP.read_text().strip())
    except OSError:
        # No cgroup means no running unit.
        return False


def tune_halt(timeout=10):
    """Stop a run and wait for it to clean up after itself.

    Asks first, so the tuner restores stock voltage and unpins the frequency on
    its way out; only kills the unit if it will not go. Killing it outright
    would leave the GPU pinned to one OPP at whatever shift was under test.
    """
    if not tune_running():
        return False
    tune_stop()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not tune_running():
            return True
        time.sleep(0.25)
    _systemctl("stop", f"{TUNE_UNIT}.service")
    # It never got to tidy up, so do it here.
    write_shift([])
    return True


def tune_status():
    """The tuner's journal, plus whether its unit is actually alive.

    The journal alone is not enough: a run killed by a GPU hang leaves
    running=True behind forever, and the UI would wait for a process that no
    longer exists.
    """
    try:
        with TUNE_STATE.open(encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return {"running": False, "phase": "idle", "steps": []}
    if not isinstance(state, dict):
        return {"running": False, "phase": "idle", "steps": []}
    # Only ask about liveness when the journal claims to be running; otherwise
    # the common case costs one file read of the journal and nothing else.
    if state.get("running") and not tune_running():
        state["running"] = False
        state["phase"] = "interrupted"
        state["message"] = ("the tuner stopped without finishing - the last step "
                            "may have hung the GPU; it will be treated as unstable")
    state["stopRequested"] = TUNE_STOP.exists()
    return state


def bootstrap():
    """Make the hook fire once so the module can snapshot the OPP table.

    The module reads gpu_freqs and the stock votes from the GMU the first time
    a6xx_hfi_start runs, so a freshly loaded module knows nothing until the GPU
    next boots its GMU. Under gamescope that never happens on its own - the GPU
    is ~99% active - so the UI would sit on "waiting for the GPU" forever.

    Forcing one runtime-PM cycle costs nothing here: the shift is still zero, so
    the hook writes the stock votes straight back.
    """
    if gpu_freqs():
        return True, ""
    if not module_loaded():
        return False, "module not loaded"
    if tune_running():
        # The tuner is cycling the GPU for its own reasons; do not add to it.
        return False, "a tuning run is in progress"
    return apply_now()


def curve_is_live(curve):
    """Is this exact curve already in the GPU's vote table?

    Used to decide whether a FORCED GPU cycle is needed - never to skip the
    write. Writing is free; cycling is not, and a cycle while the compositor is
    drawing stalls the UI. Note an all-zero curve on a stock table is also
    already live, which is the case that made an earlier version of this check
    useless.
    """
    stock = _read(STOCK_VOTES).split(",")
    applied = _read(APPLIED_VOTES).split(",")
    freqs = gpu_freqs()
    if not stock or not applied or not freqs or len(applied) != len(stock):
        return False
    want = expected_votes(stock, clean_curve(list(curve), len(freqs)), freqs)
    return bool(want) and applied == want


def curve_for(config, index):
    """None means stock, and stock is an explicit zero curve rather than
    nothing: a previous game may have left a shift in place, and it has to be
    undone rather than inherited."""
    count = len(gpu_freqs())
    stock = [0] * count
    if index is None:
        return stock
    if isinstance(index, bool) or not isinstance(index, int):
        return stock
    if index < 0 or index >= PROFILES:
        # NO_PROFILE lands here: stock is the correct answer, and it is an
        # explicit zero curve so a previously applied profile gets undone.
        return stock
    profiles = config.get("profiles") or []
    if index >= len(profiles):
        return stock
    return clean_curve(profiles[index], count)


def expected_votes(stock, shift, freqs):
    """What applied_votes should look like for this shift.

    Mirrors the remap in a6xx_uv.c exactly, including the floor at the lowest
    real OPP. Comparing this against what the module actually wrote is the only
    honest way to tell "the undervolt is running" from "the undervolt is
    requested but the GPU has not cycled yet".
    """
    out = []
    for i, f in enumerate(freqs):
        if i >= len(stock):
            break
        if not f:
            out.append(stock[i])
            continue
        s = shift[i] if i < len(shift) else 0
        src = i - s
        if src < 1:
            src = 1
        while src < i and not freqs[src]:
            src += 1
        if src > i:
            src = i
        out.append(stock[src])
    return out


def matching_profile(config, shift, count):
    """Which stored profile the live shift corresponds to, or -1.

    The dropdown shows what is *selected*, which is not necessarily what the
    GPU is running - you can pick Medium and never press Apply. This is
    resolved from the shift the module actually holds, so the panel can name
    the profile in effect rather than the one highlighted.
    """
    if not any(shift):
        return -1
    target = clean_curve(list(shift), count)
    for i, profile in enumerate(config.get("profiles") or []):
        if clean_curve(profile, count) == target:
            return i
    return -1


def state():
    loaded = module_loaded()
    freqs = gpu_freqs()

    stock = _read(STOCK_VOTES).split(",") if loaded else []
    applied = _read(APPLIED_VOTES).split(",") if loaded else []
    shift = _read_list(SHIFT) if loaded else []
    expected = expected_votes(stock, shift, freqs) if stock and freqs else []
    # "Requested" is the shift asking for anything at all; "live" is the module
    # having actually written it into the table the GMU booted with.
    requested = any(shift)
    # Ground truth is what the module last wrote into the GMU's table, not what
    # was asked for. Deriving "stock" from the shift alone was wrong in the one
    # direction that matters: clearing a profile writes zeros immediately, but
    # the GPU keeps the old undervolted votes until it next cycles, and the UI
    # would report STOCK while the reduction was still running.
    live_differs = bool(applied) and applied != stock
    matches_request = bool(expected) and applied == expected
    live = live_differs and matches_request

    config = load_config()
    # -1 means either stock, or a shift that matches no stored profile (e.g.
    # edited after applying). The UI says "custom" rather than naming one.
    applied_profile = matching_profile(config, shift, len(freqs)) if requested else -1

    return {
        "moduleLoaded": loaded,
        # Loaded is not the same as usable: after an OS update the module
        # loads into a stock kernel it was not built for and goes inert.
        "kernelIncompatible": kernel_incompatible(),
        "moduleStatus": module_status(),
        # Why not, when not - the loader's own words rather than a guess.
        "moduleError": module_error(),
        "shiftWritable": shift_writable(),
        "canApplyNow": can_apply_now(),
        "freqs": freqs,
        "maxShift": MAX_SHIFT,
        "profileNames": PROFILE_NAMES,
        "shift": shift,
        # The undervolt is in the GMU's table right now.
        "undervoltLive": live,
        # What is running does not match what was asked for - in either
        # direction: a profile not yet in effect, or one not yet removed.
        "pending": bool(expected) and not matches_request,
        # There is an undervolt in the vote table right now, whatever was asked.
        "liveDiffersFromStock": live_differs,
        "appliedProfile": applied_profile,
        "stockVotes": _read(STOCK_VOTES) if loaded else "",
        "appliedVotes": _read(APPLIED_VOTES) if loaded else "",
        "hits": int(_read(HITS) or 0) if loaded else 0,
        # True only if something else is also driving GPU voltage.
        "armadaUvLive": ARMADA_UV.exists(),
        "config": config,
    }
