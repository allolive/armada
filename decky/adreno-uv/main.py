import asyncio

from adreno_uv import uv


class Plugin:
    # Whether the undervolt is meant to outlive the plugin. Kept in memory
    # because _unload cannot afford to read it: see the comment there.
    persist = False

    # Blocking work goes to a thread so a slow sysfs read cannot stall Decky's
    # asyncio loop.
    async def _main(self):
        # Decky starts plugins once the session is up, so this is the "startup
        # is done" point - late enough that the GPU driver is present.
        # A reload means the plugin - and with it bin/autotune.py and
        # bin/gpustress - may have just been replaced underneath a run in
        # progress. Its results would be a mixture of two versions, so end it
        # cleanly rather than let it finish and be believed.
        if await asyncio.to_thread(uv.tune_running):
            print("adreno-uv: plugin reloaded - stopping the tuning run", flush=True)
            await asyncio.to_thread(uv.tune_halt)

        # Install and start our own unit. Decky runs this backend as root but
        # without CAP_SYS_MODULE, so it can drive systemd yet cannot insmod -
        # the unit exists to do that one thing, and installing it from here
        # means the plugin sets itself up with no manual step.
        ok, err = await asyncio.to_thread(uv.ensure_service)
        if not ok:
            print(f"adreno-uv: service not ready: {err}", flush=True)
        # ensure_loaded now goes through the unit first and only falls back to
        # insmod, so a missing module is repaired here rather than reported.
        ok, err = await asyncio.to_thread(uv.ensure_loaded)
        if not ok:
            # Not fatal to the plugin any more: get_state retries the service
            # periodically, so dropping a rebuilt .ko into bin/ recovers without
            # a Decky restart. The panel shows this reason meanwhile.
            print(f"adreno-uv: module not loaded: {err}", flush=True)
            return

        # A freshly loaded module has not seen a GMU boot yet, so it knows no
        # operating points and the UI has nothing to draw. Force one cycle.
        ok, note = await asyncio.to_thread(uv.bootstrap)
        print(f"adreno-uv: bootstrap: {ok} {note} "
              f"({len(await asyncio.to_thread(uv.gpu_freqs))} OPPs)", flush=True)

        config = await asyncio.to_thread(uv.load_config)
        Plugin.persist = config["autoApply"] and config["active"] >= 0
        # Nothing is applied here, ever. The undervolt is for games: the
        # frontend applies it when a game is launched, so the desktop runs at
        # stock and the GPU cycle that commits the votes happens before the
        # game starts rather than mid-session.
        print(f"adreno-uv: auto-apply on game start armed: {Plugin.persist}", flush=True)

    async def _unload(self):
        # Only undo the undervolt if it was not meant to outlive the UI.
        # Zeroing unconditionally would fight auto-apply: every Decky restart
        # would silently drop a reduction the user asked to persist.
        #
        # Nothing here may await asyncio.to_thread: by the time Decky calls
        # _unload the executor is going away, so the first such await never
        # returns and the plugin is SIGKILLed five seconds later with the
        # undervolt still running. Read nothing, and write straight through.
        #
        # Take the unit with us. Installing it on load but leaving it behind on
        # unload meant a DISABLED plugin still loaded a kernel module at every
        # boot - the service outlived the thing that owns it. Removing it here
        # makes enable/disable symmetric.
        #
        # Safe to do before the returns below: the unit is Type=oneshot with no
        # ExecStop, so stopping it does NOT rmmod. A live undervolt and a
        # running tuner both survive; only the next boot changes.
        # Called synchronously - see the to_thread rule above.
        try:
            uv.remove_service()
        except Exception as exc:                     # never block the unload
            print(f"adreno-uv: _unload could not remove service: {exc}", flush=True)

        if Plugin.persist:
            print("adreno-uv: _unload leaving undervolt in place", flush=True)
            return
        # Never yank the shift out from under a tuning run: a Decky restart
        # mid-measurement would zero the very setting being tested and record
        # it as stable.
        if uv.tune_running():
            print("adreno-uv: _unload leaving the tuner alone", flush=True)
            return
        print(f"adreno-uv: _unload restored stock: {uv.write_shift([])}", flush=True)

    async def _uninstall(self):
        # Decky calls this when the plugin is removed. Take the unit with us so
        # nothing of ours is left in /etc.
        print("adreno-uv: uninstalling - restoring stock and removing service", flush=True)
        uv.write_shift([])
        await asyncio.to_thread(uv.remove_service)

    async def get_state(self):
        # The module can go missing while the plugin is up: an OS update
        # replaces the kernel, a rebuild rmmods it, or _unload removed the unit
        # and Decky never came back through _main. Try to put the service back
        # instead of sitting on the not-loaded screen until someone reboots -
        # rate-limited inside uv, because this is a polling path and spawning
        # systemctl on one is what made the menu appear to hang.
        if not await asyncio.to_thread(uv.module_loaded):
            ok, err = await asyncio.to_thread(uv.repair_service)
            print(f"adreno-uv: module missing, service repair: {ok} {err}", flush=True)

        # Recover if the module was reloaded while the panel was closed: without
        # this the UI stays on "waiting for the GPU" until something else
        # happens to idle it, which under gamescope may be never.
        if not await asyncio.to_thread(uv.gpu_freqs):
            await asyncio.to_thread(uv.bootstrap)
        return await asyncio.to_thread(uv.state)

    async def save_config(self, config):
        saved = await asyncio.to_thread(uv.save_config, config)
        Plugin.persist = saved["autoApply"] and saved["active"] >= 0
        return await asyncio.to_thread(uv.state)

    async def apply_profile(self, index=None, force=False, timeout=0.5):
        """Apply a stored profile. index=None means the active one.

        The master switch outranks the caller: without that check a stale UI
        could push a curve the switch says must not run.
        """
        config = await asyncio.to_thread(uv.load_config)
        if index is None:
            index = config["active"]
        curve = await asyncio.to_thread(uv.curve_for, config, index)
        # Always write. The shift is what the module applies on every future
        # GMU boot, so it must be correct regardless.
        ok, err = await asyncio.to_thread(uv.write_shift, curve)
        note = ""
        if ok and force:
            # Only cycle the GPU if the hardware does not already match. The
            # launch hook fires on Steam actions that are not real launches -
            # six times in one session with no game running - and a forced
            # suspend/resume while gamescope is drawing stalls the UI.
            if await asyncio.to_thread(uv.curve_is_live, curve):
                note = "already live, no GPU cycle needed"
            else:
                _, note = await asyncio.to_thread(uv.apply_now, timeout)
        # Timestamped deliberately: if a game dies near a launch, this line is
        # what says whether we touched the GPU at that moment or not.
        print(f"adreno-uv: apply profile {index}: written={ok} {err}{note} "
              f"(force={force}, window={timeout}s)", flush=True)
        return {"written": ok, "error": err, "curve": curve, "note": note,
                "skipped": False,
                "profile": index if isinstance(index, int) and index >= 0 else -1}

    async def apply_curve(self, curve):
        """The Apply button. The editor hands the curve over directly instead of
        having it read back from disk, so a curve that hangs the GPU is never
        persisted and the reboot that recovers the device also discards it."""
        ok, err = await asyncio.to_thread(uv.write_shift, curve)
        if not ok:
            return {"written": False, "error": err, "curve": curve, "note": ""}
        # Only the explicit button forces a reset. Apply-on-startup does not:
        # the GPU boots its GMU several times while the system comes up, so the
        # votes land there for free.
        _, note = await asyncio.to_thread(uv.apply_now)
        return {"written": True, "error": "", "curve": curve, "note": note}

    async def refresh(self):
        """Reboot the GMU so whatever shift is set takes effect now."""
        ok, note = await asyncio.to_thread(uv.apply_now)
        return {"written": ok, "error": "" if ok else note, "note": note}

    async def tune_start(self, step_seconds=60, verify_seconds=300, resume=True):
        """Sweep for limits: push each frequency until it breaks."""
        ok, err = await asyncio.to_thread(uv.tune_start, step_seconds,
                                          verify_seconds, resume, "sweep")
        print(f"adreno-uv: autotune start: {ok} {err} (resume={resume})", flush=True)
        return {"started": ok, "error": err}

    async def soak_start(self, step_seconds=60):
        """Try to break the profile in use. Never deepens anything.

        A different question from tuning: the sweep asks "how far can this go",
        the soak asks "does what I am actually running survive an evening".
        """
        ok, err = await asyncio.to_thread(uv.tune_start, step_seconds, 0, False, "soak")
        print(f"adreno-uv: soak start: {ok} {err}", flush=True)
        return {"started": ok, "error": err}

    async def tune_stop(self):
        ok, err = await asyncio.to_thread(uv.tune_stop)
        return {"stopped": ok, "error": err}

    async def tune_status(self):
        return await asyncio.to_thread(uv.tune_status)

    # Curve names, in the order a user would try them. "normal" carries a step
    # of margin wherever the sweep found an edge; "aggressive" is the exact last
    # stable point as measured. "measured" is the raw curve with no crash margin
    # at all and is not offered in the UI.
    TUNED_CURVES = {"normal": "recommended", "aggressive": "aggressive", "measured": "curve"}

    async def tune_accept(self, useRecommended=True, mode=None):
        """Save a tuned curve into the active profile.

        mode picks which curve: "normal" (default) or "aggressive". The older
        useRecommended flag still works for callers that predate the choice -
        False meant the raw measured curve.
        """
        status = await asyncio.to_thread(uv.tune_status)
        if mode is None:
            mode = "normal" if useRecommended else "measured"
        key = Plugin.TUNED_CURVES.get(mode, "recommended")
        curve = status.get(key)
        if not curve:
            # A journal written before this existed has no "aggressive" field,
            # but curve IS the aggressive answer by definition - the deepest
            # shift that passed. Falling back to "recommended" here would
            # silently hand back the conservative curve under the other name.
            fallback = "curve" if mode == "aggressive" else "recommended"
            curve = status.get(fallback) or status.get("curve") or []
        config = await asyncio.to_thread(uv.load_config)
        index = config["active"] if config["active"] >= 0 else 0
        config["profiles"][index] = list(curve)
        config["active"] = index
        saved = await asyncio.to_thread(uv.save_config, config)
        print(f"adreno-uv: saved {mode} curve into profile {index}", flush=True)
        return {"saved": True, "profile": index, "mode": mode,
                "curve": saved["profiles"][index]}

    async def restore_stock(self):
        ok, err = await asyncio.to_thread(uv.write_shift, [])
        return {"written": ok, "error": err}
