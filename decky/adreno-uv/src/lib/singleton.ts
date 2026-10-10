/*
 * Guards against duplicate frontend instances.
 *
 * Decky reloads a plugin by injecting its bundle again into the Steam UI. If a
 * previous instance is not torn down first - a loader that was killed rather
 * than stopped, an onDismount that never ran - its timers and Steam event
 * registrations keep running inside steamwebhelper, and every redeploy adds
 * another set. Thirty deploys later that is thirty polling loops in the process
 * that renders the whole interface.
 *
 * Anything with a lifetime longer than a render goes through here, keyed by
 * name, so a new instance disposes the old one before taking over.
 */
const KEY = "__adrenoUvDisposers";

type Disposer = () => void;

function registry(): Map<string, Disposer> {
  const host = window as any;
  if (!host[KEY]) host[KEY] = new Map<string, Disposer>();
  return host[KEY];
}

/** Register a disposable under `name`, replacing and disposing any predecessor. */
export function claim(name: string, dispose: Disposer): Disposer {
  const map = registry();
  const previous = map.get(name);
  if (previous) {
    try {
      previous();
    } catch {
      /* a dead instance failing to clean up must not stop this one */
    }
  }
  map.set(name, dispose);
  return () => {
    if (map.get(name) === dispose) map.delete(name);
    dispose();
  };
}
