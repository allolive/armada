import { call } from "@decky/api";

/** One shift step per operating point, index-aligned to UvState.freqs. */
export type Curve = number[];

export interface UvConfig {
  /** Whether the destructive-action warning has been acknowledged once. */
  warned: boolean;
  /** Apply the selected profile automatically when a game is launched. */
  autoApply: boolean;
  active: number;
  profiles: Curve[];
}

export interface UvState {
  moduleLoaded: boolean;
  /**
   * The module loaded, then found the GMU's vote table was not the layout it
   * was compiled against, and went inert. vermagic cannot tell our kernel
   * build from Armada's stock one, so insmod succeeds after an OS update and
   * the module looks healthy while doing nothing.
   */
  kernelIncompatible: boolean;
  /** "waiting" | "ok" | "incompatible"; empty on a module without the param. */
  moduleStatus: string;
  /**
   * Why the module is not loaded, when it is not: the loader unit's own error
   * rather than a guess. Empty while it is loaded.
   */
  moduleError: string;
  /** False means adreno-uv.service has not handed the knob over. */
  shiftWritable: boolean;
  /** False means the module predates apply_now and cannot apply immediately. */
  canApplyNow: boolean;
  /** Hz per shift index. Empty until the GPU has booted once. */
  freqs: number[];
  maxShift: number;
  profileNames: string[];
  shift: number[];
  /** The undervolt is in the GMU's table right now. */
  undervoltLive: boolean;
  /** What is running differs from what was asked for, in either direction. */
  pending: boolean;
  /** An undervolt is in the GPU's table right now, whatever was requested. */
  liveDiffersFromStock: boolean;
  /** Index of the stored profile the live shift matches, or -1 for stock/custom. */
  appliedProfile: number;
  stockVotes: string;
  appliedVotes: string;
  hits: number;
  /** Something else is also driving GPU voltage. */
  armadaUvLive: boolean;
  config: UvConfig;
}

export interface ApplyResult {
  written: boolean;
  error: string;
  curve: Curve;
  /** Human-readable outcome of the immediate-apply attempt. */
  note: string;
  /** True when the curve was already in the GPU and nothing was touched. */
  skipped?: boolean;
  /** Which profile was applied, or -1 for stock. */
  profile?: number;
}

export const getState = () => call<[], UvState>("get_state");
export const saveConfig = (config: UvConfig) => call<[UvConfig], UvState>("save_config", config);
export const applyProfile = (index?: number | null, force = false, timeout = 0.5) =>
  call<[number | null | undefined, boolean, number], ApplyResult>("apply_profile", index, force, timeout);
export const refresh = () => call<[], ApplyResult>("refresh");
export const applyCurve = (curve: Curve) => call<[Curve], ApplyResult>("apply_curve", curve);
export const restoreStock = () => call<[], { written: boolean; error: string }>("restore_stock");

export interface TuneStep {
  index: number;
  freq: number;
  shift: number;
  result: string;
  detail: string;
  startTemp: number;
  peakTemp: number | null;
  seconds: number;
}

export interface TuneStatus {
  running: boolean;
  phase: string;
  message?: string;
  depth?: number;
  /** Deepest shift measured stable at each point. */
  curve?: number[];
  /** Normal: one step back wherever an edge was measured. The default. */
  recommended?: number[];
  /** Aggressive: the exact last stable point, backed off only after a crash. */
  aggressive?: number[];
  freqs?: number[];
  steps?: TuneStep[];
  current?: { index: number; freq: number; shift: number; startTemp: number } | null;
  verify?: { result: string; detail: string; peakTemp: number };
  stopRequested?: boolean;
  referenceCrc?: string;
  /** Set when this run continued from an earlier journal instead of stock. */
  resumedFrom?: { curve: number[]; updated?: number; phase?: string };
  /** Set when a resume was asked for but the OPP table had moved since. */
  resumeRefused?: string;
  /** Curve banked before the recheck and the soak, so neither can erase it. */
  lastStable?: number[];
  /** Depth at which each point was measured to fail, by index. */
  failedAt?: Record<string, number>;
  /** Steps that stopped the GPU recovering. Blocked for good, by index. */
  crashedAt?: Record<string, number>;
  /** How many soak rounds each point has survived, by index. */
  soakPasses?: Record<string, number>;
  /** The step that ended this run, when one did. */
  crash?: { index: number; freq: number; shift: number; detail: string; at: number };
}

export const tuneStart = (stepSeconds: number, verifySeconds: number, resume = true) =>
  call<[number, number, boolean], { started: boolean; error: string }>(
    "tune_start", stepSeconds, verifySeconds, resume);

/** Soak the profile in use until stopped. Never deepens anything. */
export const soakStart = (stepSeconds = 60) =>
  call<[number], { started: boolean; error: string }>("soak_start", stepSeconds);
export const tuneStop = () => call<[], { stopped: boolean; error: string }>("tune_stop");
export const tuneStatus = () => call<[], TuneStatus>("tune_status");
export type TunedCurve = "normal" | "aggressive";

export const tuneAccept = (useRecommended: boolean, mode: TunedCurve = "normal") =>
  call<[boolean, TunedCurve], { saved: boolean; profile: number; mode: string; curve: number[] }>(
    "tune_accept", useRecommended, mode);
