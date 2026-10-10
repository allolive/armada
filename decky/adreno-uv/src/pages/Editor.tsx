import {
  ConfirmModal,
  DialogButton,
  Dropdown,
  Focusable,
  GamepadButton,
  showModal,
} from "@decky/ui";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { GamepadEvent } from "@decky/ui";
import { getState, saveConfig, soakStart, tuneAccept, tuneStart, tuneStatus, tuneStop } from "../backend";
import type { TuneStatus, TunedCurve, UvConfig, UvState } from "../backend";
import { claim } from "../lib/singleton";
import { editorStyles } from "./editorStyles";

/*
 * Full-screen curve editor, ported from armada-control's undervolt page.
 *
 * The unit differs from that original, which edited a millivolt-ish delta in
 * [min, 0]. Here the stored value is an OPP shift in [0, maxShift] - a shift of
 * N means "use the voltage corner belonging to the operating point N steps
 * lower" - but the plot does not show shifts. It shows the resulting
 * voltage/frequency curve: x is real frequency, y is the real RPMh corner, and
 * stock is drawn underneath as a dashed reference. The gap between the two
 * lines is the undervolt.
 */

export const EDITOR_ROUTE = "/adreno-uv/editor";

/* Fallback size only. The plot is drawn at the element's real pixel size so a
 * unit is a pixel: with a fixed viewBox and preserveAspectRatio="none" the SVG
 * stretches to fit, which turns the dots into ovals and the labels into tall
 * thin text whenever the box is not exactly this shape. */
const PLOT_W0 = 1000;
const PLOT_H0 = 300;
const PAD_L = 54;
const PAD_R = 24;
const PAD_T = 16;
const PAD_B = 12;

const mhz = (hz: number) => Math.round(hz / 1000000);

/**
 * The RPMh voltage corner, packed in the top 16 bits of a vote word.
 *
 * This is what makes the plot a real voltage/frequency curve rather than a
 * picture of the shift numbers: the y axis is the corner the GPU actually
 * votes for. The rungs are unevenly spaced (…128, 144, 192, 224, 256…), and
 * plotting them linearly shows that honestly - the step from shift 1 to 2 is
 * visibly a bigger drop than 2 to 3.
 */
const levelOf = (vote: string) => (Number(vote) >>> 16) || 0;

/**
 * Which operating point's corner a shift borrows. Mirrors the remap in
 * a6xx_uv.c, including the floor at the lowest real OPP, so the curve on
 * screen is the curve the module will produce.
 */
function sourceIndex(index: number, shift: number, freqs: number[]) {
  let src = index - shift;
  if (src < 1) src = 1;
  while (src < index && !freqs[src]) src += 1;
  if (src > index) src = index;
  return src;
}

/**
 * Axis mapping for the voltage/frequency plot.
 *
 * BOTH AXES ARE LOGARITHMIC. Linearly, the curve is a wall: corners run
 * 50…448 with most of the range spent above 128, and frequencies bunch at the
 * top (900, 967, 1050, 1100) while the interesting low end is squashed into the
 * first inch. Log on y flattens the climb so the shape is readable; log on x
 * gives the low frequencies room to be aimed at.
 *
 * The cost is that vertical distance no longer equals volts, so a step near the
 * bottom looks bigger than an equal-sized step at the top. The numbers on the
 * axis and in the readout stay the real corner values, which is where anyone
 * reads an actual amount from.
 */
const lg = (v: number) => Math.log(Math.max(1, v));
function plotGeometry(state: UvState, points: number[], W: number, H: number) {
  const levels = state.stockVotes.split(",").map(levelOf);
  const freqs = state.freqs;
  const loF = freqs[points[0]] || 0;
  const hiF = freqs[points[points.length - 1]] || 1;
  const shown = points.map((i) => levels[i] || 0);
  const loL = Math.min(...shown);
  const hiL = Math.max(...shown);
  /*
   * Both axes logarithmic. Two alternatives were tried on 2026-08-23 and both
   * were worse to read on the device: a linear voltage axis, and a y axis
   * warped so the stock line came out as a straight diagonal. Neither is
   * hiding a bug - they were simply harder to use.
   */
  return {
    levels,
    loL,
    hiL,
    xFor: (hz: number) =>
      PAD_L + ((lg(hz) - lg(loF)) / (lg(hiF) - lg(loF) || 1)) * (W - PAD_L - PAD_R),
    yFor: (level: number) =>
      PAD_T + (1 - (lg(level) - lg(loL)) / (lg(hiL) - lg(loL) || 1)) * (H - PAD_T - PAD_B),
  };
}

/** Destination picker for a copy. Named at both ends so there is no doubt
 *  which profile is the source and which one is about to be overwritten. */
