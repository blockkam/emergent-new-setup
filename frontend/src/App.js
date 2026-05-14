import { useEffect, useMemo, useState } from "react";
import axios from "axios";
import {
  ResponsiveContainer, LineChart, Line, XAxis, YAxis, Tooltip,
  BarChart, Bar, CartesianGrid, ReferenceLine
} from "recharts";
import { RefreshCw, Send, Activity, TrendingUp, TrendingDown, Clock } from "lucide-react";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

const fmt = (n, d = 2) => (n === null || n === undefined || isNaN(n) ? "—" : Number(n).toFixed(d));
const fmtPct = (n) => (n === null || n === undefined || isNaN(n) ? "—" : (n * 100).toFixed(1) + "%");
const fmtR = (n) => (n === null || n === undefined || isNaN(n) ? "—" : (n >= 0 ? "+" : "") + Number(n).toFixed(2) + "R");
const shortTime = (iso) => { if (!iso) return "—"; const d = new Date(iso); return d.toISOString().slice(5, 16).replace("T", " "); };

const Kpi = ({ label, value, sub, color }) => (
  <div className="kpi" data-testid={`kpi-${label.toLowerCase().replace(/\s/g, "-")}`}>
    <div className="lbl">{label}</div>
    <div className="val num" style={{ color }}>{value}</div>
    {sub && <div className="sub mono">{sub}</div>}
  </div>
);

const Pill = ({ children, tone = "dim" }) => <span className={`pill pill-${tone}`}>{children}</span>;

const statusTone = (s) => {
  if (!s) return "dim";
  if (s === "OPEN") return "amber";
  if (s.startsWith("TP")) return "green";
  if (s === "STOPPED" || s === "BE_STOP") return "red";
  return "dim";
};

const sideTone = (s) => (s === "LONG" ? "green" : "red");

