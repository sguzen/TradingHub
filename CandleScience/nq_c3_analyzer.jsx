import { useState, useMemo, useEffect, useRef } from "react";

// ─────────────────────────────────────────────────────────────
// LOAD PROBABILITIES FROM nq_probs.json
// Place nq_probs.json in the same folder and serve with:
//   python3 -m http.server 3000
// Then open http://localhost:3000
// ─────────────────────────────────────────────────────────────

const SECTIONS = [
  {
    title: "HIGH WICKS",
    rows: [
      { c2key: "c2_high_gt_c1_high",  c3key: "c3_high_gt_c2_high",  leftC2: "C2 High",  rightC1: "C1 High",  leftC3: "C3 High",  rightC2: "C2 High"  },
      { c2key: "c2_high_gt_c1_open",  c3key: "c3_high_gt_c2_open",  leftC2: "C2 High",  rightC1: "C1 Open",  leftC3: "C3 High",  rightC2: "C2 Open"  },
    ],
  },
  {
    title: "LOW WICKS",
    rows: [
      { c2key: "c2_low_gt_c1_low",    c3key: "c3_low_gt_c2_low",    leftC2: "C2 Low",   rightC1: "C1 Low",   leftC3: "C3 Low",   rightC2: "C2 Low"   },
      { c2key: "c2_low_gt_c1_open",   c3key: "c3_low_gt_c2_open",   leftC2: "C2 Low",   rightC1: "C1 Open",  leftC3: "C3 Low",   rightC2: "C2 Open"  },
    ],
  },
  {
    title: "BODY",
    rows: [
      { c2key: "c2_close_gt_c1_high",  c3key: "c3_close_gt_c2_high",  leftC2: "C2 Close", rightC1: "C1 High",  leftC3: "C3 Close", rightC2: "C2 High"  },
      { c2key: "c2_close_gt_c1_low",   c3key: "c3_close_gt_c2_low",   leftC2: "C2 Close", rightC1: "C1 Low",   leftC3: "C3 Close", rightC2: "C2 Low"   },
      { c2key: "c2_close_gt_c1_close", c3key: "c3_close_gt_c2_close", leftC2: "C2 Close", rightC1: "C1 Close", leftC3: "C3 Close", rightC2: "C2 Close" },
      { c2key: "c2_close_gt_c1_open",  c3key: "c3_close_gt_c2_open",  leftC2: "C2 Close", rightC1: "C1 Open",  leftC3: "C3 Close", rightC2: "C2 Open"  },
    ],
  },
  {
    title: "GAPS",
    rows: [
      { c2key: "c2_open_gt_c1_close", c3key: "c3_open_gt_c2_close", leftC2: "C2 Open",  rightC1: "C1 Close", leftC3: "C3 Open",  rightC2: "C2 Close" },
      { c2key: "c2_open_gt_c1_open",  c3key: "c3_open_gt_c2_open",  leftC2: "C2 Open",  rightC1: "C1 Open",  leftC3: "C3 Open",  rightC2: "C2 Open"  },
      { c2key: "c2_open_gt_c1_high",  c3key: "c3_open_gt_c2_high",  leftC2: "C2 Open",  rightC1: "C1 High",  leftC3: "C3 Open",  rightC2: "C2 High"  },
      { c2key: "c2_open_gt_c1_low",   c3key: "c3_open_gt_c2_low",   leftC2: "C2 Open",  rightC1: "C1 Low",   leftC3: "C3 Open",  rightC2: "C2 Low"   },
    ],
  },
];

