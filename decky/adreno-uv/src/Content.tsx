import {
  ButtonItem,
  ConfirmModal,
  DropdownItem,
  Navigation,
  PanelSection,
  PanelSectionRow,
  ToggleField,
  showModal,
} from "@decky/ui";
import { toaster } from "@decky/api";
import { useEffect, useState } from "react";
import { claim } from "./lib/singleton";
import { applyCurve, getState, restoreStock, saveConfig, tuneStatus, tuneStop } from "./backend";
import type { TuneStatus, UvConfig, UvState } from "./backend";
import { CopyModal, EDITOR_ROUTE } from "./pages/Editor";

/** Matches NO_PROFILE in uv.py: no profile selected, so stock voltage. */
const NO_PROFILE = -1;

export function Content() {
  const [state, setState] = useState<UvState | null>(null);
  const [status, setStatus] = useState("");
  const [tune, setTune] = useState<TuneStatus | null>(null);

  // Surfaced here as well as in the editor: a run lasts about an hour and
  // survives leaving the page, so the panel is where someone would notice it
  // before starting a game.
  useEffect(() => {
    let cancelled = false;
    const poll = () => {
      tuneStatus().then((s) => { if (!cancelled) setTune(s); }).catch(() => {});
    };
    poll();
    // Poll quickly only while a run is live; idle costs one file read every
    // ten seconds. Nothing here should be expensive enough to notice, but the
    // panel hanging for a minute is what happens when that assumption slips.
    const timer = window.setInterval(poll, 10000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, []);

  useEffect(() => {
    let cancelled = false;
    getState()
      .then((s) => !cancelled && setState(s))
      .catch(() => !cancelled && setStatus("could not talk to the backend"));
    return () => {
      cancelled = true;
    };
  }, []);

  if (!state) {
    return (
      <PanelSection>
        <PanelSectionRow>{status || "Loading…"}</PanelSectionRow>
      </PanelSection>
    );
  }

  if (!state.moduleLoaded) {
    return (
      <PanelSection>
        <PanelSectionRow>
          The a6xx_uv kernel module is not loaded. It is built against one exact
          kernel build, so a kernel update needs a rebuilt module.
        </PanelSectionRow>
        {/*
          The backend reinstalls and restarts adreno-uv.service on this screen,
          at most once a minute, so a rebuilt .ko dropped into bin/ recovers
          without restarting Decky. This line is the loader's own reason - after
          a kernel update it is usually the vermagic mismatch, which says
          "rebuild me" far more clearly than "not loaded" does.
        */}
        {state.moduleError ? (
          <PanelSectionRow>Loader says: {state.moduleError}</PanelSectionRow>
        ) : null}
        <PanelSectionRow>
          Retrying the service every minute while this is open.
        </PanelSectionRow>
      </PanelSection>
    );
  }

  // Loaded but inert. This is what an OS update leaves behind: Armada's stock
  // kernel and ours are both "7.2.0 SMP preempt mod_unload aarch64", so the
  // module loads into either and nothing in the loader objects. The module
  // itself catches it by validating the GMU's vote table, and reports it here.
  // Showing the normal panel would be worse than useless - every control would
  // look live and change nothing.
  if (state.kernelIncompatible) {
    return (
      <PanelSection>
        <PanelSectionRow>
          Incompatible kernel. The module loaded but was built against a
          different kernel than the one running, so it has stayed inert rather
          than write to a table it cannot trust.
        </PanelSectionRow>
        <PanelSectionRow>
          Rebuild a6xx_uv against the running kernel and reboot. No undervolt is
          applied, and nothing has been changed.
        </PanelSectionRow>
      </PanelSection>
    );
  }

  if (!state.shiftWritable) {
    return (
      <PanelSection>
        <PanelSectionRow>
          The module is loaded but its shift control is not writable. Run
          install.sh, or start adreno-uv.service.
        </PanelSectionRow>
      </PanelSection>
    );
  }

  const config = state.config;

  // The module snapshots the operating points from the GMU the first time it
  // hooks, so before the GPU has ever booted there is nothing to index by.
  if (state.freqs.length === 0) {
    return (
      <PanelSection>
        <PanelSectionRow>
          Waiting for the GPU to boot once — the operating points are read from
          the hardware, not assumed.
        </PanelSectionRow>
      </PanelSection>
    );
  }

  const active = config.active;
  const names = state.profileNames;
  const curve = active >= 0 ? config.profiles[active] || [] : state.freqs.map(() => 0);

  const push = (next: UvConfig) => {
    setState({ ...state, config: next });
    saveConfig(next)
      .then(setState)
      .catch(() => setStatus("save failed"));
  };

  // Warn once, at the first moment an undervolt is actually armed. A dialog on
  // every Apply would just train you to dismiss it.
  const selectProfile = (next: number) => {
    if (next < 0 || config.warned) {
      push({ ...config, active: next });
      return;
    }
    showModal(
      <ConfirmModal
        bDestructiveWarning
        strTitle="Use a GPU undervolt profile?"
        strOKButtonText="Continue"
        strCancelButtonText="Cancel"
        onOK={() => push({ ...config, active: next, warned: true })}
        strDescription={
          "Undervolting runs the GPU below the voltage it was characterised for. "
          + "If a profile is too aggressive the GPU freezes rather than slowing down: "
          + "the picture stops and the only way out is holding the power button, which "
          + "loses anything unsaved in a running game.\n\n"
          + "Nothing changes until you pick a profile and apply it. A reboot always "
          + "returns to stock voltage."
        }
      />,
    );
  };

  const applyNow = () => {
    setStatus("applying…");
    applyCurve(curve)
      .then((r) => {
        // Reported in a toast rather than a status line: the panel should not
        // carry a running description of GPU state.
        toaster.toast({
          title: "Adreno Undervolt",
          body: r.written
            ? `${active < 0 ? "Stock voltage" : names[active]} applied`
            : `Not applied: ${r.error}`,
        });
        setStatus("");
        // Re-read rather than assume: the button's job is done only when the
        // module reports the votes actually in the table.
        return getState().then(setState);
      })
      .catch(() => setStatus("apply failed"));
  };

  const openCopy = () => {
    if (active < 0) return;
    showModal(
      <CopyModal
        source={active}
        names={names}
        onCopy={(target) => {
          push({
            ...config,
            profiles: config.profiles.map((p, i) => (i === target ? [...curve] : p)),
          });
          setStatus(`Copied ${names[active]} to ${names[target]}.`);
        }}
      />,
    );
  };

  if (tune?.running) {
    return (
      <PanelSection>
        <PanelSectionRow>
          <div>
            <strong>● AUTO-TUNING</strong>
            <div>{tune.message || tune.phase}</div>
            <div>{`${(tune.steps || []).length} steps done`}</div>
          </div>
        </PanelSectionRow>
        <PanelSectionRow>Do not launch a game — starting one will stop the run.</PanelSectionRow>
        <PanelSectionRow>
          {/* The run outlives this panel, so there has to be a way back to the
              curve while it is still being discovered. */}
          <ButtonItem
            layout="below"
            onClick={() => {
              Navigation.CloseSideMenus();
              Navigation.Navigate(EDITOR_ROUTE);
            }}
          >
            View progress
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() => {
              tuneStop()
                .then(() => setStatus("Stopping after this step."))
                .catch(() => setStatus("Could not stop the tuner."));
            }}
          >
            Stop tuning
          </ButtonItem>
        </PanelSectionRow>
        {status ? <PanelSectionRow>{status}</PanelSectionRow> : null}
      </PanelSection>
    );
  }

  return (
    <PanelSection>
      <PanelSectionRow>
            <DropdownItem
              label="Profile"
              rgOptions={[{ data: NO_PROFILE, label: "None (stock)" }].concat(
                names.map((label, i) => ({ data: i, label })),
              )}
              selectedOption={active}
              onChange={(option) => {
                const next = Number(option.data);
                selectProfile(next);
                // Selecting None is a request for stock, so honour it at once
                // rather than waiting for an Apply nobody would think to press.
                if (next < 0) restoreStock().catch(() => {});
              }}
            />
          </PanelSectionRow>

          <PanelSectionRow>
            <ButtonItem
              layout="below"
              onClick={() => {
                Navigation.CloseSideMenus();
                Navigation.Navigate(EDITOR_ROUTE);
              }}
            >
              Configure
            </ButtonItem>
          </PanelSectionRow>


          <PanelSectionRow>
            <ToggleField
              label="Auto-apply on game start"
              description="Applies the selected profile when you launch a game. It stays applied after the game exits, until you change it or reboot. Never applied at boot."
              checked={config.autoApply}
              onChange={(autoApply) => push({ ...config, autoApply })}
            />
          </PanelSectionRow>

          <PanelSectionRow>
            <ButtonItem layout="below" onClick={applyNow}>
              {active < 0 ? "Apply (restore stock)" : `Apply ${names[active]}`}
            </ButtonItem>
          </PanelSectionRow>

      {status ? <PanelSectionRow>{status}</PanelSectionRow> : null}

      {state.armadaUvLive ? (
        <PanelSectionRow>
          Warning: Armada Control's undervolt interface is also present. Two
          things writing the same vote table will fight — turn one off.
        </PanelSectionRow>
      ) : null}
    </PanelSection>
  );
}
