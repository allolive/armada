# Adreno Undervolt

A standalone Decky plugin that undervolts Adreno GPUs, with three profiles,
applied when you ask for them or automatically as a game launches.

Developed and tested on the AYN Odin 3 (SM8750, Adreno 830), but nothing in it
is specific to that device: it works on any Adreno whose GPU uses the msm
driver's a6xx-style GMU DVFS (a6xx/a7xx/a8xx, so most Snapdragon 8-series since
~845). The GPU is discovered through the adreno driver binding rather than a
hardcoded address.

It does **not** work on Mali, Tegra or PowerVR - those have no
`a6xx_hfi_start` and no RPMh corner table, and would undervolt through OPP
microvolt values and a real regulator instead.

It is deliberately separate from Armada Control. It shares no file, no config
path and no service with Armada, so the two can be installed side by side and
neither needs the other to be modified.

## How it works

The undervolt itself is an out-of-tree kernel module, `a6xx_uv`, which places a
kprobe on `a6xx_hfi_start`. Each time the GMU boots, the hook rewrites
`gmu->gx_arc_votes[]` so a given operating point uses the RPMh voltage corner
belonging to a *lower* operating point. That is what a "shift" is: a shift of 1
on the 832 MHz point moves it from the SVS_L2 corner to SVS_L1.

Nothing is patched into the kernel tree.

### Making a change take effect immediately

The vote table reaches the GMU once, at GMU boot, so a new shift does nothing
until the GPU next cycles — and under gamescope it never does on its own. At
60fps the gaps between submits are ~16ms while the autosuspend delay is 66ms,
so the GPU sits active ~99% of the time.

Apply therefore drops the autosuspend delay to 0, waits for one suspend/resume
(typically ~20ms), and puts the delay back. The GPU idles between two frames
and the hook reprograms the table on the way back up. This is ordinary runtime
PM: no reset, no lost submits, no stranded fences, and the display never
notices.

**Rejected alternative: GPU recovery.** Queueing the driver's `recover_work` —
what hangcheck runs — looks like the obvious lever and is a trap. With no
in-flight submit `recover_worker` logs `hangcheck recover!` and returns without
calling `->recover()`, so it does nothing; with one, it counts a fault against
the submit and can mark the app's VM unusable. Calling `a6xx_recover()`
directly would cycle power but skips the `retire_submits()`/replay that
`recover_worker` wraps it in, stranding fences the compositor waits on. All
three were tested or read before being discarded.

### Why shifts and not millivolts

The GPU rail is driven by RPMh voltage *corners* (LOW_SVS … TURBO_L3), not by a
regulator the kernel can set to an arbitrary voltage. There is no millivolt
value to write; the only thing that can be changed is which corner an operating
point votes for. So the unit the UI exposes is the honest one.

### Privileges

Decky **does not honour `plugin.json`'s `root` flag on this build** — plugin
backends run as the desktop user (uid 1000). This is why Armada Control ships a
privileged daemon and talks to it over a socket; it is a requirement, not a
preference.

`adreno-uv` needs far less than a daemon, so instead of copying that pattern it
installs one oneshot unit, `adreno-uv.service`, which:

1. `insmod`s the module at boot, and
2. `chown`s the two knobs the plugin drives to the desktop user:
   `/sys/module/a6xx_uv/parameters/shift` and the GPU's
   `power/autosuspend_delay_ms`.

The plugin then writes that one file directly. The introspection parameters stay
root-owned and read-only, so the user is handed control of the undervolt without
being handed anything else, and there is no long-running root process to keep
correct.

## Layout

| path | role |
|---|---|
| `main.py` | Decky entry point |
| `py_modules/adreno_uv/uv.py` | module I/O, profile storage, clamping |
| `src/Content.tsx` | quick-access panel UI |
| `src/lib/gameHook.ts` | one-shot: applies at the first game launch |
| `bin/a6xx_uv.ko` | the module, built per kernel |
| `install.sh` | installs the unit and the module (run as root) |

Profiles live in Decky's own settings directory, **not** `/etc/armada/`.

Files created outside the plugin directory — all new, none belonging to Armada:

- `/etc/systemd/system/adreno-uv.service`
- `/var/lib/adreno-uv/{a6xx_uv.ko,load.sh}`

## Building and installing

Frontend:

```sh
npm install && npm run build      # emits dist/
```

The dev host has no node, and the device's container policy rejects the
docker.io node images, so `helpers/build-plugin.sh` builds it inside the same
pinned Fedora image the kernel build uses.

Module: built out-of-tree against the running kernel's build tree — see
`armada-packages/kernel/uvmod/` and `build-uvmod.sh`. Copy the resulting
`a6xx_uv.ko` into `bin/`.

Then copy the plugin to `~/homebrew/plugins/adreno-uv/` and run `install.sh` as
root.

**The `.ko` is bound to one exact kernel build** (vermagic and symbol CRCs). A
kernel update makes `insmod` fail, and the plugin then reports the module as
unavailable rather than pretending to undervolt. Rebuild it after any kernel
change.

## Interaction with Armada Control

Armada Control contains its own undervolt UI that writes to
`/sys/module/msm/parameters/gpu_volt_deltas`. That interface comes from kernel
patch 0123, which does not boot, so it is inert dead code. If it ever becomes
live, both would be driving the same vote table; the plugin detects that node's
existence and warns rather than silently competing for it.

## Status

Verified on device (kernel 7.1.5):

- [x] Module builds with the `gpu_freqs` parameter
- [x] `gpu_freqs` reports 15 operating points, 0 (rail-off) + 160 MHz … 1100 MHz,
      index-aligned with `stock_votes`
- [x] `adreno-uv.service` loads the module at boot and hands over the shift knob
- [x] The **unprivileged** user can write `shift`
- [x] A shift of 1 on indices 9–14 moves each to the corner one step lower,
      confirmed in `applied_votes`; zero GPU faults
- [x] Frontend builds clean; plugin loads in Decky with no errors
- [x] Apply-on-startup verified across a real reboot: unit loads the module,
      plugin applies on top, indices 9–14 one corner lower, 0 faults
- [x] `_unload` restores stock when apply-on-startup is off, and leaves the
      undervolt alone when it is on
- [x] Immediate apply: unprivileged, one GMU boot, ~20ms, delay restored,
      votes verified live and reverted again — zero faults
- [ ] Panel UI not yet exercised in Game Mode (needs hands on the device)
- [ ] First-game-launch one-shot not yet observed firing on a real launch
- [ ] No power/thermal benefit measured yet — stability only

### Note on testing

`_unload` zeroes the shift unless apply-on-startup is set, so restarting Decky
mid-test can silently revert an undervolt. A shift only reaches hardware on the
next GMU boot (a GPU idle/resume), not at write time.

`_unload` must never `await asyncio.to_thread(...)`. By the time Decky calls it
the executor is going away, so the first such await never returns and the
plugin is SIGKILLed five seconds later with the undervolt still running — with
no traceback, which makes it look like the logic is wrong rather than the
scheduling. A clean shutdown is visible as `Unloaded Adreno Undervolt` in the
loader log; if that line is missing, `_unload` hung.