// ─────────────────────────────────────────────────────────────
// PROB LOOKUP
// Given loaded JSON data, c1/c2 color, and current selections,
// return the correct probability for any metric
// ─────────────────────────────────────────────────────────────
function lookupProb(data, c1, c2, selections, metricKey, side /* "c2"|"c3" */) {
  if (!data) return null;
  const combo = `${c1}_${c2}`;

  // Count active selections
  const activeKeys = Object.entries(selections).filter(([, v]) => v);

  // If no selections, use base probs for this color combo
  if (activeKeys.length === 0) {
    const base = data.probs?.[combo];
    if (!base) return null;
    return side === "c2" ? base.c2?.[metricKey] : base.c3?.[metricKey];
  }

  // If one selection, use conditional lookup directly
  if (activeKeys.length === 1 && side === "c3") {
    const [selKey, selDir] = activeKeys[0];
    const condKey = `${selKey}_${selDir}`;
    const cond = data.conditional?.[combo]?.[condKey];
    if (cond && cond.n >= 10) {
      return cond.c3?.[metricKey] ?? null;
    }
  }

  // For C2 metrics with a selection: use Bayesian-weighted blend
  // P(metric) = P(metric|above)*P(above) + P(metric|below)*P(below)
  // We approximate by blending conditionals weighted by selection strength
  if (side === "c2") {
    // Start from base and adjust by active selections
    const base = data.probs?.[combo]?.c2?.[metricKey];
    if (base == null) return null;

    let adjusted = base;
    for (const [selKey, selDir] of activeKeys) {
      if (selKey === metricKey) continue; // skip self
      // Find how this selection shifts things
      const condAbove = data.conditional?.[combo]?.[`${selKey}_above`]?.c3;
      const condBelow = data.conditional?.[combo]?.[`${selKey}_below`]?.c3;
      // We don't have C2 conditionals, so use a conservative shift
      const baseSelPct = data.probs?.[combo]?.c2?.[selKey] ?? 50;
      const expectedDir = selDir === "above" ? baseSelPct : 100 - baseSelPct;
      const shift = ((expectedDir - 50) / 50) * 3; // max ±3% nudge
      adjusted = Math.min(98, Math.max(2, adjusted + shift));
    }
    return Math.round(adjusted * 10) / 10;
  }

  // Multiple C3 selections: average the conditionals (naive but reasonable)
  if (side === "c3") {
    const vals = [];
    for (const [selKey, selDir] of activeKeys) {
      const condKey = `${selKey}_${selDir}`;
      const cond = data.conditional?.[combo]?.[condKey];
      if (cond && cond.n >= 10 && cond.c3?.[metricKey] != null) {
        // Weight by sample size
        vals.push({ v: cond.c3[metricKey], n: cond.n });
      }
    }
    if (vals.length === 0) {
      return data.probs?.[combo]?.c3?.[metricKey] ?? null;
    }
    const totalN = vals.reduce((s, x) => s + x.n, 0);
    const weighted = vals.reduce((s, x) => s + x.v * x.n, 0) / totalN;
    return Math.round(weighted * 10) / 10;
  }

  return null;
}

function lookupC3Bull(data, c1, c2, selections) {
  if (!data) return null;
  const combo = `${c1}_${c2}`;
  const activeKeys = Object.entries(selections).filter(([, v]) => v);

  if (activeKeys.length === 0) return data.probs?.[combo]?.c3_bull ?? null;

  if (activeKeys.length === 1) {
    const [selKey, selDir] = activeKeys[0];
    const cond = data.conditional?.[combo]?.[`${selKey}_${selDir}`];
    if (cond && cond.n >= 10) return cond.c3?.c3_bull ?? null;
  }

  // Multi-selection: weighted average
  const vals = [];
  for (const [selKey, selDir] of activeKeys) {
    const cond = data.conditional?.[combo]?.[`${selKey}_${selDir}`];
    if (cond && cond.n >= 10 && cond.c3?.c3_bull != null) {
      vals.push({ v: cond.c3.c3_bull, n: cond.n });
    }
  }
  if (vals.length === 0) return data.probs?.[combo]?.c3_bull ?? null;
  const totalN = vals.reduce((s, x) => s + x.n, 0);
  return Math.round(vals.reduce((s, x) => s + x.v * x.n, 0) / totalN * 10) / 10;
}

