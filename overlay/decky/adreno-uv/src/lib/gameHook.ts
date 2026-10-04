import { toaster } from "@decky/api";
import { applyProfile, getState, tuneStop, tuneStatus } from "../backend";

/**
 * Apply the active profile when a game is launched - before it starts.
 *
 * Why GameActionStart and not AppLifetimeNotifications: the lifetime
 * notification fires once the game process is already running, so the voltage
 * change and the GPU cycle that commits it would land while Proton is
 * initialising and may already be using the GPU. GameActionStart fires when
 * Play is pressed, before the process exists - the GPU is only drawing the
 * Steam UI, and the profile is settled before the game touches it.
 *
 * The apply is idempotent: if the profile is already live in the vote table,
 * nothing is written and the GPU is not cycled. So this can run on every
 * launch without spending anything on launches after the first.
 *
 * This needs no cooperation from any other plugin - Steam publishes these
 * notifications to every caller.
 */
export function armGameLaunchApply(): () => void {
  const apps = (window as any).SteamClient?.Apps;
  if (!apps?.RegisterForGameActionStart) return () => {};


  const registration = apps.RegisterForGameActionStart(
    (_actionType: number, _appId: string, actionName: string) => {
      if (actionName !== "LaunchApp") return;

      // Tuning and gaming cannot coexist: the tuner owns the GPU, pins its
      // clock and deliberately drives it unstable. Rather than trust the
      // warning to be remembered an hour later, end the run.
      tuneStatus()
        .then((tune) => {
          if (tune.running) return tuneStop().then(() => {});
          return undefined;
        })
        .catch(() => {});

      getState()
        .then((state) => {
          if (!state.config.autoApply || state.config.active < 0) return undefined;
          const name = state.profileNames[state.config.active] || "profile";
          // The backend decides whether anything actually needs doing: an
          // already-live curve, including an all-zero one, must not cycle the
          // GPU while the game is starting up.
          // Always applied, never assumed: the cost of a redundant apply is a
          // brief GPU cycle, the cost of a wrong assumption is a game running
          // at the wrong voltage.
          // Short window on the launch path: the shift is written either way,
          // and if the GPU will not idle in 150ms it takes effect at the next
          // cycle rather than us holding it in a suspend/resume loop while the
          // game creates its context.
          return applyProfile(null, true, 0.15).then((result) => {
            // Nothing changed in hardware - no need to tell anyone.
            if (result.note === "already live, no GPU cycle needed") return;
            toaster.toast({
              title: "Adreno Undervolt",
              body: result.written
                ? `${name} applied`
                : `${name} not applied: ${result.error}`,
            });
          });
        })
        .catch(() => {});
    },
  );

  return () => {
    try {
      registration?.unregister?.();
    } catch {
      /* Steam is going away anyway */
    }
  };
}