export function CopyModal({ source, names, onCopy, closeModal }: {
  source: number;
  names: string[];
  onCopy: (target: number) => void;
  closeModal?: () => void;
}) {
  const targets = names.map((_, i) => i).filter((i) => i !== source);
  const [target, setTarget] = useState(targets[0]);
  const options = targets.map((i) => ({ data: String(i), label: names[i] }));
  return (
    <ConfirmModal
      strTitle={`Copy ${names[source]}`}
      strDescription={`${names[target]} will be overwritten with the curve from ${names[source]}.`}
      strOKButtonText={`Copy to ${names[target]}`}
      strCancelButtonText="Cancel"
      closeModal={closeModal}
      onOK={() => onCopy(target)}
    >
      <div className="adreno-uv-modal-field">
        <div className="adreno-uv-label">Copy to</div>
        <Dropdown
          selectedOption={String(target)}
          rgOptions={options}
          onChange={(option) => setTarget(Number(option.data) || 0)}
        />
      </div>
    </ConfirmModal>
  );
}

/** The warnings are the same whichever way the run is started, so they live in
 *  one place rather than being re-stated per entry point. Kept terse: this is
 *  read on a handheld, and a wall of text gets dismissed unread, which defeats
 *  the point of warning at all. */
function tuneDescription(resume: boolean) {
  return "DO NOT RUN A GAME while tuning.\n\n"
    + (resume
      ? "Continues the last curve: proven depths are skipped, only deeper ones "
        + "tried. Starts over by itself if the operating points changed.\n\n"
      : "Sweeps every frequency from stock, re-testing what already works. Use "
        + "after a kernel or module change.\n\n")
    + "It pushes each frequency until it breaks, so the GPU will hang - that is "
    + "how the limit is found. Some hangs need the power button, losing unsaved "
    + "work.\n\n"
    + "Every step is journalled before it runs, so a setting that kills the "
    + "device is never retried. A reboot comes back at stock.\n\n"
    + "Stop any time; the curve found so far is kept.";
}

const TUNE_MODES = [
  { data: "resume", label: "Continue from where we got to" },
  { data: "fresh",  label: "Start from scratch" },
];

export function TuneModal({ resumable, onStart, closeModal }: {
  resumable: boolean;
  onStart: (resume: boolean) => void;
  closeModal?: () => void;
}) {
  // Without a previous curve there is nothing to continue from, so that option
  // would silently behave as "from scratch" - offer it only when it means
  // something.
  const options = resumable ? TUNE_MODES : TUNE_MODES.filter((o) => o.data === "fresh");
  const [mode, setMode] = useState(options[0].data);
  const resume = mode === "resume";
  return (
    <ConfirmModal
      strTitle="Auto-tune"
      strDescription={tuneDescription(resume)}
      strOKButtonText="Start tuning"
      strCancelButtonText="Cancel"
      closeModal={closeModal}
      bDestructiveWarning
      onOK={() => onStart(resume)}
    >
      <div className="adreno-uv-modal-field">
        <div className="adreno-uv-label">How</div>
        <Dropdown
          selectedOption={mode}
          rgOptions={options}
          onChange={(option) => setMode(String(option.data))}
        />
      </div>
    </ConfirmModal>
  );
}

const CURVE_MODES = [
  { data: "normal",     label: "Normal - one step of margin" },
  { data: "aggressive", label: "Aggressive - the last setting that passed" },
];

export function UseCurveModal({ profileName, onUse, closeModal }: {
  profileName: string;
  onUse: (mode: TunedCurve) => void;
  closeModal?: () => void;
}) {
  const [mode, setMode] = useState<TunedCurve>("normal");
  return (
    <ConfirmModal
      strTitle="Use tuned curve"
      strDescription={
        `The tuned curve will be written into ${profileName}.\n\n`
        + (mode === "normal"
          ? "Normal backs off one step wherever the sweep actually found an edge. "
            + "Points that never failed - because they hit the shift ceiling or the "
            + "run was stopped - keep their full value, since backing those off "
            + "would discard a result that was never disproved."
          : "Aggressive is the exact last setting that passed, with no margin "
            + "anywhere. It is a real measurement, but each point was proven by a "
            + "single stress run rather than by hours of play. Soak the curve if "
            + "you want more than one run behind it.")
      }
      strOKButtonText={mode === "normal" ? "Use normal curve" : "Use aggressive curve"}
      strCancelButtonText="Cancel"
      closeModal={closeModal}
      onOK={() => onUse(mode)}
    >
      <div className="adreno-uv-modal-field">
        <div className="adreno-uv-label">Which curve</div>
        <Dropdown
          selectedOption={mode}
          rgOptions={CURVE_MODES}
          onChange={(option) => setMode(option.data as TunedCurve)}
        />
      </div>
    </ConfirmModal>
  );
}

/** Soaking asks a different question from tuning: not "how far can this go"
 *  but "does what I actually run survive an evening". It never deepens
 *  anything, so it cannot find a new limit - it can only take one away. */