// ─────────────────────────────────────────────────────────────
// UI COMPONENTS
// ─────────────────────────────────────────────────────────────
function Pill({ label, pct, selected, onClick, readOnly }) {
  const isAbove = label === "Above";
  const dominant = pct != null && pct >= 60;
  const dotColor = dominant ? (isAbove ? "#4caf50" : "#ef5350") : "#2e2e3e";
  const loading = pct == null;

  return (
    <div onClick={!readOnly ? onClick : undefined} style={{
      background: selected ? "#1c1c2e" : "#0e0e1a",
      border: `1px solid ${selected ? "#3a3a58" : "#181826"}`,
      borderRadius: 7, padding: "5px 10px",
      display: "flex", alignItems: "center", gap: 7,
      cursor: readOnly ? "default" : "pointer",
      transition: "all 0.12s",
      boxShadow: selected ? "inset 0 0 0 1px #44446a" : "none",
      userSelect: "none", opacity: loading ? 0.4 : 1,
    }}>
      <div style={{
        width: 9, height: 9, borderRadius: "50%",
        background: dotColor, flexShrink: 0,
        boxShadow: dominant ? `0 0 6px ${dotColor}aa` : "none",
        transition: "background 0.2s, box-shadow 0.2s",
      }} />
      <span style={{
        fontSize: 12,
        color: selected ? "#d0d0f0" : dominant ? "#999" : "#484858",
        transition: "color 0.12s",
      }}>
        {label} {loading ? "..." : `${pct}%`}
      </span>
    </div>
  );
}

