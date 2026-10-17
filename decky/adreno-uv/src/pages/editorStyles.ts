/*
 * Ported from the full-screen undervolt editor in armada-control, which the
 * same author wrote. Every class is renamed armada-uv-* -> adreno-uv-*: both
 * plugins inject their stylesheet into the same document, so sharing class
 * names would let one restyle the other.
 */
export const editorStyles = `
      /* The route renders under Steam's own chrome: the header with the search
         box sits over the top of the page and the footer legend over the
         bottom. Neither is part of this document, so the insets are the only
         thing keeping the first and last rows visible. */
      .adreno-uv-page {
        box-sizing: border-box;
        height: 100%;
        overflow-y: auto;
        /* The footer legend overlaps further than 88px, which was hiding the
           action buttons, so the bottom inset has to clear it. 132px was set
           when the plot was a fixed 52vh and the slack was invisible; now that
           the plot fills the column that inset IS the empty band under the
           graph, so it is trimmed to the measured overlap plus a small margin.
           If the footer starts clipping the readout again, this is the number
           to raise. */
        padding: 48px 40px 100px;
        display: flex;
        flex-direction: column;
        gap: 8px;
        background: #0D141C;
      }
      /* Two columns: controls in a narrow sidebar on the left, the curve
         taking whatever width is left on the right. */
      .adreno-uv-body {
        display: flex;
        gap: 20px;
        align-items: stretch;
        flex: 1 1 auto;
        min-height: 0;
      }
      .adreno-uv-plotcol {
        flex: 1 1 auto;
        min-width: 0;
        /* Without this a flex item refuses to shrink below its content, and the
           plot below cannot resolve its share of the column. */
        min-height: 0;
        display: flex;
        flex-direction: column;
      }
      .adreno-uv-side {
        width: 300px;
        flex: 0 0 auto;
        display: flex;
        flex-direction: column;
        gap: 10px;
      }
      .adreno-uv-field {
        display: flex;
        flex-direction: column;
        gap: 4px;
      }
      .adreno-uv-head {
        display: flex;
        align-items: flex-start;
        gap: 28px;
      }
      .adreno-uv-profile {
        width: 240px;
        flex: 0 0 auto;
      }
      .adreno-uv-title {
        flex: 1 1 auto;
        text-align: right;
      }
      .adreno-uv-heading {
        font-size: 19px;
        font-weight: 700;
      }
      .adreno-uv-state {
        font-size: 13px;
        margin-top: 2px;
      }
      .adreno-uv-state.on { color: #6dd36d; }
      .adreno-uv-state.off { opacity: 0.6; }
      .adreno-uv-label {
        text-transform: uppercase;
        font-size: 12px;
        font-weight: 600;
        letter-spacing: 0.5px;
        opacity: 0.7;
        padding-bottom: 6px;
      }
      /* Inside the readout the label carries two lines, so it must not add the
         padding that spaces it from a following control elsewhere. */
      .adreno-uv-readout .adreno-uv-label {
        padding-bottom: 0;
        line-height: 20px;
      }
      .adreno-uv-plot {
        position: relative;
        /* Takes whatever the column has left. Its siblings - the x axis and the
           readout - are both FIXED height, which is what makes this safe: the
           graph must not resize when the readout appears or its text changes,
           because every resize redraws the whole curve. Grow the readout and
           you must grow its declared height with it, never let it size to
           content. */
        display: flex;
        flex-direction: column;
        flex: 1 1 auto;
        min-height: 220px;
        background: rgba(255,255,255,0.04);
        border: 1px solid rgba(255,255,255,0.12);
        padding: 8px;
      }
      .adreno-uv-plot:focus-within {
        border-color: rgba(255,255,255,0.5);
      }
      .adreno-uv-svg {
        display: block;
        width: 100%;
        height: 100%;
        flex: 1 1 auto;
        min-height: 0;
        touch-action: none;
      }
      /* The plot only captures the D-pad once activated, so the two states have
         to be told apart at a glance. */
      .adreno-uv-plot.editing {
        border-color: #2677d8;
        background: rgba(38,119,216,0.10);
      }
      /* Overlaid, not stacked. As a flex row it stole height from the svg the
         moment edit mode turned on, which resized the plot and redrew the whole
         curve - a visible jump every time the graph was activated. */
      .adreno-uv-scale-note {
        position: absolute;
        bottom: 6px;
        right: 12px;
        z-index: 1;
        pointer-events: none;
        font-size: 10px;
        opacity: 0.35;
        letter-spacing: 0.5px;
      }
      .adreno-uv-plot-hint {
        position: absolute;
        top: 10px;
        right: 14px;
        z-index: 1;
        pointer-events: none;
        font-size: 12px;
        opacity: 0.55;
        padding: 0 2px 4px;
      }
      .adreno-uv-plot-hint.editing {
        opacity: 1;
        color: #6ea8e8;
      }
      .adreno-uv-modal-field {
        padding-top: 8px;
        min-width: 320px;
      }
      .adreno-uv-grid {
        stroke: rgba(255,255,255,0.13);
        stroke-width: 1;
      }
      .adreno-uv-col {
        stroke: rgba(255,255,255,0.07);
        stroke-width: 1;
      }
      .adreno-uv-col.selected {
        stroke: rgba(255,255,255,0.34);
        stroke-width: 2;
      }
      .adreno-uv-line {
        fill: none;
        stroke: #2677d8;
        stroke-width: 3;
      }
      /* Stock voltage, drawn under the edited curve. The gap between the two
         lines is the undervolt, which is the whole point of the picture. */
      .adreno-uv-stockline {
        fill: none;
        stroke: rgba(255,255,255,0.35);
        stroke-width: 2;
        stroke-dasharray: 6 5;
      }
      .adreno-uv-dot {
        fill: #2677d8;
        stroke: rgba(255,255,255,0.65);
        stroke-width: 2;
      }
      .adreno-uv-dot.selected {
        fill: #fff;
        stroke: #2677d8;
        stroke-width: 4;
      }
      /* The two curves the last sweep produced, drawn on every profile as
         reference. They are what the tuner measured; the solid blue line is
         whatever this profile currently holds, which may be neither. */
      .adreno-uv-normalline {
        fill: none;
        stroke: #e8cc3a;
        stroke-width: 2;
        stroke-dasharray: 4 4;
        opacity: 0.85;
      }
      .adreno-uv-aggressiveline {
        fill: none;
        stroke: #ef4444;
        stroke-width: 2;
        stroke-dasharray: 1 4;
        stroke-linecap: round;
        opacity: 0.9;
      }
      .adreno-uv-legend .normal { color: #e8cc3a; }
      .adreno-uv-legend .aggressive { color: #ef4444; }
      /* Where the sweep actually hit something. A failure is drawn at the
         depth that FAILED - one step past the curve - so the picture shows the
         edge that was found, not just the value that survived it. */
      .adreno-uv-failmark {
        fill: none;
        stroke: #e07b39;
        stroke-width: 2.5;
      }
      /* A crash is a harder fact than a failure: the GPU stopped recovering
         and that depth is blocked for good, so it gets its own mark. */
      .adreno-uv-crashmark line {
        stroke: #ffffff;
        stroke-width: 3;
        stroke-linecap: round;
      }
      .adreno-uv-crashmark .halo {
        stroke: #b3161a;
        stroke-width: 6;
        stroke-linecap: round;
      }
      /* Overlaid for the same reason as the scale note: as a flex child it
         took its height out of the svg, shrinking the graph the moment a sweep
         produced anything to put in the legend. */
      /* A fixed-height row under the x axis, NOT a child of the plot: as a flex
         child in there it took its height out of the svg, and overlaid on the
         graph it covered the curve. */
      .adreno-uv-legend {
        flex: 0 0 auto;
        height: 18px;
        line-height: 18px;
        margin: 0 9px;
        color: rgba(255,255,255,0.55);
        font-size: 13px;
        white-space: nowrap;
        overflow: hidden;
      }
      .adreno-uv-legend.hidden {
        visibility: hidden;
      }
      .adreno-uv-legend b { font-weight: 600; }
      .adreno-uv-legend .fail { color: #e07b39; }
      .adreno-uv-legend .crash { color: #ffffff; text-shadow: 0 0 3px #b3161a; }
      /* Rotated into the left margin, which PAD_L already reserves for the
         tick numbers - no layout has to move to make room for it. */
      /* Sits under the corner readout while editing. Dimmer than the value
         being dragged: it is evidence about the point, not the point itself. */
      .adreno-uv-tested {
        display: block;
        color: rgba(255,255,255,0.5);
        font-size: 13px;
        line-height: 20px;
        text-transform: none;
        letter-spacing: 0;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .adreno-uv-axislabel {
        fill: rgba(255,255,255,0.45);
        font-size: 13px;
        letter-spacing: 0.4px;
        text-anchor: middle;
      }
      .adreno-uv-ytick {
        fill: rgba(255,255,255,0.6);
        font-size: 15px;
        text-anchor: end;
      }
      .adreno-uv-xtick {
        fill: rgba(255,255,255,0.45);
        font-size: 14px;
        text-anchor: middle;
      }
      .adreno-uv-xtick.selected {
        fill: #fff;
        font-weight: 700;
      }
      /* Outside the plot box, aligned to the same fractions of the drawing
         width so the ticks line up with the columns without living inside the
         chart. The 9px inset matches the plot's 8px padding + 1px border. */
      .adreno-uv-xaxis {
        position: relative;
        height: 16px;
        margin: 4px 9px 0;
        flex: 0 0 auto;
      }
      .adreno-uv-xlabel {
        position: absolute;
        transform: translateX(-50%);
        font-size: 11px;
        opacity: 0.5;
        white-space: nowrap;
      }
      .adreno-uv-xlabel.selected {
        opacity: 1;
        color: #fff;
        font-weight: 600;
      }
      .adreno-uv-readout.hidden {
        visibility: hidden;
      }
      /* TWO lines now - the corner readout plus what the tuner found here -
         and still a fixed height. It sits under a graph that must not move when
         this appears, so it can neither wrap unpredictably nor size to content.
         44px = 22px per line; change one and change the other. */
      .adreno-uv-readout {
        display: flex;
        align-items: flex-start;
        height: 44px;
        flex: 0 0 auto;
        overflow: hidden;
      }
      .adreno-uv-freq {
        font-weight: 700;
        margin-right: 12px;
      }
      /* The stored step, badged so it reads as a setting rather than as part
         of the sentence describing its effect. */
      .adreno-uv-step {
        display: inline-block;
        min-width: 46px;
        text-align: center;
        padding: 1px 8px;
        margin-right: 12px;
        border-radius: 3px;
        background: rgba(38,119,216,0.30);
        border: 1px solid rgba(38,119,216,0.65);
        font-size: 13px;
        font-weight: 600;
      }
      .adreno-uv-detail {
        opacity: 0.7;
        font-size: 13px;
      }
      .adreno-uv-readout .adreno-uv-label {
        width: auto;
        white-space: nowrap;
        text-overflow: ellipsis;
        overflow: hidden;
        text-transform: none;
        letter-spacing: 0;
        font-size: 15px;
        opacity: 1;
        padding: 0;
      }
      .adreno-uv-input {
        width: 280px;
      }
      .adreno-uv-tuning {
        border: 1px solid rgba(216,148,38,0.7);
        background: rgba(216,148,38,0.12);
        padding: 8px 10px;
        display: flex;
        flex-direction: column;
        gap: 3px;
      }
      .adreno-uv-tuning-title {
        font-weight: 700;
        font-size: 13px;
        letter-spacing: 0.5px;
        color: #e0a93c;
      }
      .adreno-uv-actions {
        display: flex;
        flex-direction: column;
        gap: 8px;
        align-items: stretch;
      }
      .adreno-uv-copy {
        width: 170px;
      }
      .adreno-uv-status {
        min-height: 20px;
        font-size: 13px;
        opacity: 0.75;
      }
      .adreno-uv-empty {
        padding: 40px;
        font-size: 15px;
        opacity: 0.8;
      }
    `;
