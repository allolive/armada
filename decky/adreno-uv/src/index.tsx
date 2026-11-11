import { definePlugin, routerHook } from "@decky/api";
import { Content } from "./Content";
import { armGameLaunchApply } from "./lib/gameHook";
import { claim } from "./lib/singleton";
import { EDITOR_ROUTE, EditorPage } from "./pages/Editor";

export default definePlugin(() => {
  // Runs at plugin load whether or not the panel is ever opened. Nothing is
  // applied unless the user asks, or a game is launched with the setting on.
  const disarm = claim("gameHook", armGameLaunchApply());

  // Full-screen editor: a 14-point curve needs more width than the quick
  // access panel has, and it must stay reachable after the panel closes.
  const unroute = claim("editorRoute", () => routerHook.removeRoute(EDITOR_ROUTE));
  routerHook.addRoute(EDITOR_ROUTE, EditorPage, { exact: true });

  return {
    name: "Adreno Undervolt",
    content: <Content />,
    onDismount() {
      disarm();
      unroute();
    },
    icon: (
      <svg
        xmlns="http://www.w3.org/2000/svg"
        width="24"
        height="24"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z" />
      </svg>
    ),
    // The game hook and the route live in this closure, not in the panel, so
    // nothing needs the content mounted while the menu is closed - and every
    // mounted component is work for steamwebhelper.
    alwaysRender: false,
  };
});