export function SoakModal({ profileName, onStart, closeModal }: {
  profileName: string;
  onStart: () => void;
  closeModal?: () => void;
}) {
  return (
    <ConfirmModal
      strTitle={`Soak ${profileName}?`}
      strDescription={
        "DO NOT RUN A GAME while this is running.\n\n"
        + "It stress-tests the SAVED curve for this profile, point by point, "
        + "round after round, trying to break it. Nothing is pushed deeper than "
        + "you have set - anything that fails a setting it previously passed is "
        + "backed off one step and recorded.\n\n"
        + "Save your edits first: it reads the profile from disk, not the curve "
        + "on screen.\n\n"
        + "It will not stop by itself. Stop it from here when you have had "
        + "enough; every change is journalled as it happens."
      }
      strOKButtonText="Start soaking"
      strCancelButtonText="Cancel"
      closeModal={closeModal}
      bDestructiveWarning
      onOK={onStart}
    />
  );
}

export function EditorPage() {
  const [state, setState] = useState<UvState | null>(null);
  /** Real shift indices that are editable: index 0 is the rail-off entry and
   *  has no lower corner to borrow, so it never appears on the plot. */
  const [points, setPoints] = useState<number[]>([]);
  const [profiles, setProfiles] = useState<number[][]>([]);
  // Which profile is being edited. Distinct from the panel's selection, which
  // may be None - opening the editor must not silently select a profile.
  const [active, setActive] = useState(0);
  const [selection, setSelection] = useState(-1);
  // null until a point is picked, so the readout can stay hidden.
  const [selected, setSelected] = useState<number | null>(null);
  const [status, setStatus] = useState("Loading…");
  // Two stages: the plot is an ordinary focus stop until it is activated, so
  // passing over it on the way somewhere else does not capture the D-pad.
  const [editing, setEditing] = useState(false);
  // The drawing surface in real pixels, so nothing is scaled unevenly.
  const [box, setBox] = useState({ w: PLOT_W0, h: PLOT_H0 });
  // Pointer drags edit without entering gamepad edit mode, so they need to
  // light the readout too.
  const [touching, setTouching] = useState(false);
  const [tune, setTune] = useState<TuneStatus | null>(null);
  const tuningRef = useRef(false);
  const intervalRef = useRef(10000);

  // While a run is in progress the page is a read-only display of it: the
  // tuner owns the shift, and an edit from here would silently invalidate
  // whatever step is being measured.
  const tuning = !!tune?.running;
  tuningRef.current = tuning;
  // There is something to continue from only if the journal holds a curve that
  // actually reached somewhere. An all-zero curve is indistinguishable from a
  // fresh start, so offering to "continue" from it would be a lie.
  const resumable = !tuning && (tune?.curve || []).some((v) => v);

  const editingRef = useRef(false);
  editingRef.current = editing;
  const selectedRef = useRef<number | null>(null);
  selectedRef.current = selected;
  const latest = useRef({ profiles, active, selection, state, tuning: false });
  latest.current = { profiles, active, selection, state, tuning };
  // Nothing is written to disk while the page is open. A curve that hangs the
  // GPU can only be recovered by a reboot, and a reboot must not bring it back.
  const dirty = useRef(false);

  useEffect(() => {
    let cancelled = false;
    getState()
      .then((s) => {
        if (cancelled) return;
        setState(s);
        setPoints(s.freqs.map((hz, i) => (hz && i > 0 ? i : -1)).filter((i) => i >= 0));
        setProfiles(s.config.profiles.map((curve) => s.freqs.map((_, i) => Number(curve[i]) || 0)));
        setActive(s.config.active >= 0 ? s.config.active : 0);
        setSelection(s.config.active);
        setStatus("");
      })
      .catch((error) => {
        if (!cancelled) setStatus(`Could not read state: ${String(error)}`);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const save = useCallback(async () => {
    if (!dirty.current) return;
    dirty.current = false;
    const current = latest.current;
    // Re-read the switches rather than trusting the copy this page loaded:
    // they are owned by the quick access panel, which may have moved meanwhile.
    let warned = !!current.state?.config.warned;
    let autoApply = !!current.state?.config.autoApply;
    try {
      const fresh = await getState();
      warned = fresh.config.warned;
      autoApply = fresh.config.autoApply;
    } catch {
      // Keep the values we have; losing the edits is the worse outcome.
    }
    const payload: UvConfig = {
      warned,
      autoApply,
      // The panel's selection, not the profile being edited.
      active: current.selection,
      profiles: current.profiles,
    };
    try {
      await saveConfig(payload);
    } catch (error) {
      dirty.current = true;
      setStatus(`Save failed: ${String(error)}`);
    }
  }, []);

  // Leaving the page is the commit point, however it happens.
  useEffect(() => () => { void save(); }, [save]);

  // Poll the tuner's journal. It runs in its own systemd unit, so this is the
  // only way to see it - and it keeps working if the run took the session down
  // and Decky restarted underneath us.
  useEffect(() => {
    let cancelled = false;
    const poll = () => {
      tuneStatus()
        .then((status) => { if (!cancelled) setTune(status); })
        .catch(() => {});
    };
    poll();
    // Poll quickly only while a run is live; idle costs one file read every
    // ten seconds. Nothing here should be expensive enough to notice, but the
    // panel hanging for a minute is what happens when that assumption slips.
    const timer = window.setInterval(poll, 10000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, []);

  const mutate = useCallback((nextProfiles: number[][], nextActive: number, note: string) => {
    dirty.current = true;
    setProfiles(nextProfiles);
    setActive(nextActive);
    setStatus(note);
  }, []);

  const maxShift = state?.maxShift || 4;

  const setShift = useCallback((realIndex: number, value: number) => {
    if (latest.current.tuning) return;
    const current = latest.current;
    // Cap at the deepest shift that still changes the corner. The module floors
    // the source at the lowest real OPP, so at index 2 shifts 1..4 all land on
    // index 1 - storing 4 there means four presses of Up before anything moves.
    const useful = Math.max(0, Math.min(maxShift, realIndex - 1));
    const clamped = Math.max(0, Math.min(useful, value));
    mutate(
      current.profiles.map((curve, i) => (
        i === current.active ? curve.map((entry, j) => (j === realIndex ? clamped : entry)) : curve
      )),
      current.active,
      "Edited. Not saved until you leave the page.",
    );
  }, [mutate, maxShift]);

  // One path for both buttons, so they cannot drift apart in what they save or
  // how they report failure. useRecommended is passed for the benefit of any
  // older backend that predates the mode argument.
  const saveTuned = (mode: TunedCurve) => {
    tuneAccept(mode === "normal", mode)
      .then((r) => {
        setStatus(`Saved the ${mode} curve into ${names[r.profile]}.`);
        return getState();
      })
      .then((fresh) => {
        setState(fresh);
        setProfiles(fresh.config.profiles.map((c) => fresh.freqs.map((_, i) => Number(c[i]) || 0)));
      })
      .catch(() => setStatus(`Could not save the ${mode} curve.`));
  };

  const launchTune = (stepSeconds: number, resume: boolean) => {
    tuneStart(stepSeconds, 300, resume)
      .then((r) => setStatus(r.started ? "Tuning started." : `Could not start: ${r.error}`))
      .catch(() => setStatus("Could not start the tuner."));
  };

  const openTune = () => {
    showModal(
      <TuneModal resumable={resumable} onStart={(resume) => launchTune(60, resume)} />,
    );
  };

  const openSoak = () => {
    showModal(
      <SoakModal
        profileName={names[active] || "this profile"}
        onStart={() => {
          soakStart(60)
            .then((r) => setStatus(
              r.started ? "Soaking the saved profile until you stop it."
                        : `Could not start: ${r.error}`))
            .catch(() => setStatus("Could not start the soak."));
        }}
      />,
    );
  };

  const openUseCurve = () => {
    showModal(
      <UseCurveModal
        profileName={names[active] || "this profile"}
        onUse={(mode) => saveTuned(mode)}
      />,
    );
  };

  const openCopy = () => {
    showModal(
      <CopyModal
        source={latest.current.active}
        names={state?.profileNames || []}
        onCopy={(target) => {
          const current = latest.current;
          mutate(
            current.profiles.map((curve, i) => (
              i === target ? [...(current.profiles[current.active] || [])] : curve
            )),
            current.active,
            `Copied ${state?.profileNames[current.active]} to ${state?.profileNames[target]}.`,
          );
        }}
      />,
    );
  };

  // Only while editing does the plot take the D-pad; otherwise every direction
  // falls through to Steam so the plot behaves like any other focus stop.
  const onPlotButton = (evt: GamepadEvent) => {
    if (!editingRef.current) return;
    const button = evt.detail.button;
    const isHorizontal = button === GamepadButton.DIR_LEFT || button === GamepadButton.DIR_RIGHT;
    const isVertical = button === GamepadButton.DIR_UP || button === GamepadButton.DIR_DOWN;
    if (!isHorizontal && !isVertical) return;
    if (isHorizontal) {
      const step = button === GamepadButton.DIR_LEFT ? -1 : 1;
      setSelected((current) => Math.max(0, Math.min(points.length - 1, (current ?? 0) + step)));
    } else {
      // Up is toward stock, matching the plot: 0 sits at the top.
      const step = button === GamepadButton.DIR_UP ? -1 : 1;
      const current = latest.current;
      const real = points[selectedRef.current ?? 0];
      setShift(real, (Number(current.profiles[current.active]?.[real]) || 0) + step);
    }
    evt.stopPropagation();
    evt.preventDefault();
  };

  // Pointer drag for desktop mode and touch. Maps y back to a shift and the
  // nearest column to an operating point.
  const svgRef = useRef<SVGSVGElement | null>(null);
  // clientWidth/Height, not getBoundingClientRect: Steam scales focused
  // elements, and a rect that grows on focus would change the viewBox and
  // redraw the curve at a different size the moment the graph is highlighted.
  // Layout size ignores transforms, so the drawing stays put.
  useLayoutEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const measure = () => {
      const w = svg.clientWidth;
      const h = svg.clientHeight;
      if (w > 0 && h > 0) {
        setBox((current) => (current.w === w && current.h === h ? current : { w, h }));
      }
    };
    // Measure now rather than waiting for a resize: until the viewBox matches
    // the element the drawing is letterboxed, which is why the graph looked
    // smaller until something touched it.
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(svg);
    return () => observer.disconnect();
  }, [points.length]);
  const dragging = useRef(false);
  const pointerEdit = (clientX: number, clientY: number) => {
    const svg = svgRef.current;
    const s = latest.current.state;
    if (!svg || !points.length || !s) return;
    const rect = svg.getBoundingClientRect();
    // Scale from screen pixels back into viewBox units, so a focused (and
    // possibly scaled) plot still hit-tests where you actually touched.
    const x = ((clientX - rect.left) / (rect.width || 1)) * box.w;
    const y = ((clientY - rect.top) / (rect.height || 1)) * box.h;

    const geom = plotGeometry(s, points, box.w, box.h);
    let nearest = points[0];
    for (const real of points) {
      if (Math.abs(geom.xFor(s.freqs[real]) - x) < Math.abs(geom.xFor(s.freqs[nearest]) - x)) {
        nearest = real;
      }
    }
    // Snap to whichever shift lands closest to the pointer. Only real corners
    // exist, so dragging between two of them has to resolve to one.
    let best = 0;
    let bestDist = Infinity;
    const useful = Math.max(0, Math.min(maxShift, nearest - 1));
    for (let candidate = 0; candidate <= useful; candidate += 1) {
      const level = geom.levels[sourceIndex(nearest, candidate, s.freqs)] || 0;
      const dist = Math.abs(geom.yFor(level) - y);
      if (dist < bestDist) {
        bestDist = dist;
        best = candidate;
      }
    }
    setSelected(points.indexOf(nearest));
    setShift(nearest, best);
  };

  if (!state || !points.length) {
    return (
      <div className="adreno-uv-page">
        <style>{editorStyles}</style>
        <div className="adreno-uv-empty">{status}</div>
      </div>
    );
  }

  // While tuning, the curve shown is the tuner's measured one, so you watch it
  // being discovered rather than looking at a stale profile.
  const curve = tuning && tune?.curve ? tune.curve : profiles[active] || [];
  const names = state.profileNames;
  const profileOptions = names.map((label, i) => ({ data: String(i), label }));
  const geom = plotGeometry(state, points, box.w, box.h);
  const levelAt = (real: number) =>
    geom.levels[sourceIndex(real, Number(curve[real]) || 0, state.freqs)] || 0;
  // Where a given shift would sit for a point. levelAt only answers that for
  // the value currently in the curve; the failure and crash marks need it for
  // a DIFFERENT depth - the one that failed, which is a step past the curve.
  const yForShift = (real: number, shift: number) =>
    geom.yFor(geom.levels[sourceIndex(real, shift, state.freqs)] || 0);
  // Both tuned curves, drawn on EVERY profile rather than only the one that
  // was just tuned: the measurement is a property of the hardware, not of the
  // profile, and it is the thing you compare a hand-edited curve against.
  const tunedLine = (c?: number[]) =>
    c && c.length
      ? points.map((real) => `${geom.xFor(state.freqs[real])},${yForShift(real, Number(c[real]) || 0)}`)
      : null;
  // A journal written before "aggressive" existed still has curve, which IS
  // the aggressive answer - the deepest shift that passed. Without this the
  // red line and its button simply never appear on an older sweep.
  const aggressiveCurve = (tune?.aggressive && tune.aggressive.length)
    ? tune.aggressive
    : tune?.curve;
  const normalLine = tunedLine(tune?.recommended);
  const aggressiveLine = tunedLine(aggressiveCurve);
  /** What the tuner actually learned about one frequency, in a line. The plot
   *  marks show WHERE the edge is; this says what happened there, which is the
   *  difference between "never pushed this far" and "pushed here and it broke". */
  const testedSummary = (real: number) => {
    if (!tune) return "";
    const key = String(real);
    const mine = (tune.steps || []).filter((step) => step.index === real);
    const passes = mine.filter((step) => step.result === "pass").map((step) => step.shift);
    const deepest = passes.length ? Math.max(...passes) : 0;
    const failed = (tune.failedAt || {})[key];
    const crashed = (tune.crashedAt || {})[key];
    const soaked = (tune.soakPasses || {})[key];
    if (!mine.length && failed === undefined && crashed === undefined) return "never tested";
    const bits: string[] = [];
    bits.push(deepest ? `passed to −${deepest}` : "no pass recorded");
    if (typeof crashed === "number") bits.push(`crashed at −${crashed} · blocked`);
    else if (typeof failed === "number") bits.push(`unstable at −${failed}`);
    else bits.push("no edge found");
    if (soaked) bits.push(`${soaked} soak ${soaked === 1 ? "pass" : "passes"}`);
    return bits.join("  ·  ");
  };
  const failedAt = tune?.failedAt || {};
  const crashedAt = tune?.crashedAt || {};
  const anyMarks = Object.keys(failedAt).length > 0 || Object.keys(crashedAt).length > 0;

  // The edited curve, and stock underneath it for reference - the gap between
  // the two lines is the undervolt.
  const plotPoints = points.map((real) =>
    `${geom.xFor(state.freqs[real])},${geom.yFor(levelAt(real))}`);
  const stockPoints = points.map((real) =>
    `${geom.xFor(state.freqs[real])},${geom.yFor(geom.levels[real] || 0)}`);
  // Label the corners actually in use rather than an arbitrary linear scale.
  const gridRows = Array.from(new Set(points.map((real) => geom.levels[real] || 0)))
    .sort((a, b) => a - b)
    .filter((_, i, all) => all.length <= 6 || i % 2 === 0 || i === all.length - 1);
  const selectedReal = selected !== null ? points[selected] : -1;

  /*
   * Which frequencies get a printed label. All fourteen never fit - on a log
   * axis the top four sit within a few percent of each other - so keep them
   * greedily left to right with a minimum gap, then force the selected one in
   * and evict whatever it lands on. The dots are all still there and still
   * selectable; only the text thins out.
   */
  const LABEL_GAP = 46;
  const labelled = (() => {
    const keep: number[] = [];
    let lastX = -Infinity;
    points.forEach((real, i) => {
      const x = geom.xFor(state.freqs[real]);
      if (x - lastX >= LABEL_GAP) {
        keep.push(i);
        lastX = x;
      }
    });
    if (selected !== null && !keep.includes(selected)) {
      const sx = geom.xFor(state.freqs[points[selected]]);
      return new Set(
        keep.filter((i) => Math.abs(geom.xFor(state.freqs[points[i]]) - sx) >= LABEL_GAP)
          .concat(selected),
      );
    }
    return new Set(keep);
  })();

  const stockLevel = selectedReal >= 0 ? geom.levels[selectedReal] || 0 : 0;

  return (
    <div className="adreno-uv-page">
      <style>{editorStyles}</style>

      {/* Horizontal container so focus can cross between the controls and the
          graph. Without it the two columns are unrelated siblings and the
          nested vertical containers swallow left/right. */}
      <Focusable className="adreno-uv-body" flow-children="horizontal">
        {/* One vertical container: D-pad up/down walks the controls, and
            right leaves the sidebar for the graph beside it. */}
        <Focusable className="adreno-uv-side" flow-children="vertical">
          <div className="adreno-uv-heading">GPU Undervolt</div>
          <div className="adreno-uv-field">
            <div className="adreno-uv-label">Profile</div>
            <Dropdown
              disabled={tuning}
              selectedOption={String(active)}
              rgOptions={profileOptions}
              onChange={(option) => mutate(profiles, Number(option.data) || 0, `Editing ${names[Number(option.data) || 0]}.`)}
            />
          </div>

          {tuning ? (
            <div className="adreno-uv-tuning">
              <div className="adreno-uv-tuning-title">AUTO-TUNING — read only</div>
              <div className="adreno-uv-detail">{tune?.message || tune?.phase}</div>
              {tune?.current ? (
                <div className="adreno-uv-detail">
                  {`${Math.round(tune.current.freq / 1000000)} MHz at −${tune.current.shift}, ${tune.current.startTemp}°C`}
                </div>
              ) : null}
              <div className="adreno-uv-detail">
                {`${(tune?.steps || []).length} steps done · ` +
                  `${(tune?.steps || []).filter((s) => s.result !== "pass").length} limits found`}
              </div>
              {tune?.resumedFrom ? (
                <div className="adreno-uv-detail">
                  {`continuing from ${(tune.resumedFrom.curve || []).filter((v) => v).length} `
                    + "points tuned earlier"}
                </div>
              ) : null}
              {tune?.resumeRefused ? (
                <div className="adreno-uv-detail">{`started over: ${tune.resumeRefused}`}</div>
              ) : null}
            </div>
          ) : null}

          <Focusable className="adreno-uv-actions" flow-children="vertical">
            {tuning ? (
              <DialogButton
                onClick={() => {
                  tuneStop()
                    .then(() => setStatus("Stopping after this step — the curve so far is kept."))
                    .catch(() => setStatus("Could not stop the tuner."));
                }}
              >
                Stop tuning
              </DialogButton>
            ) : (
              /* Continue and from-scratch are two ways to start the same run,
                 so the choice belongs in the dialog rather than in a second
                 button. Soaking is a separate question and gets its own. */
              <>
                <DialogButton onClick={openTune}>Auto-tune…</DialogButton>
                {/* Tries to break the profile in use, rather than looking for a
                    deeper one. Only worth offering once something is set. */}
                {(profiles[active] || []).some((v) => v) ? (
                  <DialogButton onClick={openSoak}>Soak this profile…</DialogButton>
                ) : null}
              </>
            )}
            {/* "wedged" belongs here as much as the rest: the run ended early,
                but everything it measured before the GPU died is real, and this
                button is the only way to keep it. Leaving it out is how an
                hour of measurement gets thrown away by a crash in minute 61. */}
            {!tuning && tune && (tune.phase === "done" || tune.phase === "stopped"
              || tune.phase === "interrupted" || tune.phase === "wedged")
              && (tune.recommended || []).some((v) => v) ? (
              /* Normal and aggressive come from one sweep and differ by a
                 step; the dialog can explain that trade, a second button
                 cannot. */
              <DialogButton onClick={openUseCurve}>Use tuned curve…</DialogButton>
            ) : null}
            <DialogButton
              onClick={() => mutate(
                profiles.map((entry, i) => (i === active ? entry.map(() => 0) : entry)),
                active,
                `${names[active]} reset to stock.`,
              )}
            >
              Reset to Stock
            </DialogButton>
            <DialogButton disabled={tuning} onClick={openCopy}>Copy this profile to…</DialogButton>
          </Focusable>

          <div className="adreno-uv-status">{status || " "}</div>
        </Focusable>

        <div className="adreno-uv-plotcol">
      <Focusable
        className={editing ? "adreno-uv-plot editing" : "adreno-uv-plot"}
        onActivate={() => tuning ? undefined : setEditing((current) => {
          if (!current && selectedRef.current === null) setSelected(0);
          return !current;
        })}
        {...(editing
          ? {
              // Attached ONLY while editing. Registering the handler at all
              // makes Steam treat B as consumed, so returning early from it
              // still swallowed the press and trapped you on the page.
              onCancelButton: (evt: GamepadEvent) => {
                setEditing(false);
                evt.stopPropagation();
                evt.preventDefault();
              },
            }
          : {})}
        onButtonDown={onPlotButton}
        onOKActionDescription={editing ? "Done" : "Edit curve"}
        onCancelActionDescription={editing ? "Done" : "Back"}
      >
        <div className="adreno-uv-scale-note">log scale</div>
        {editing ? (
          <div className="adreno-uv-plot-hint editing">
            Left / Right: frequency     Up / Down: voltage     A or B: done
          </div>
        ) : null}
        <svg
          ref={svgRef}
          viewBox={`0 0 ${box.w} ${box.h}`}
          className="adreno-uv-svg"
          onPointerDown={(e) => {
            dragging.current = true;
            setTouching(true);
            (e.target as Element).setPointerCapture?.(e.pointerId);
            pointerEdit(e.clientX, e.clientY);
          }}
          onPointerMove={(e) => {
            if (dragging.current) pointerEdit(e.clientX, e.clientY);
          }}
          onPointerUp={() => { dragging.current = false; setTouching(false); }}
          onPointerLeave={() => { dragging.current = false; setTouching(false); }}
        >
          {/* The y axis is the voltage corner the GPU actually lands on, not the
              shift number - a shift of -3 at 443 MHz and at 1100 MHz are
              different corners, and the plot shows where each one really sits. */}
          <text
            className="adreno-uv-axislabel"
            transform={`translate(15, ${PAD_T + (box.h - PAD_T - PAD_B) / 2}) rotate(-90)`}
          >
            voltage corner
          </text>

          {gridRows.map((level) => {
            const y = geom.yFor(level);
            return (
              <g key={level}>
                <line x1={PAD_L} x2={box.w - PAD_R} y1={y} y2={y} className="adreno-uv-grid" />
                <text x={PAD_L - 10} y={y + 4} className="adreno-uv-ytick">
                  {level}
                </text>
              </g>
            );
          })}

          {/* Stock first, then the two measured curves, then the edited one on
              top - the line you are dragging must never be hidden behind a
              reference line that happens to share a value. */}
          <polyline points={stockPoints.join(" ")} className="adreno-uv-stockline" />
          {normalLine ? (
            <polyline points={normalLine.join(" ")} className="adreno-uv-normalline" />
          ) : null}
          {aggressiveLine ? (
            <polyline points={aggressiveLine.join(" ")} className="adreno-uv-aggressiveline" />
          ) : null}
          <polyline points={plotPoints.join(" ")} className="adreno-uv-line" />

          {points.map((real, i) => {
            const x = geom.xFor(state.freqs[real]);
            const y = geom.yFor(levelAt(real));
            const isSelected = i === selected;
            return (
              <g key={real}>
                <line
                  x1={x}
                  x2={x}
                  y1={PAD_T}
                  y2={box.h - PAD_B}
                  className={isSelected ? "adreno-uv-col selected" : "adreno-uv-col"}
                />
                <circle cx={x} cy={y} r={isSelected ? 11 : 7} className={isSelected ? "adreno-uv-dot selected" : "adreno-uv-dot"} />
                {/* A crash sets failedAt too, so show only the crash where both
                    land on the same depth - two marks on one spot reads as two
                    separate events. */}
                {typeof failedAt[String(real)] === "number"
                  && failedAt[String(real)] !== crashedAt[String(real)] ? (
                  <circle
                    cx={x}
                    cy={yForShift(real, failedAt[String(real)])}
                    r={5}
                    className="adreno-uv-failmark"
                  />
                ) : null}
                {typeof crashedAt[String(real)] === "number" ? (
                  <g className="adreno-uv-crashmark">
                    {/* Dark halo first, white X over it: aggressive is red too,
                        and at this size two reds read as one thing. */}
                    <line className="halo"
                      x1={x - 6} y1={yForShift(real, crashedAt[String(real)]) - 6}
                      x2={x + 6} y2={yForShift(real, crashedAt[String(real)]) + 6}
                    />
                    <line className="halo"
                      x1={x - 6} y1={yForShift(real, crashedAt[String(real)]) + 6}
                      x2={x + 6} y2={yForShift(real, crashedAt[String(real)]) - 6}
                    />
                    <line
                      x1={x - 6} y1={yForShift(real, crashedAt[String(real)]) - 6}
                      x2={x + 6} y2={yForShift(real, crashedAt[String(real)]) + 6}
                    />
                    <line
                      x1={x - 6} y1={yForShift(real, crashedAt[String(real)]) + 6}
                      x2={x + 6} y2={yForShift(real, crashedAt[String(real)]) - 6}
                    />
                  </g>
                ) : null}
              </g>
            );
          })}
        </svg>
        </Focusable>

          <div className="adreno-uv-xaxis">
            {points.map((real, i) => (labelled.has(i) ? (
              <span
                key={real}
                className={i === selected ? "adreno-uv-xlabel selected" : "adreno-uv-xlabel"}
                style={{ left: `${(geom.xFor(state.freqs[real]) / (box.w || 1)) * 100}%` }}
              >
                {mhz(state.freqs[real])}
              </span>
            ) : null))}
          </div>

          {/* Transient: only while the curve is being edited. Rendered always
              and hidden with visibility, because removing it from the layout
              would resize the plot and redraw the curve on every activation. */}
          {/* Below the graph, and ALWAYS rendered - hidden with visibility, not
              removed - so the row it occupies never appears or disappears. A
              legend that comes and goes resizes the plot and redraws the curve,
              which is why it cannot simply be conditional here. */}
          <div className={anyMarks || normalLine || aggressiveLine
            ? "adreno-uv-legend" : "adreno-uv-legend hidden"}>
            {normalLine ? <><span className="normal">— —</span> <b>normal</b>{"   "}</> : null}
            {aggressiveLine ? <><span className="aggressive">· · ·</span> <b>aggressive</b>{"   "}</> : null}
            {Object.keys(failedAt).length ? (
              <><span className="fail">○</span> <b>failed</b> here{"   "}</>
            ) : null}
            {Object.keys(crashedAt).length ? (
              <><span className="crash">✕</span> <b>crashed</b> — blocked{"   "}</>
            ) : null}
          </div>

          <div
            className={
              (editing || touching) && selected !== null && selectedReal >= 0
                ? "adreno-uv-readout"
                : "adreno-uv-readout hidden"
            }
          >
            {selectedReal >= 0 ? (
              <div className="adreno-uv-label">
                <span className="adreno-uv-freq">{mhz(state.freqs[selectedReal])} MHz</span>
                {/* The stored value first, then what it actually does. Showing
                    only the step hides that its effect varies wildly; showing
                    only the corner hides what is saved in the profile. */}
                <span className="adreno-uv-step">
                  {Number(curve[selectedReal]) ? `−${Number(curve[selectedReal])}` : "stock"}
                </span>
                <span className="adreno-uv-detail">
                  {levelAt(selectedReal) !== stockLevel
                    ? `corner ${levelAt(selectedReal)} — ${stockLevel - levelAt(selectedReal)} below stock (${stockLevel})`
                    : `corner ${stockLevel}`}
                </span>
                {/* What the sweep found HERE. Without it the marks on the plot
                    are the only evidence, and they are easy to miss while
                    dragging the point they sit on. */}
                <span className="adreno-uv-tested">{testedSummary(selectedReal)}</span>
              </div>
            ) : null}
          </div>
        </div>
      </Focusable>
    </div>
  );
}