function PillRow({ leftLabel, rightLabel, abovePct, belowPct, selected, onSelect, basePct, showDelta, readOnly }) {
  const deltaA = (basePct != null && abovePct != null && showDelta)
    ? (abovePct - basePct).toFixed(1) : null;

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 9 }}>
      <span style={{ width: 58, textAlign: "right", fontSize: 11, color: "#444", flexShrink: 0 }}>
        {leftLabel}
      </span>
      <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 3 }}>
        <Pill label="Above" pct={abovePct}
          selected={selected === "above"}
          onClick={() => onSelect(selected === "above" ? null : "above")}
          readOnly={readOnly} />
        <Pill label="Below" pct={belowPct != null ? belowPct : (abovePct != null ? 100 - abovePct : null)}
          selected={selected === "below"}
          onClick={() => onSelect(selected === "below" ? null : "below")}
          readOnly={readOnly} />
      </div>
      <span style={{ width: 52, fontSize: 11, color: "#444", flexShrink: 0 }}>{rightLabel}</span>
      <div style={{ width: 42, textAlign: "right", flexShrink: 0 }}>
        {deltaA !== null && (
          <>
            <div style={{ fontSize: 10, color: +deltaA > 0.4 ? "#4caf50" : +deltaA < -0.4 ? "#ef5350" : "#333" }}>
              {+deltaA > 0 ? "+" : ""}{deltaA}%
            </div>
            <div style={{ fontSize: 10, color: +deltaA < -0.4 ? "#4caf50" : +deltaA > 0.4 ? "#ef5350" : "#333" }}>
              {+deltaA < 0 ? "+" : "-"}{Math.abs(+deltaA).toFixed(1)}%
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function DirBtn({ label, pct, active, onClick, color, readOnly }) {
  return (
    <button onClick={!readOnly ? onClick : undefined} style={{
      display: "flex", alignItems: "center", gap: 8,
      padding: "7px 14px", borderRadius: 7,
      cursor: readOnly ? "default" : "pointer",
      background: active ? "#1a1a2c" : "#0d0d18",
      border: `1px solid ${active ? color + "44" : "#161622"}`,
      color: active ? "#ccc" : "#333",
      fontSize: 12, width: "100%", marginBottom: 5,
      outline: "none", transition: "all 0.15s",
    }}>
      <div style={{
        width: 9, height: 9, borderRadius: "50%",
        background: active ? color : "#1e1e28",
        boxShadow: active ? `0 0 6px ${color}88` : "none",
        transition: "all 0.15s",
      }} />
      {label} {pct != null ? `${pct}%` : "..."}
    </button>
  );
}

function HR() {
  return <div style={{ borderTop: "1px solid #10101a", margin: "6px 0 20px" }} />;
}

// ─────────────────────────────────────────────────────────────
// MAIN APP
// ─────────────────────────────────────────────────────────────
export default function App() {
  const [data, setData]   = useState(null);
  const [error, setError] = useState(null);
  const [c1, setC1]       = useState("bull");
  const [c2, setC2]       = useState("bull");
  const [sel, setSel]     = useState({});

  // Load nq_probs.json
  useEffect(() => {
    fetch("nq_probs.json")
      .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
      .then(setData)
      .catch(e => setError(e.message));
  }, []);

  const onSelect = (key, val) => setSel(prev => ({ ...prev, [key]: val }));
  const onSetC1  = (v) => { setC1(v); setSel({}); };
  const onSetC2  = (v) => { setC2(v); setSel({}); };

  // Base probs (no selection) for delta comparison
  const getBase = (key, side) => {
    if (!data) return null;
    const combo = `${c1}_${c2}`;
    return side === "c2"
      ? data.probs?.[combo]?.c2?.[key] ?? null
      : data.probs?.[combo]?.c3?.[key] ?? null;
  };

  const getProb = (key, side) => lookupProb(data, c1, c2, sel, key, side);
  const getC3Bull = () => lookupC3Bull(data, c1, c2, sel);

  const activeCount = Object.values(sel).filter(Boolean).length;
  const c3Bull = getC3Bull();
  const bias = c3Bull == null ? null
    : c3Bull >= 58 ? { label: "BULLISH", color: "#4caf50" }
    : c3Bull <= 42 ? { label: "BEARISH", color: "#ef5350" }
    : { label: "NEUTRAL", color: "#ffd54f" };

  const sampleN = data?.probs?.[`${c1}_${c2}`]?.n;

  return (
    <div style={{
      minHeight: "100vh", background: "#090912", color: "#e0e0ff",
      fontFamily: "'SF Mono','Fira Mono','Consolas',monospace",
      paddingBottom: 60,
    }}>

      {/* Header */}
      <div style={{
        padding: "12px 16px 10px", background: "#07070f",
        borderBottom: "1px solid #11111e",
      }}>
        <div style={{ fontSize: 9, color: "#222232", letterSpacing: 3, marginBottom: 2 }}>
          NQ FUTURES · PATTERN ENGINE
        </div>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
          <div style={{ fontSize: 16, fontWeight: 800 }}>C1 / C2 → C3 Dashboard</div>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            {data && sampleN && (
              <span style={{ fontSize: 10, color: "#333" }}>n={sampleN.toLocaleString()}</span>
            )}
            {bias && activeCount > 0 && (
              <span style={{ fontSize: 11, fontWeight: 700, color: bias.color, letterSpacing: 1 }}>
                {bias.label}
              </span>
            )}
            {activeCount > 0 && (
              <button onClick={() => setSel({})} style={{
                fontSize: 9, color: "#444", background: "#0d0d18",
                border: "1px solid #1a1a28", borderRadius: 5,
                padding: "3px 8px", cursor: "pointer", letterSpacing: 1
              }}>RESET</button>
            )}
          </div>
        </div>

        {/* Source & status */}
        <div style={{ marginTop: 6, fontSize: 9, color: error ? "#ef5350" : data ? "#2a4a2a" : "#333" }}>
          {error
            ? `⚠ Could not load nq_probs.json — ${error}. Run: python3 nq_build_probs.py`
            : data
            ? `✓ Loaded nq_probs.json · Generated ${new Date(data.generated).toLocaleDateString()}`
            : "Loading nq_probs.json..."}
        </div>

        {/* Bullish pressure bar */}
        {c3Bull != null && activeCount > 0 && (
          <div style={{ marginTop: 8 }}>
            <div style={{ display: "flex", justifyContent: "space-between", fontSize: 9, color: "#252535", marginBottom: 3 }}>
              <span>BEAR</span>
              <span>BULLISH PRESSURE · {c3Bull}%</span>
              <span>BULL</span>
            </div>
            <div style={{ height: 3, background: "#0d0d18", borderRadius: 2, overflow: "hidden" }}>
              <div style={{
                height: "100%", width: `${c3Bull}%`,
                background: bias?.color ?? "#ffd54f",
                borderRadius: 2, transition: "width 0.3s ease, background 0.3s ease"
              }} />
            </div>
          </div>
        )}
      </div>

      <div style={{ padding: "14px 12px 0" }}>

        {/* Candle Direction */}
        <div style={{ marginBottom: 18 }}>
          <div style={{ fontSize: 11, fontWeight: 700, marginBottom: 10, color: "#555", letterSpacing: 1 }}>
            CANDLE DIRECTION
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 8 }}>
            {[
              { label: "C1", val: c1, set: onSetC1, ro: false },
              { label: "C2", val: c2, set: onSetC2, ro: false },
              { label: "C3", val: null, set: null,   ro: true  },
            ].map(({ label, val, set, ro }) => {
              const bullPct = ro ? c3Bull : (data?.probs?.[`${c1}_${c2}`]?.n ? (
                label === "C1"
                  ? Math.round(Object.entries(data.probs).filter(([k]) => k.startsWith("bull")).reduce((s,[,v])=>s+v.n,0) /
                    Object.values(data.probs).reduce((s,v)=>s+v.n,0) * 100)
                  : null
              ) : null);

              // For C1/C2 buttons, show distribution from data
              const c1BullN = (data?.probs?.bull_bull?.n ?? 0) + (data?.probs?.bull_bear?.n ?? 0);
              const totalN  = Object.values(data?.probs ?? {}).reduce((s,v)=>s+(v.n??0),0);
              const c1BullPct = totalN ? Math.round(c1BullN / totalN * 100) : 56;

              const c2BullN = (data?.probs?.bull_bull?.n ?? 0) + (data?.probs?.bear_bull?.n ?? 0);
              const c2BullPct = totalN ? Math.round(c2BullN / totalN * 100) : 56;

              const bPct = ro ? c3Bull
                : label === "C1" ? c1BullPct
                : c2BullPct;
              const bearPct = bPct != null ? 100 - bPct : null;

              return (
                <div key={label} style={{
                  background: "#0d0d18", border: "1px solid #16162a",
                  borderRadius: 9, padding: "10px 11px"
                }}>
                  <div style={{ fontSize: 9, color: "#252535", letterSpacing: 2, marginBottom: 7 }}>{label}</div>
                  <DirBtn label="Bull" pct={bPct} active={!ro && val === "bull"}
                    onClick={() => !ro && set("bull")} color="#4caf50" readOnly={ro} />
                  <DirBtn label="Bear" pct={bearPct} active={!ro && val === "bear"}
                    onClick={() => !ro && set("bear")} color="#ef5350" readOnly={ro} />
                </div>
              );
            })}
          </div>
        </div>

        <HR />

        {/* Sections */}
        {SECTIONS.map(({ title, rows }, si) => (
          <div key={si}>
            <div style={{ fontSize: 11, fontWeight: 700, marginBottom: 10, color: "#555", letterSpacing: 1 }}>
              {title}
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14, marginBottom: 6 }}>

              {/* Left — C2 vs C1 selectable */}
              <div>
                <div style={{ fontSize: 9, color: "#1e1e2e", letterSpacing: 2, marginBottom: 7 }}>C2 vs C1</div>
                {rows.map(({ c2key, leftC2, rightC1 }) => {
                  const abovePct = getProb(c2key, "c2");
                  return (
                    <PillRow key={c2key}
                      leftLabel={leftC2} rightLabel={rightC1}
                      abovePct={abovePct != null ? Math.round(abovePct * 10) / 10 : null}
                      belowPct={abovePct != null ? Math.round((100 - abovePct) * 10) / 10 : null}
                      selected={sel[c2key] || null}
                      onSelect={(v) => onSelect(c2key, v)}
                      basePct={getBase(c2key, "c2")}
                      showDelta={false}
                      readOnly={false}
                    />
                  );
                })}
              </div>

              {/* Right — C3 vs C2 read-only reactive */}
              <div>
                <div style={{ fontSize: 9, color: "#1e1e2e", letterSpacing: 2, marginBottom: 7 }}>C3 vs C2</div>
                {rows.map(({ c3key, leftC3, rightC2 }) => {
                  const abovePct = getProb(c3key, "c3");
                  return (
                    <PillRow key={c3key}
                      leftLabel={leftC3} rightLabel={rightC2}
                      abovePct={abovePct != null ? Math.round(abovePct * 10) / 10 : null}
                      belowPct={abovePct != null ? Math.round((100 - abovePct) * 10) / 10 : null}
                      selected={null}
                      onSelect={() => {}}
                      basePct={getBase(c3key, "c3")}
                      showDelta={activeCount > 0}
                      readOnly={true}
                    />
                  );
                })}
              </div>
            </div>
            {si < SECTIONS.length - 1 && <HR />}
          </div>
        ))}

        <div style={{ fontSize: 9, color: "#111120", textAlign: "center", marginTop: 16 }}>
          LIVE FROM NQ_PROBS.JSON · NOT FINANCIAL ADVICE
        </div>
      </div>
    </div>
  );
}
