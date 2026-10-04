#!/usr/bin/env python3
"""Find the deepest stable undervolt per operating point, by measurement.

Runs OUTSIDE the Decky plugin, as a transient systemd unit. A step that hangs
the GPU can take gamescope - and therefore the plugin - down with it, and the
result is the only thing worth having, so the thing recording results must not
share a fate with the thing being tested.

Search order is round-based, deepening: every frequency at -1, then every
frequency at -2, and so on. A frequency that fails is frozen at its last stable
value and skipped in later rounds. Stopping halfway therefore still leaves a
coherent, conservative curve rather than a half-tuned one.

Every step is journalled BEFORE it runs. If the device hard-hangs and has to be
power-cycled, that record is what identifies the step that did it: on the next
start the tuner sees an unfinished step and treats it as a failure, so the
setting that killed the machine is never retried.

A step can also take the GPU out entirely - recovery fails, and from then on
every setting produces faults or wrong answers regardless of voltage. That is
not a measurement, so the sweep ENDS there: the culprit is recorded in
`crashedAt`, the curve is banked at its last stable values, and the recheck and
the soak are skipped rather than run against dead hardware. The next run skips
that point and carries on with the next frequency. Two independent detectors,
because the failure presents both ways: kernel markers for a failed reset, and
a zero-change control at stock after every failure - if stock fails too, the
GPU is broken and the setting is innocent.

A run RESUMES from the previous journal by default: the curve it reached is the
starting point, points with a measured edge stay frozen there, and depths
already proven stable are not re-tested. A sweep that was stopped, or that ended
with a hang and a power cycle, therefore continues where it left off instead of
spending an hour re-proving what it already knows. Resume is refused if the OPP
table has changed since - the curve is indexed by operating point, so a
different table would silently apply each result to the wrong frequency.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

PARAMS = Path("/sys/module/a6xx_uv/parameters")
SHIFT = PARAMS / "shift"
GPU_FREQS = PARAMS / "gpu_freqs"
HITS = PARAMS / "hits"

HERE = Path(__file__).resolve().parent
STRESS = HERE / "gpustress"

MAX_SHIFT = 8
STRESS_SIZE = 1024
STRESS_ITERS = 512

# Shapes of work for the soak to rotate through. One fixed workload only ever
# proves the curve survives THAT workload - a setting that fails on heavy memory
# traffic, or on the voltage transitions a bursty load forces, sails through an
# entire soak of the default load without a mark against it.
#
# Each entry needs its OWN reference checksum: the frame is deterministic for a
# given size and iteration count, so changing either changes the correct answer.
# Comparing against the wrong one would report corruption on a healthy GPU.
SOAK_LOADS = [
    {"name": "default", "size": 1024, "iters": 512, "args": []},
    # No burst: sustained load with no idle gaps, so the rail never gets to
    # settle. The opposite of the default's duty cycle.
    {"name": "steady", "size": 1024, "iters": 512, "args": ["-F"]},
    # Wide and shallow: a bigger framebuffer per pass, dominated by memory
    # traffic rather than ALU work.
    {"name": "wide", "size": 1536, "iters": 256, "args": []},
    # Short bursts with long gaps. Every gap is a power-state transition, which
    # is where an undervolt tends to fail rather than in steady state.
    {"name": "cycling", "size": 1024, "iters": 512, "args": ["-o", "3", "-f", "18"]},
]
DEFAULT_LOAD = SOAK_LOADS[0]
# Above this the die is hot enough that a failure says more about cooling than
# about voltage; wait for it to come down rather than record a bogus result.
TEMP_PAUSE_C = 85
TEMP_RESUME_C = 75

FAULT_RE = re.compile(r"CP REG PROTECT|CP HW FAULT|CP SW FAULT|hangcheck")

# Recovery itself FAILED. A hangcheck is routine - the driver resets the GPU and
# carries on - but these mean the reset did not take, and nothing runs correctly
# again until the device is power-cycled. Anything measured afterwards is noise:
# on 2026-08-22 a sweep ran for 11 more minutes past "cx gdsc didn't collapse"
# and recorded ten frequencies as failures, including 222 MHz at -1, which has
# passed every sweep ever run. The GPU was not executing at all - peak
# temperature fell from 66C to 40C while it "failed" step after step.
WEDGE_RE = re.compile(
    r"didn't collapse"
    r"|fenced register write"
    r"|GMU firmware fault"
    r"|imeout waiting for GMU"
    r"|failed to load GMU"
    r"|GMU is not ready")

# After a crash, how long to wait for the GPU to start working again before
# giving up on the run. A wedge does not always need a power cycle: on
# 2026-08-23 three separate steps stopped the GPU recovering, the run ended each
# time - and the GPU was fine minutes later, once nothing was submitting to it.
# Ending a sweep on the first crash makes one bad operating point cost the whole
# hour, and the soak is never reached at all.
RECOVER_TRIES = 6
RECOVER_WAIT = 10

# How many crashes a single run may carry on past. Continuing after ONE wedge
# saves an hour when a single operating point is simply too aggressive. Carrying
# on indefinitely does the opposite: on 2026-08-23 a run crashed at 832, 900 and
# 1050 MHz, recovered each time because the control said the GPU was working
# again, then went into 1100 MHz and took the device down hard enough to need a
# power button. Repeated wedges are not independent events - each one leaves the
# GPU worse - so past this many, stop and let the next run resume from a fresh
# boot with all of them blocked.
MAX_CRASHES = 2

# The zero-change control is short on purpose: it runs after every failure, and
# it only has to answer "does stock still work at all", not measure anything.
CONTROL_SECONDS = 8


def find_gpu_devfreq():
    for base in ("/sys/bus/platform/drivers/adreno", "/sys/bus/platform/devices"):
        for dev in sorted(Path(base).glob("*")):
            df = dev / "devfreq"
            if df.is_dir():
                for node in sorted(df.iterdir()):
                    if (node / "max_freq").exists():
                        return node
    for node in sorted(Path("/sys/class/devfreq").glob("*.gpu")):
        if (node / "max_freq").exists():
            return node
    return None


DEVFREQ = find_gpu_devfreq()


def read(path, default=""):
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return default


def write(path, value):
    try:
        Path(path).write_text(f"{value}\n")
        return True
    except OSError:
        return False


def gpu_temp_c():
    hottest = 0
    for zone in Path("/sys/class/thermal").glob("thermal_zone*"):
        if read(zone / "type").startswith("gpuss"):
            try:
                hottest = max(hottest, int(read(zone / "temp", "0")))
            except ValueError:
                pass
    return hottest // 1000


def kernel_marks():
    """(faults, wedges) from a single dmesg read, or None if it could not run.

    One read for both counts. dmesg is sampled before and after every step and
    the ring buffer rotates under load; two separate reads invite the counts to
    disagree about which window they describe. None rather than zeroes so a
    failed read is never mistaken for "nothing happened".
    """
    try:
        out = subprocess.run(["dmesg"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return len(FAULT_RE.findall(out)), len(WEDGE_RE.findall(out))


def apply_shift(curve):
    """Write the curve and force it live via one runtime-PM cycle."""
    write(SHIFT, ",".join(str(v) for v in curve))
    if not DEVFREQ:
        return
    pm = Path(str(DEVFREQ / "device" / "power"))
    delay = pm / "autosuspend_delay_ms"
    if not delay.exists():
        return
    original = read(delay, "66")
    before = read(HITS)
    write(delay, 0)
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline and read(HITS) == before:
        time.sleep(0.01)
    write(delay, original)


class Tuner:
    def __init__(self, settings_dir, step_seconds, verify_seconds, resume=True,
                 forever=False, mode="sweep"):
        self.resume = resume
        self.forever = forever
        # "sweep" finds limits by pushing past them. "soak" never pushes: it
        # takes the curve the user actually runs and tries to break it, which
        # is a different question and deserves a different entry point.
        self.mode = mode
        self.resumed_from = None
        self.dir = Path(settings_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "autotune.json"
        self.stop_path = self.dir / "autotune.stop"
        self.step_seconds = step_seconds
        self.verify_seconds = verify_seconds

        self.freqs = [int(f) for f in read(GPU_FREQS).split(",") if f.strip()]
        # Index 0 is the rail-off entry, and index i can never shift deeper
        # than i-1 because the module floors the source at the lowest real OPP.
        self.points = [i for i, f in enumerate(self.freqs) if i > 0 and f]
        self.curve = [0] * len(self.freqs)
        self.state = {}
        self.reference_crc = None
        # name -> checksum at stock. Only the default is needed for a sweep.
        self.reference_crcs = {}
        self.loads = [DEFAULT_LOAD]

    # ---------- journal ----------

    def save(self, **changes):
        self.state.update(changes)
        self.state["curve"] = list(self.curve)
        # Two curves come out of one sweep, and the user picks which to run.
        #
        # NORMAL ("recommended") - a step of margin wherever an edge was
        # actually found. If a point never failed, because the sweep was
        # stopped or it hit the shift ceiling, its value is proven stable but
        # not proven maximal, and backing off would discard a real result for
        # nothing. This is the conservative curve and stays the default.
        #
        # AGGRESSIVE - the last passing step, with no margin anywhere. curve[i]
        # is by construction the deepest shift that PASSED its stress run, so it
        # is a measurement rather than an estimate and is applied exactly.
        #
        # That holds after a crash too: the depth that crashed is recorded in
        # crashedAt and blocked from ever being retried, so what is left in the
        # curve is still a step that passed. Aggressive means aggressive - the
        # margin lives in `recommended`, and that is what the choice is for.
        failed = self.state.get("failedAt") or {}
        self.state["recommended"] = [
            max(0, v - 1) if failed.get(str(i)) else v
            for i, v in enumerate(self.curve)
        ]
        self.state["aggressive"] = list(self.curve)
        self.state["freqs"] = self.freqs
        self.state["updated"] = time.time()
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1) + "\n", encoding="utf-8")
        tmp.replace(self.state_path)

    def load_previous(self):
        """Recover from a run that never came back."""
        try:
            with self.state_path.open(encoding="utf-8") as f:
                previous = json.load(f)
        except (OSError, ValueError):
            return None
        current = previous.get("current")
        if previous.get("running") and isinstance(current, dict):
            return current
        return None

    def load_resume(self):
        """The previous run's curve and measured edges, or None.

        Refused when the OPP table has moved. `curve` is indexed by operating
        point, so resuming onto a different table would apply each result to
        the wrong frequency - and a shift is relative, so that is not a small
        error: -5 at 832 MHz and -5 at 1100 MHz land on completely different
        voltage corners. Better to sweep again than to trust a misaligned curve.
        """
        try:
            with self.state_path.open(encoding="utf-8") as f:
                previous = json.load(f)
        except (OSError, ValueError):
            return None
        if not isinstance(previous, dict):
            return None
        curve = previous.get("curve")
        if not isinstance(curve, list) or not any(
                isinstance(v, int) and not isinstance(v, bool) and v for v in curve):
            # Nothing worth resuming from - an all-zero curve IS from scratch.
            return None
        if previous.get("freqs") != self.freqs:
            return {"mismatch": True, "freqs": previous.get("freqs")}

        clean = []
        for i in range(len(self.freqs)):
            value = curve[i] if i < len(curve) else 0
            if isinstance(value, bool) or not isinstance(value, int):
                value = 0
            clean.append(max(0, min(value, self.useful_max(i))))

        failed = {}
        for key, value in (previous.get("failedAt") or {}).items():
            try:
                index = int(key)
            except (TypeError, ValueError):
                continue
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= index < len(self.freqs):
                failed[index] = value
        crashed = {}
        for key, value in (previous.get("crashedAt") or {}).items():
            try:
                index = int(key)
            except (TypeError, ValueError):
                continue
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= index < len(self.freqs):
                crashed[index] = value
        return {"curve": clean, "failedAt": failed, "crashedAt": crashed,
                "updated": previous.get("updated"), "phase": previous.get("phase")}

    def load_profile_curve(self):
        """The curve the user actually runs, from the plugin's own config.

        Soaking the tuner's own curve would only re-prove the tuner. What
        matters is whether the profile in use survives - it may have been
        hand-edited, or copied from a sweep taken against a different kernel.
        """
        try:
            with (self.dir / "profiles.json").open(encoding="utf-8") as f:
                config = json.load(f)
        except (OSError, ValueError):
            return None
        if not isinstance(config, dict):
            return None
        profiles = config.get("profiles") or []
        try:
            index = int(config.get("active", -1))
        except (TypeError, ValueError):
            return None
        if index < 0 or index >= len(profiles):
            return None
        raw = profiles[index] or []
        curve = []
        for i in range(len(self.freqs)):
            value = raw[i] if i < len(raw) else 0
            if isinstance(value, bool) or not isinstance(value, int):
                value = 0
            curve.append(max(0, min(value, self.useful_max(i))))
        self.state["soakedProfile"] = index
        return curve

    def run_soak_profile(self, original):
        """Soak the active profile until stopped. Never deepens anything."""
        curve = self.load_profile_curve()
        if not curve or not any(curve):
            self.save(running=False, phase="failed",
                      message="the active profile is stock - nothing to soak")
            self.unpin(original)
            return 1
        self.save(running=True, phase="reference",
                  message="measuring reference checksum")
        ok, detail = self.measure_reference()
        if not ok:
            self.save(running=False, phase="failed",
                      message=f"reference run failed: {detail}")
            self.unpin(original)
            return 1
        self.curve = list(curve)
        apply_shift(self.curve)
        self.save(referenceCrc=self.reference_crc, phase="soak",
                  message="soaking the active profile until stopped")
        survived = self.soak_forever()
        write(SHIFT, "0") if survived else None
        self.unpin(original)
        stopped = self.stop_requested()
        try:
            self.stop_path.unlink()
        except OSError:
            pass
        if not survived:
            self.save(running=False, current=None, phase="wedged")
            return 2
        self.save(running=False, current=None,
                  phase="stopped" if stopped else "done",
                  message=("stopped by user" if stopped
                           else "soak finished - nothing left undervolted"))
        return 0

    def stop_requested(self):
        return self.stop_path.exists()

    # ---------- measurement ----------

    def useful_max(self, index):
        return max(0, min(MAX_SHIFT, index - 1))

    def pin(self, freq):
        if not DEVFREQ:
            return
        # Order matters: raise the ceiling before the floor, or the write is
        # rejected for crossing.
        write(DEVFREQ / "max_freq", freq)
        write(DEVFREQ / "min_freq", freq)

    def unpin(self, original):
        if not DEVFREQ or not original:
            return
        write(DEVFREQ / "min_freq", original[0])
        write(DEVFREQ / "max_freq", original[1])

    def wait_for_cool(self):
        if gpu_temp_c() < TEMP_PAUSE_C:
            return
        self.save(message=f"paused: GPU at {gpu_temp_c()}C, waiting to cool")
        while gpu_temp_c() > TEMP_RESUME_C and not self.stop_requested():
            time.sleep(5)

    def run_stress(self, seconds, load=None):
        """Returns (verdict, detail, peak_temp). verdict is 'pass' or a reason."""
        load = load or DEFAULT_LOAD
        args = [str(STRESS), "-s", str(load["size"]), "-t", str(seconds),
                "-i", str(load["iters"])] + list(load["args"])
        crc = self.reference_crcs.get(load["name"]) or (
            self.reference_crc if load is DEFAULT_LOAD else None)
        if crc:
            args += ["-r", crc]

        before = kernel_marks()
        peak = gpu_temp_c()
        try:
            proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
        except OSError as exc:
            return "error", f"cannot run gpustress: {exc}", peak

        output = []
        while proc.poll() is None:
            peak = max(peak, gpu_temp_c())
            time.sleep(1)
            if self.stop_requested():
                proc.terminate()
                return "stopped", "stopped by user", peak
        output = (proc.stdout.read() or "").strip() if proc.stdout else ""
        code = proc.returncode

        after = kernel_marks()
        if before and after:
            # Checked before the fault count, and it has to be: a wedge raises
            # that too, so testing faults first would file the end of the run
            # as an ordinary measured edge for this voltage.
            if after[1] > before[1]:
                return "wedged", "GPU recovery failed - power cycle needed", peak
            # A GPU that faulted counts as unstable even if the client survived.
            if after[0] > before[0]:
                return "fault", "GPU fault in dmesg", peak
        if code == 4:
            return "corrupt", "checksum mismatch - wrong results", peak
        if code == 2:
            return "reset", "GL context lost - GPU reset", peak
        if code != 0:
            return "error", f"gpustress exit {code}: {output[-200:]}", peak
        return "pass", "", peak

    def reference_for(self, load):
        """Checksum for one load shape, at stock. None if it could not be taken."""
        try:
            proc = subprocess.run(
                [str(STRESS), "-s", str(load["size"]), "-t", "6",
                 "-i", str(load["iters"])] + list(load["args"]),
                capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            return None
        match = re.search(r"crc ([0-9a-f]{8})", proc.stdout or "")
        if proc.returncode != 0 or not match:
            return None
        return match.group(1)

    def measure_reference(self):
        """Checksum at stock, which every later run is compared against.

        Applies stock to the HARDWARE without touching self.curve. It used to
        zero the curve itself, and since the journal is read live by the UI that
        made a resumed run flash the whole plot back to stock and then restore
        it seconds later - the curve looked like it had been thrown away.

        A soak takes one per load shape. A sweep takes only the default, so its
        results stay directly comparable with every sweep before it.
        """
        apply_shift([0] * len(self.freqs))
        crc = self.reference_for(DEFAULT_LOAD)
        if not crc:
            return False, "no crc from the reference run"
        self.reference_crc = crc
        self.reference_crcs = {DEFAULT_LOAD["name"]: crc}
        self.loads = [DEFAULT_LOAD]
        if self.mode == "soak":
            for load in SOAK_LOADS[1:]:
                self.save(message=f"measuring the reference for the {load['name']} load")
                other = self.reference_for(load)
                if other:
                    self.reference_crcs[load["name"]] = other
                    self.loads.append(load)
                else:
                    # Better to soak with fewer shapes than to compare a load
                    # against a checksum that was never measured for it.
                    self.save(message=f"skipping the {load['name']} load - no reference")
            self.state["soakLoads"] = [load["name"] for load in self.loads]
        return True, crc

    def control_ok(self):
        """Re-run the reference at stock. False means the GPU is broken.

        The zero-change control, applied to the one decision that matters. A
        failure only means "this voltage is too low" if stock still passes;
        once the GPU has been left in a bad state EVERY setting fails, and
        recording those as measured edges poisons the curve for every run
        afterwards. Catches the case the dmesg markers miss - a GPU returning
        wrong arithmetic without logging anything at all.
        """
        apply_shift([0] * len(self.freqs))
        verdict, detail, _ = self.run_stress(CONTROL_SECONDS)
        apply_shift(self.curve)
        return verdict == "pass", (detail or verdict)

    def gpu_came_back(self, phase):
        """Wait for the GPU to work again after a crash. False means it did not.

        The crash is already recorded and blocked by the time this runs, so the
        only question is whether the REST of the sweep can still be trusted -
        and the control answers that directly rather than by guessing.
        """
        for attempt in range(1, RECOVER_TRIES + 1):
            if self.stop_requested():
                return False
            self.save(message=(
                f"GPU crashed - waiting for it to come back "
                f"({attempt * RECOVER_WAIT}s of {RECOVER_TRIES * RECOVER_WAIT}s)"))
            time.sleep(RECOVER_WAIT)
            ok, _ = self.control_ok()
            if ok:
                self.save(phase=phase, message=(
                    f"GPU recovered after {attempt * RECOVER_WAIT}s - "
                    f"that point is blocked, carrying on"))
                return True
        return False

    def record_crash(self, index, depth, detail):
        """Remember the step that killed the GPU and freeze the curve there.

        Kept as `crashedAt` as well as in failedAt, because the two mean
        different things to the next run. failedAt says "this point has a
        measured edge", which is what entitles `recommended` to back it off by
        one. A crash says something stronger - never run this again - and if
        they shared a field the next run could not tell them apart.
        """
        previous = (self.state.get("crashedAt") or {}).get(str(index))
        if isinstance(previous, int) and not isinstance(previous, bool):
            depth = min(depth, previous)
        self.state.setdefault("crashedAt", {})[str(index)] = depth
        # Lowest known failure wins, the same rule a recovered hang follows: a
        # crash at 4 puts 4 out of reach whatever failedAt happened to hold.
        known = (self.state.get("failedAt") or {}).get(str(index))
        if isinstance(known, int) and not isinstance(known, bool):
            depth = min(depth, known)
        self.state.setdefault("failedAt", {})[str(index)] = depth
        self.state["crash"] = {"index": index, "freq": self.freqs[index],
                               "shift": depth, "detail": detail, "at": time.time()}
        self.state["lastStable"] = list(self.curve)
        self.save(phase="wedged", message=(
            f"GPU stopped recovering at {self.freqs[index] // 1000000} MHz "
            f"-{depth}. Sweep ended; curve kept at the last stable values."))

    def test(self, index, depth, load=None):
        """One step: journal it, run it, judge it."""
        self.wait_for_cool()
        if self.stop_requested():
            return "stopped", "", 0

        trial = list(self.curve)
        trial[index] = depth
        start_temp = gpu_temp_c()

        # Journalled BEFORE anything is applied: if the device dies here, this
        # is the record that says which setting did it.
        self.save(running=True, current={
            "index": index, "freq": self.freqs[index], "shift": depth,
            "startTemp": start_temp, "startedAt": time.time(),
        }, message=f"testing {self.freqs[index] // 1000000} MHz at -{depth}")

        self.pin(self.freqs[index])
        apply_shift(trial)
        verdict, detail, peak = self.run_stress(self.step_seconds, load)

        # The control decides, in BOTH directions - the kernel markers only
        # nominate. Stock still passing means the GPU recovered, so this is an
        # ordinary edge for this voltage and the sweep carries on; stock failing
        # too means the GPU is gone and the run must end.
        #
        # This matters because the markers are ambiguous. On 2026-08-23 a step
        # at 443 MHz -4 produced "fenced register write" and a burst of GMU
        # fence errors, the run aborted as wedged - and the GPU was fine
        # afterwards, passing the reference checksum at full speed. Treating the
        # marker as terminal threw away the rest of a 35-minute sweep for a
        # fault the driver had already recovered from.
        if verdict not in ("pass", "stopped"):
            ok, control = self.control_ok()
            if not ok:
                if verdict != "wedged":
                    detail = f"{detail}; stock control also failed ({control})"
                verdict = "wedged"
            elif verdict == "wedged":
                verdict = "fault"
                detail = f"{detail}; stock control passed - an edge, not a wedge"

        step = {"index": index, "freq": self.freqs[index], "shift": depth,
                "result": verdict, "detail": detail,
                "startTemp": start_temp, "peakTemp": peak,
                "seconds": self.step_seconds,
                "load": (load or DEFAULT_LOAD)["name"]}
        self.state.setdefault("steps", []).append(step)

        if verdict == "pass":
            self.curve = trial
        else:
            # Stay at the last stable setting for this point.
            apply_shift(self.curve)
        self.save(current=None)
        return verdict, detail, peak

    def soak_forever(self):
        """Keep re-testing the finished curve until asked to stop.

        A sweep proves each point with ONE stress run. That is enough to locate
        an edge and nowhere near enough to trust a curve for an evening of play:
        a marginal setting passes the first run and fails the fifth, at a
        different temperature or against a different workload. This keeps going
        - every undervolted point re-tested in turn, round after round - and
        backs off anything that fails.

        Every change is journalled as it happens, so stopping at any moment
        leaves a curve that is coherent and strictly better proven than when the
        soak began. Returns False if the GPU stopped recovering.
        """
        rounds = 0
        crashes = 0
        while not self.stop_requested():
            live = [i for i in self.points if self.curve[i] > 0]
            if not live:
                self.save(phase="soak", message="nothing undervolted left to soak")
                return True
            rounds += 1
            # A different shape of work each round, so a curve has to survive
            # all of them rather than just the one it was tuned against.
            load = self.loads[(rounds - 1) % len(self.loads)]
            for index in live:
                if self.stop_requested():
                    break
                depth = self.curve[index]
                if depth <= 0:
                    continue          # backed off to stock earlier this round
                passes = (self.state.get("soakPasses") or {}).get(str(index), 0)
                self.save(phase="soak", soakRound=rounds, soakLoad=load["name"], message=(
                    f"soak round {rounds} ({load['name']} load): "
                    f"{self.freqs[index] // 1000000} MHz at -{depth} "
                    f"({passes} passed so far)"))
                verdict, detail, _ = self.test(index, depth, load)
                if verdict == "stopped":
                    break
                if verdict == "wedged":
                    self.record_crash(index, depth, detail)
                    crashes += 1
                    if crashes >= MAX_CRASHES or not self.gpu_came_back("soak"):
                        return False
                    # Blocked by crashedAt, and the curve was backed off to the
                    # last value that passed - keep soaking the rest.
                    self.curve[index] = max(0, depth - 1)
                    apply_shift(self.curve)
                    self.save()
                    continue
                if verdict == "pass":
                    self.state.setdefault("soakPasses", {})[str(index)] = passes + 1
                    self.save()
                    continue
                # It failed a setting it had ALREADY passed. Back off and say so
                # loudly: this is the whole reason the soak exists, and it is
                # exactly what a single-pass sweep cannot see.
                self.curve[index] = max(0, depth - 1)
                apply_shift(self.curve)
                known = (self.state.get("failedAt") or {}).get(str(index))
                self.state.setdefault("failedAt", {})[str(index)] = (
                    min(depth, known)
                    if isinstance(known, int) and not isinstance(known, bool)
                    else depth)
                self.state.setdefault("soakFailures", []).append({
                    "round": rounds, "index": index, "freq": self.freqs[index],
                    "shift": depth, "result": verdict, "detail": detail,
                    "load": load["name"],
                    "backedOffTo": self.curve[index], "at": time.time()})
                self.save(message=(
                    f"soak: {self.freqs[index] // 1000000} MHz failed at -{depth} "
                    f"on the {load['name']} load after {passes} passes - "
                    f"backed off to -{self.curve[index]}"))
        return True

    # ---------- the sweep ----------

    def run(self):
        if not STRESS.is_file():
            self.save(running=False, phase="failed",
                      message=f"no stress binary at {STRESS}")
            return 1
        if not self.points:
            self.save(running=False, phase="failed",
                      message="no operating points - has the GPU booted?")
            return 1

        original = None
        if DEVFREQ:
            original = (read(DEVFREQ / "min_freq"), read(DEVFREQ / "max_freq"))

        killed = self.load_previous()
        resumed = self.load_resume() if self.resume else None
        self.state = {"steps": [], "limits": {}, "startedAt": time.time(),
                      "stepSeconds": self.step_seconds}
        blocked = {}
        resume_curve = [0] * len(self.freqs)
        if resumed and resumed.get("mismatch"):
            # Say so rather than silently starting over: "it ran for an hour
            # again" is exactly the surprise this message exists to prevent.
            self.state["resumeRefused"] = "operating points changed since the last run"
            resumed = None
        if resumed:
            resume_curve = resumed["curve"]
            # Publish it BEFORE the first save. Otherwise the opening journal
            # write says "all stock", which is what the editor draws while the
            # reference is being measured.
            self.curve = list(resume_curve)
            # A point with a measured edge is finished. Carrying failedAt over
            # keeps it frozen AND keeps `recommended` entitled to back it off -
            # that back-off is only justified where an edge was actually found.
            for index, depth in resumed["failedAt"].items():
                blocked[index] = depth
                self.state.setdefault("failedAt", {})[str(index)] = depth
            # A crash is carried forward verbatim and blocks that point for
            # good, so the sweep walks on to the NEXT frequency instead of back
            # into the step that took the device down.
            for index, depth in (resumed.get("crashedAt") or {}).items():
                blocked[index] = min(depth, blocked.get(index, depth))
                self.state.setdefault("crashedAt", {})[str(index)] = depth
                self.state.setdefault("failedAt", {})[str(index)] = blocked[index]
            self.resumed_from = {"curve": list(resume_curve),
                                 "updated": resumed.get("updated"),
                                 "phase": resumed.get("phase")}
            self.state["resumedFrom"] = self.resumed_from
        if killed:
            # The previous run never reported back: that step took the device
            # down, so it is a failure and must never be retried.
            index, shift = killed.get("index"), killed.get("shift")
            if isinstance(index, int) and isinstance(shift, int):
                # Lowest known failure wins. A resumed failedAt of 4 and a hang
                # at 5 means 4 is still a failure: taking the hang alone would
                # let depth 4 be retried as if it had never been measured.
                shift = min(shift, blocked.get(index, shift))
                blocked[index] = shift
                self.state.setdefault("failedAt", {})[str(index)] = shift
                self.state.setdefault("crashedAt", {})[str(index)] = shift
                self.state["steps"].append({
                    "index": index, "freq": killed.get("freq"), "shift": shift,
                    "result": "hang", "detail": "device did not survive this step",
                    "startTemp": killed.get("startTemp"), "peakTemp": None,
                    "seconds": 0})

        try:
            self.stop_path.unlink()
        except OSError:
            pass

        if self.mode == "soak":
            return self.run_soak_profile(original)

        self.save(running=True, phase="reference", message="measuring reference checksum")
        ok, detail = self.measure_reference()
        if not ok:
            self.save(running=False, phase="failed",
                      message=f"reference run failed: {detail}")
            self.unpin(original)
            return 1
        self.save(referenceCrc=self.reference_crc)

        # The reference had to be taken with stock applied to the hardware, so
        # put the resumed curve back on the GPU before the sweep starts - every
        # step below is measured on top of it, not on top of stock. self.curve
        # already holds it, so only the hardware needs catching up.
        if any(resume_curve):
            self.curve = list(resume_curve)
            apply_shift(self.curve)
            done = sum(1 for v in resume_curve if v)
            self.save(message=f"resuming from the previous curve ({done} points already tuned)")

        alive = [i for i in self.points if self.useful_max(i) >= 1]
        first_tested = None
        wedged = False
        crashes = 0

        for depth in range(1, MAX_SHIFT + 1):
            if not alive or self.stop_requested():
                break
            self.save(phase="sweep", depth=depth)
            for index in list(alive):
                if self.stop_requested():
                    break
                if depth > self.useful_max(index):
                    alive.remove(index)
                    continue
                if blocked.get(index) is not None and depth >= blocked[index]:
                    alive.remove(index)
                    continue
                if depth <= self.curve[index]:
                    # Already measured stable in an earlier run. Skip rather
                    # than drop: this point still has deeper shifts to try.
                    continue
                if first_tested is None:
                    first_tested = (index, depth)
                verdict, detail, _ = self.test(index, depth)
                if verdict == "stopped":
                    break
                if verdict == "wedged":
                    # Remember which step did it, then find out whether the GPU
                    # is actually gone or merely fell over. If it comes back,
                    # this point is blocked for good and the sweep carries on
                    # with the others; if it does not, nothing measured from now
                    # on would mean anything, so stop.
                    self.record_crash(index, depth, detail)
                    crashes += 1
                    if crashes < MAX_CRASHES and self.gpu_came_back("sweep"):
                        blocked[index] = depth
                        alive.remove(index)
                        continue
                    if crashes >= MAX_CRASHES:
                        self.save(message=(
                            f"{crashes} crashes this run - stopping rather than "
                            f"pushing a GPU that keeps falling over. All of them "
                            f"are blocked; resume after a reboot to carry on."))
                    wedged = True
                    break
                if verdict != "pass":
                    # Record that this point has a measured edge, which is what
                    # entitles the recommendation to back off from it.
                    self.state.setdefault("failedAt", {})[str(index)] = depth
                    alive.remove(index)
                self.state["limits"][str(index)] = self.curve[index]
                self.save()
            if wedged:
                break

        # The answer as measured, banked before the recheck or the soak can
        # touch it: neither may be able to erase an hour of real results.
        if not wedged:
            self.state["lastStable"] = list(self.curve)
            self.save()

        # The very first step ran on a cold GPU, which is the easiest test of
        # the whole sweep. Repeat it now that the device is hot.
        if first_tested and not wedged and not self.stop_requested():
            index, depth = first_tested
            if self.curve[index] >= depth:
                # What this point ended the sweep at. The recheck re-runs an
                # EARLY, shallow step; it is not this point's result.
                achieved = self.curve[index]
                self.save(phase="recheck",
                          message=f"re-testing {self.freqs[index] // 1000000} MHz hot")
                verdict, _, _ = self.test(index, depth)
                if verdict == "pass":
                    # test() writes its trial back on success, which would DEMOTE
                    # a point that later rounds pushed deeper - a hot pass at -4
                    # is not a reason to throw away a measured -8. Only a failure
                    # carries information here.
                    if self.curve[index] != achieved:
                        self.curve[index] = achieved
                        apply_shift(self.curve)
                elif verdict != "stopped":
                    self.curve[index] = max(0, min(achieved, depth - 1))
                    apply_shift(self.curve)

        # One long soak on the finished curve. A sweep tests candidates; this
        # tests the answer, which is the only result anyone relies on.
        if not wedged and not self.stop_requested() and any(self.curve):
            self.save(phase="verify",
                      message=f"verifying the curve for {self.verify_seconds}s")
            apply_shift(self.curve)
            verdict, detail, peak = self.run_stress(self.verify_seconds)
            self.state["verify"] = {"result": verdict, "detail": detail, "peakTemp": peak}
            if verdict == "wedged":
                # The soak did not disprove the curve, the GPU stopped working.
                # Backing off here would discard an hour of real measurement on
                # the strength of a run that measured nothing.
                wedged = True
                self.state["message"] = ("verification could not run - GPU "
                                         "stopped recovering; curve kept as measured")
            elif verdict not in ("pass", "stopped"):
                # Do not hand back a curve that failed its own soak test.
                self.curve = [max(0, v - 1) for v in self.curve]
                self.state["message"] = "verification failed - curve backed off one step"

        # Then, if asked, never stop. Deliberately after the soak above, so a
        # forever run still produces a normal, complete result first - stopping
        # it five minutes in leaves exactly what a plain run would have.
        if self.forever and not wedged and not self.stop_requested():
            self.save(phase="soak", soakRound=0,
                      message="soaking the curve until stopped")
            if not self.soak_forever():
                wedged = True

        # Stock on a clean finish: day to day the plugin owns what runs, and it
        # applies the saved profile itself. After a wedge the write would not
        # reach the hardware anyway, and the curve worth having is the one
        # banked in the journal.
        if not wedged:
            write(SHIFT, "0")
        self.unpin(original)
        # Clear the flag so the next run does not see a stale stop request and
        # so the UI stops reporting one.
        stopped = self.stop_requested()
        try:
            self.stop_path.unlink()
        except OSError:
            pass
        if wedged:
            # phase and message are already the crash's own - do not overwrite
            # them with "finished", which is exactly what this run did not do.
            self.save(running=False, current=None, phase="wedged")
            return 2
        self.save(running=False, current=None,
                  phase="stopped" if stopped else "done",
                  message="stopped by user" if stopped else "finished")
        return 0


def main():
    settings = os.environ.get("ADRENO_UV_SETTINGS",
                              "/home/deck/homebrew/settings/adreno-uv")
    step = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    verify = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    # Resume is the default; "0" as the third argument (or ADRENO_UV_TUNE_FRESH)
    # forces a sweep from stock. Positional, and last, so existing callers that
    # pass two arguments keep working.
    resume = not (len(sys.argv) > 3 and sys.argv[3] in ("0", "fresh", "--fresh"))
    if os.environ.get("ADRENO_UV_TUNE_FRESH") == "1":
        resume = False
    # Fifth positional, so the callers that predate it still work unchanged.
    # "soak" skips the sweep entirely and stress-tests the active profile.
    arg = sys.argv[4] if len(sys.argv) > 4 else ""
    mode = "soak" if arg in ("soak", "--soak") else "sweep"
    forever = arg in ("1", "forever", "--forever") \
        or os.environ.get("ADRENO_UV_TUNE_FOREVER") == "1"
    return Tuner(settings, step, verify, resume, forever, mode).run()


if __name__ == "__main__":
    sys.exit(main())