function GroupTable({ title, rows, keyLabel }) {
  return (
    <div className="panel">
      <div className="panel-hd">
        <div style={{ fontSize: 12, letterSpacing: ".1em", color: "var(--dim)", textTransform: "uppercase" }}>{title}</div>
        <Pill tone="dim">{rows?.length || 0}</Pill>
      </div>
      <div className="panel-bd" style={{ padding: 0 }}>
        <div className="scroll">
          <table className="t" data-testid={`table-${title.toLowerCase().replace(/\s/g, "-")}`}>
            <thead>
              <tr>
                <th>{keyLabel}</th>
                <th className="r">N</th>
                <th className="r">WR</th>
                <th className="r">Total</th>
                <th className="r">Avg</th>
                <th className="r">MFE</th>
                <th className="r">MAE</th>
              </tr>
            </thead>
            <tbody>
              {(rows || []).map((r) => (
                <tr key={r.key}>
                  <td className="mono">{r.key}</td>
                  <td className="r num">{r.n}</td>
                  <td className="r num" style={{ color: r.win_rate >= 0.5 ? "var(--green)" : "var(--red)" }}>{fmtPct(r.win_rate)}</td>
                  <td className="r num" style={{ color: r.total_r >= 0 ? "var(--green)" : "var(--red)" }}>{fmtR(r.total_r)}</td>
                  <td className="r num">{fmtR(r.avg_r)}</td>
                  <td className="r num pill-dim">{fmt(r.avg_mfe)}</td>
                  <td className="r num pill-dim">{fmt(r.avg_mae)}</td>
                </tr>
              ))}
              {(!rows || rows.length === 0) && (
                <tr><td colSpan={7} style={{ textAlign: "center", color: "var(--dim)", padding: 28 }}>No data yet</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

export default function App() {
  const [metrics, setMetrics] = useState(null);
  const [signals, setSignals] = useState({ items: [], total: 0 });
  const [days, setDays] = useState(30);
  const [status, setStatus] = useState("");
  const [side, setSide] = useState("");
  const [tier, setTier] = useState("");
  const [setupType, setSetupType] = useState("");
  const [entryModel, setEntryModel] = useState("");
  const [htfBias, setHtfBias] = useState("");
  const [regime, setRegime] = useState("");
  const [busy, setBusy] = useState(false);

  const load = async () => {
    try {
      const [m, s] = await Promise.all([
        axios.get(`${API}/metrics`, { params: { days } }),
        axios.get(`${API}/signals`, {
          params: {
            limit: 100, status, side, tier,
            setup_type: setupType, entry_model: entryModel,
            htf_bias: htfBias, regime,
          },
        }),
      ]);
      setMetrics(m.data);
      setSignals(s.data);
    } catch (e) { console.error(e); }
  };

  useEffect(() => { load(); }, [days, status, side, tier, setupType, entryModel, htfBias, regime]); // eslint-disable-line

  const runResolve = async () => { setBusy(true); try { await axios.post(`${API}/resolve`); await load(); } finally { setBusy(false); } };
  const runDigest = async () => { setBusy(true); try { await axios.post(`${API}/digest`); } finally { setBusy(false); } };

  const equityData = useMemo(() => (metrics?.equity || []).map((p, i) => ({ i, r: p.r })), [metrics]);
  const mfeData = metrics?.mfe_hist || [];
  const maeData = metrics?.mae_hist || [];

  return (
    <div className="grid-bg" style={{ minHeight: "100%", padding: "28px 32px" }}>
      {/* HEADER */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 22 }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: 14 }}>
          <div style={{ fontSize: 22, fontWeight: 700, letterSpacing: "-0.02em" }}>
            MySetup<span style={{ color: "var(--amber)" }}> v15</span>
          </div>
          <div className="mono" style={{ color: "var(--dim)", fontSize: 12 }}>· signal performance tracker</div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <select className="select" value={days} onChange={(e) => setDays(Number(e.target.value))} data-testid="select-days">
            <option value={1}>24h</option>
            <option value={7}>7d</option>
            <option value={30}>30d</option>
            <option value={90}>90d</option>
            <option value={365}>1y</option>
          </select>
          <button className="btn" onClick={load} data-testid="btn-refresh"><RefreshCw size={13} style={{ verticalAlign: "middle", marginRight: 6 }} />refresh</button>
          <button className="btn" onClick={runResolve} disabled={busy} data-testid="btn-resolve"><Activity size={13} style={{ verticalAlign: "middle", marginRight: 6 }} />resolve now</button>
          <button className="btn btn-primary" onClick={runDigest} disabled={busy} data-testid="btn-digest"><Send size={13} style={{ verticalAlign: "middle", marginRight: 6 }} />send digest</button>
        </div>
      </div>

      {/* KPI ROW */}
      <div className="panel" style={{ marginBottom: 18 }}>
        <div className="panel-bd" style={{ display: "grid", gridTemplateColumns: "repeat(6, 1fr)", gap: 28 }}>
          <Kpi label="Win Rate" value={fmtPct(metrics?.win_rate)} sub={`${metrics?.resolved ?? 0} resolved`} color={metrics?.win_rate >= 0.5 ? "var(--green)" : "var(--red)"} />
          <Kpi label="Total R" value={fmtR(metrics?.total_r)} sub={`expectancy ${fmtR(metrics?.expectancy)}`} color={(metrics?.total_r ?? 0) >= 0 ? "var(--green)" : "var(--red)"} />
          <Kpi label="Avg Win" value={fmtR(metrics?.avg_win)} sub={`vs avg loss ${fmtR(metrics?.avg_loss)}`} color="var(--text)" />
          <Kpi label="Open" value={metrics?.open ?? "—"} sub="currently live" color="var(--amber)" />
          <Kpi label="Fired" value={metrics?.fired ?? "—"} sub={`last ${days}d`} color="var(--text)" />
          <Kpi label="Window" value={`${days}d`} sub="rolling" color="var(--dim)" />
        </div>
      </div>

      {/* EQUITY CURVE */}
      <div className="panel" style={{ marginBottom: 18 }}>
        <div className="panel-hd">
          <div style={{ fontSize: 12, letterSpacing: ".1em", color: "var(--dim)", textTransform: "uppercase" }}>Equity Curve (cumulative R)</div>
          <Pill tone={(metrics?.total_r ?? 0) >= 0 ? "green" : "red"}>{fmtR(metrics?.total_r)}</Pill>
        </div>
        <div className="panel-bd" style={{ height: 220 }}>
          <ResponsiveContainer>
            <LineChart data={equityData} margin={{ top: 8, right: 16, left: -8, bottom: 0 }}>
              <CartesianGrid stroke="#1f2630" strokeDasharray="2 4" vertical={false} />
              <XAxis dataKey="i" hide />
              <YAxis stroke="#5a6573" tick={{ fontSize: 11, fontFamily: "JetBrains Mono" }} />
              <Tooltip contentStyle={{ background: "#0d1117", border: "1px solid #2a3340", fontSize: 12 }} labelStyle={{ color: "#8b95a5" }} />
              <ReferenceLine y={0} stroke="#2a3340" />
              <Line type="monotone" dataKey="r" stroke="#f5b800" strokeWidth={2} dot={false} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* HISTOGRAMS */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 18, marginBottom: 18 }}>
        <div className="panel">
          <div className="panel-hd"><div style={{ fontSize: 12, letterSpacing: ".1em", color: "var(--dim)", textTransform: "uppercase" }}>MFE Distribution</div><Pill tone="green">favorable</Pill></div>
          <div className="panel-bd" style={{ height: 200 }}>
            <ResponsiveContainer>
              <BarChart data={mfeData} margin={{ top: 4, right: 8, left: -16, bottom: 0 }}>
                <CartesianGrid stroke="#1f2630" strokeDasharray="2 4" vertical={false} />
                <XAxis dataKey="bucket" stroke="#5a6573" tick={{ fontSize: 10, fontFamily: "JetBrains Mono" }} />
                <YAxis stroke="#5a6573" tick={{ fontSize: 10, fontFamily: "JetBrains Mono" }} />
                <Tooltip contentStyle={{ background: "#0d1117", border: "1px solid #2a3340", fontSize: 12 }} />
                <Bar dataKey="count" fill="#26d07c" radius={[2, 2, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
        <div className="panel">
          <div className="panel-hd"><div style={{ fontSize: 12, letterSpacing: ".1em", color: "var(--dim)", textTransform: "uppercase" }}>MAE Distribution</div><Pill tone="red">adverse</Pill></div>
          <div className="panel-bd" style={{ height: 200 }}>
            <ResponsiveContainer>
              <BarChart data={maeData} margin={{ top: 4, right: 8, left: -16, bottom: 0 }}>
                <CartesianGrid stroke="#1f2630" strokeDasharray="2 4" vertical={false} />
                <XAxis dataKey="bucket" stroke="#5a6573" tick={{ fontSize: 10, fontFamily: "JetBrains Mono" }} />
                <YAxis stroke="#5a6573" tick={{ fontSize: 10, fontFamily: "JetBrains Mono" }} />
                <Tooltip contentStyle={{ background: "#0d1117", border: "1px solid #2a3340", fontSize: 12 }} />
                <Bar dataKey="count" fill="#ff5d6c" radius={[2, 2, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      </div>

      {/* BREAKDOWN TABLES */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 18, marginBottom: 18 }}>
        <GroupTable title="By Tier" rows={metrics?.by_tier} keyLabel="tier" />
        <GroupTable title="By Entry Path" rows={metrics?.by_path} keyLabel="path" />
        <GroupTable title="By Session" rows={metrics?.by_session} keyLabel="session" />
        <GroupTable title="By Regime" rows={metrics?.by_regime} keyLabel="regime" />
        <GroupTable title="By Side" rows={metrics?.by_side} keyLabel="side" />
        <GroupTable title="By Symbol (top 25)" rows={metrics?.by_symbol} keyLabel="symbol" />
        <GroupTable title="By Setup Type" rows={metrics?.by_setup_type} keyLabel="setup" />
        <GroupTable title="By Entry Model" rows={metrics?.by_entry_model} keyLabel="model" />
        <GroupTable title="By HTF Bias" rows={metrics?.by_htf_bias} keyLabel="bias" />
        <GroupTable title="By Liquidity Event" rows={metrics?.by_liquidity_event} keyLabel="event" />
      </div>

      {/* SIGNALS TABLE */}
      <div className="panel">
        <div className="panel-hd">
          <div style={{ fontSize: 12, letterSpacing: ".1em", color: "var(--dim)", textTransform: "uppercase" }}>Recent Signals</div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", justifyContent: "flex-end" }}>
            <select className="select" value={status} onChange={(e) => setStatus(e.target.value)} data-testid="filter-status">
              <option value="">all status</option>
              <option>OPEN</option><option>TP1</option><option>TP2</option><option>TP3</option>
              <option>STOPPED</option><option>BE_STOP</option><option>EXPIRED</option>
            </select>
            <select className="select" value={side} onChange={(e) => setSide(e.target.value)} data-testid="filter-side">
              <option value="">all sides</option><option>LONG</option><option>SHORT</option>
            </select>
            <select className="select" value={tier} onChange={(e) => setTier(e.target.value)} data-testid="filter-tier">
              <option value="">all tiers</option><option>S</option><option>A</option><option>B</option><option>C</option>
            </select>
            <select className="select" value={setupType} onChange={(e) => setSetupType(e.target.value)} data-testid="filter-setup-type">
              <option value="">all setups</option>
              <option value="sweep_reclaim">sweep_reclaim</option>
              <option value="fvg_continuation">fvg_continuation</option>
              <option value="ob_reversal">ob_reversal</option>
              <option value="deviation_breakout">deviation_breakout</option>
            </select>
            <select className="select" value={entryModel} onChange={(e) => setEntryModel(e.target.value)} data-testid="filter-entry-model">
              <option value="">all models</option>
              <option value="aggressive">aggressive</option>
              <option value="confirmation">confirmation</option>
              <option value="reclaim">reclaim</option>
            </select>
            <select className="select" value={htfBias} onChange={(e) => setHtfBias(e.target.value)} data-testid="filter-htf-bias">
              <option value="">all htf</option>
              <option value="bull">bull</option>
              <option value="bear">bear</option>
              <option value="neutral">neutral</option>
            </select>
            <select className="select" value={regime} onChange={(e) => setRegime(e.target.value)} data-testid="filter-regime">
              <option value="">all regimes</option>
              <option value="trending">trending</option>
              <option value="ranging">ranging</option>
              <option value="volatile">volatile</option>
              <option value="compressed">compressed</option>
            </select>
          </div>
        </div>
        <div className="panel-bd" style={{ padding: 0 }}>
          <div className="scroll" style={{ maxHeight: 560 }}>
            <table className="t" data-testid="table-signals">
              <thead>
                <tr>
                  <th>Time</th><th>Symbol</th><th>Side</th><th>Tier</th><th>Setup</th><th>Model</th><th>HTF</th><th>Path</th>
                  <th className="r">Entry</th><th className="r">SL</th><th className="r">TP1</th>
                  <th className="r">RR1</th><th>Status</th>
                  <th className="r">MFE</th><th className="r">MAE</th><th className="r">Result</th>
                </tr>
              </thead>
              <tbody>
                {signals.items.map((s) => (
                  <tr key={s.id}>
                    <td className="mono" style={{ color: "var(--dim)" }}>{shortTime(s.created_at)}</td>
                    <td className="mono">{s.symbol}</td>
                    <td><Pill tone={sideTone(s.side)}>{s.side === "LONG" ? <><TrendingUp size={10} style={{ verticalAlign: "middle" }} /> LONG</> : <><TrendingDown size={10} style={{ verticalAlign: "middle" }} /> SHORT</>}</Pill></td>
                    <td><Pill tone={s.tier === "S" ? "amber" : s.tier === "A" ? "aqua" : "dim"}>{s.tier}</Pill></td>
                    <td className="mono pill-dim" style={{ fontSize: 11 }}>{s.setup_type || "—"}</td>
                    <td className="mono pill-dim" style={{ fontSize: 11 }}>{s.entry_model || "—"}</td>
                    <td className="mono" style={{ fontSize: 11, color: s.htf_bias === "bull" ? "var(--green)" : s.htf_bias === "bear" ? "var(--red)" : "var(--dim)" }}>{s.htf_bias || "—"}</td>
                    <td className="mono pill-dim" style={{ fontSize: 11 }}>{s.entry_path || "—"}</td>
                    <td className="r num">{fmt(s.entry, 4)}</td>
                    <td className="r num" style={{ color: "var(--red)" }}>{fmt(s.sl, 4)}</td>
                    <td className="r num" style={{ color: "var(--green)" }}>{fmt(s.tp1, 4)}</td>
                    <td className="r num">{fmt(s.rr1, 2)}</td>
                    <td><span className={`status-${s.status}`}>{s.status}{s.status === "OPEN" && <Clock size={10} style={{ verticalAlign: "middle", marginLeft: 4 }} />}</span></td>
                    <td className="r num" style={{ color: "var(--green)" }}>{fmt(s.max_favorable_r, 2)}</td>
                    <td className="r num" style={{ color: "var(--red)" }}>{fmt(s.max_adverse_r, 2)}</td>
                    <td className="r num" style={{ color: (s.result_r ?? 0) >= 0 ? "var(--green)" : "var(--red)" }}>{s.result_r !== null && s.result_r !== undefined ? fmtR(s.result_r) : "—"}</td>
                  </tr>
                ))}
                {signals.items.length === 0 && (
                  <tr><td colSpan={16} style={{ textAlign: "center", color: "var(--dim)", padding: 36 }}>No signals yet · POST to <span className="mono" style={{ color: "var(--amber)" }}>{API}/signals</span></td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <div style={{ marginTop: 18, textAlign: "center", color: "var(--dim-2)", fontSize: 11 }} className="mono">
        Auto-resolver runs every 15 min · Daily digest at 00:05 UTC · API: {API}
      </div>
    </div>
  );
}
