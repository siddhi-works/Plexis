import { useEffect, useMemo, useState } from "react";
import type { TraceEvent } from "../mcp";
import { SERVER } from "../mcp";
import { fetchLedger, verifyEntries, type EntryCheck } from "../proof";

type Timed = { name: string; startMs: number; durationMs: number };
export type GraphNode = Timed & { deps: string[]; status?: string };
export type CallRecord = { tool: string; ms: number; serverMs?: number; stages: Timed[]; graph?: GraphNode[]; status: string; at: number };

type Props = {
  trace: TraceEvent[];
  calls: CallRecord[];
  lastData: any;
  ledgerTick: number;
  bookedOutboundId?: string;
  onChaosApplied: (note: string) => void;
};

const TABS = ["Protocol", "Latency", "Proof", "Chaos lab"] as const;

export function Console(props: Props) {
  const [tab, setTab] = useState<(typeof TABS)[number]>("Protocol");
  return (
    <section className="console" aria-label="Developer console">
      <nav className="tabs" role="tablist">
        {TABS.map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? "tab on" : "tab"} onClick={() => setTab(t)}>{t}</button>
        ))}
      </nav>
      <div className="console-body">
        {tab === "Protocol" && <Protocol trace={props.trace} />}
        {tab === "Latency" && <Latency calls={props.calls} />}
        {tab === "Proof" && <Proof data={props.lastData} tick={props.ledgerTick} />}
        {tab === "Chaos lab" && <Chaos bookedOutboundId={props.bookedOutboundId} onApplied={props.onChaosApplied} />}
      </div>
    </section>
  );
}

function Protocol({ trace }: { trace: TraceEvent[] }) {
  const [open, setOpen] = useState<number | null>(null);
  const rows = trace.slice(-120).reverse();
  if (!rows.length) return <p className="hint">JSON-RPC traffic between the simulated Alexa+ host and the add-on appears here.</p>;
  return (
    <ol className="trace">
      {rows.map((e) => (
        <li key={e.seq} className={`trace-row ${e.dir} ${e.kind}`}>
          <button className="trace-head" onClick={() => setOpen(open === e.seq ? null : e.seq)} aria-expanded={open === e.seq}>
            <span className="arrow" aria-label={e.dir === "out" ? "host to add-on" : "add-on to host"}>{e.dir === "out" ? "→" : "←"}</span>
            <span className="m">{e.method ?? (e.kind === "error" ? "error" : "result")}</span>
            <span className="meta">{e.id !== undefined ? `#${e.id}` : ""}{e.ms !== undefined ? ` ${e.ms} ms` : ""}</span>
            <span className="meta">{(e.size / 1024).toFixed(1)} kB</span>
          </button>
          {open === e.seq && <pre className="json">{JSON.stringify(e.body, null, 2).slice(0, 12000)}</pre>}
        </li>
      ))}
    </ol>
  );
}

function Latency({ calls }: { calls: CallRecord[] }) {
  const last = calls[calls.length - 1];
  if (!last) return <p className="hint">Make a request to see where the time goes. Alexa+ asks add-ons for a round trip under 500 ms.</p>;
  const budget = 500;
  const span = Math.max(budget * 1.15, ...last.stages.map((s) => s.startMs + s.durationMs), last.serverMs ?? 0);
  const pct = (ms: number) => `${(100 * ms) / span}%`;
  const within = (last.serverMs ?? last.ms) <= budget;
  return (
    <div className="latency">
      <p className="lat-head">
        <strong>{last.tool}</strong> answered in <strong>{last.serverMs?.toFixed(0) ?? last.ms} ms</strong> on the server
        {" "}({last.ms} ms including the network). <span className={within ? "ok" : "warn"}>{within ? "Inside" : "Over"} the 500 ms budget.</span>
      </p>
      <div className="wf" role="img" aria-label="Stage waterfall">
        <div className="wf-budget" style={{ left: pct(budget) }}><span>500 ms</span></div>
        {last.stages.map((s) => (
          <div key={s.name} className="wf-row">
            <span className="wf-label">{s.name}</span>
            <span className="wf-track"><span className="wf-bar" style={{ left: pct(s.startMs), width: `max(2px, ${pct(s.durationMs)})` }} /></span>
            <span className="wf-ms">{s.durationMs.toFixed(s.durationMs < 10 ? 1 : 0)} ms</span>
          </div>
        ))}
      </div>
      {last.graph && last.graph.length > 0 && <TaskGraph nodes={last.graph} />}
      <h3 className="sub-h">Recent requests</h3>
      <div className="spark" role="img" aria-label="Recent request latencies">
        {calls.slice(-24).map((c, i) => (
          <span key={i} title={`${c.tool}: ${c.serverMs?.toFixed(0) ?? c.ms} ms`} className={(c.serverMs ?? c.ms) <= budget ? "ok" : "warn"}
            style={{ height: `${Math.min(100, ((c.serverMs ?? c.ms) / 1000) * 100)}%` }} />
        ))}
      </div>
    </div>
  );
}

function TaskGraph({ nodes }: { nodes: GraphNode[] }) {
  const end = Math.max(...nodes.map((n) => n.startMs + n.durationMs), 1);
  const start = Math.min(...nodes.map((n) => n.startMs));
  const pct = (ms: number) => `${(100 * (ms - start)) / (end - start || 1)}%`;
  const parallel = nodes.filter((n) => !n.deps.length).length;
  return (
    <>
      <h3 className="sub-h">Task graph</h3>
      <p className="small">{parallel} partner and research lookups run at the same time; solving waits for them, then verification and the cross-check run side by side.</p>
      <div className="graph" role="img" aria-label="Task graph timeline">
        {nodes.map((n) => (
          <div key={n.name} className="wf-row">
            <span className="wf-label" title={n.deps.length ? `after ${n.deps.join(", ")}` : "starts immediately"}>{n.name}</span>
            <span className="wf-track"><span className={`wf-bar ${n.deps.length ? "dep" : "root"} ${n.status === "failed" ? "failed" : ""}`} style={{ left: pct(n.startMs), width: `max(3px, ${(100 * n.durationMs) / (end - start || 1)}%)` }} /></span>
            <span className="wf-ms">{n.durationMs.toFixed(n.durationMs < 10 ? 1 : 0)} ms</span>
          </div>
        ))}
      </div>
    </>
  );
}

function Proof({ data, tick }: { data: any; tick: number }) {
  const [checks, setChecks] = useState<EntryCheck[]>([]);
  const [key, setKey] = useState<{ keyId: string; publicKey: string } | null>(null);
  const [tamper, setTamper] = useState<number | undefined>();
  const [error, setError] = useState("");
  useEffect(() => {
    let alive = true;
    fetchLedger()
      .then(async (l) => {
        const v = await verifyEntries(l.entries, l.publicKey, tamper);
        if (alive) { setChecks(v); setKey({ keyId: l.keyId, publicKey: l.publicKey }); setError(""); }
      })
      .catch((e) => alive && setError(String(e)));
    return () => { alive = false; };
  }, [tick, tamper]);
  const rules: any[] = data?.rules ?? [];
  const proof = data?.proof;
  const allOk = checks.length > 0 && checks.every((c) => c.hashOk && c.linkOk && c.sigOk);
  return (
    <div className="proof-tab">
      {proof ? (
        <div className="cert">
          <p className="cert-line"><span className="tomb" aria-hidden="true">∎</span> {proof.passed} of {proof.checks} checks proven for the current plan by an independent checker, cross-checked by {proof.z3}.</p>
          <p className="small">Solved by {proof.solver} ({proof.solverStatus}). Parsed by {proof.parser}, {Math.round((proof.coverage ?? 1) * 100)}% of words compiled.</p>
          <ul className="rules">
            {rules.filter((r) => ["user", "profile", "default", "policy"].includes(r.origin)).map((r) => (
              <li key={r.id} className={r.passed ? "pass" : "fail"}>
                <span className="mark" aria-label={r.passed ? "proven" : "failed"}>{r.passed ? "✓" : "✗"}</span>
                <span>{r.text}{r.words ? <q>{r.words}</q> : null}{r.because ? <em> because {r.because}</em> : null}<br /><small>{r.evidence}</small></span>
              </li>
            ))}
          </ul>
        </div>
      ) : <p className="hint">Plan a trip to see its proof.</p>}
      <h3 className="sub-h">Ledger, verified in this browser</h3>
      {error && <p className="warn">{error}</p>}
      <p className="small">
        Each receipt's hash is recomputed with WebCrypto, linked to the one before it, and its Ed25519 signature is checked against key {key?.keyId ?? "…"}.
        {" "}{allOk ? "Every entry checks out." : checks.length ? "Something doesn't verify." : ""}
      </p>
      <table className="ledger">
        <thead><tr><th>#</th><th>Kind</th><th>Hash</th><th>Chain</th><th>Signature</th><th /></tr></thead>
        <tbody>
          {checks.slice().reverse().map((c) => (
            <tr key={c.entry.index}>
              <td>{c.entry.index}</td><td>{c.entry.kind}</td>
              <td className={c.hashOk ? "ok" : "bad"}>{c.hashOk ? "✓" : "✗"} <code>{c.entry.certHash.slice(0, 10)}</code></td>
              <td className={c.linkOk ? "ok" : "bad"}>{c.linkOk ? "✓ linked" : "✗ broken"}</td>
              <td className={c.sigOk ? "ok" : "bad"}>{c.sigOk ? "✓ Ed25519" : "✗ invalid"}</td>
              <td><button className="link" onClick={() => setTamper(tamper === c.entry.index ? undefined : c.entry.index)}>{tamper === c.entry.index ? "Undo tamper" : "Tamper"}</button></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const PARTNERS = ["transport", "stays", "dining", "experiences", "weather", "wallet"] as const;

function Chaos({ bookedOutboundId, onApplied }: { bookedOutboundId?: string; onApplied: (note: string) => void }) {
  const [cfg, setCfg] = useState<Record<string, { latency_ms: number; error_rate: number }>>({});
  const [failConfirm, setFailConfirm] = useState<string[]>([]);
  const send = async (body: Record<string, unknown>, note: string) => {
    const r = await fetch(`${SERVER}/dev/chaos`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json();
    setCfg(j.config ?? {});
    setFailConfirm(j.failConfirm ?? []);
    onApplied(note);
  };
  const current = useMemo(() => cfg, [cfg]);
  return (
    <div className="chaos">
      <p className="hint">Break the partners on purpose and watch Plexis degrade, retry, roll back or re-plan, without ever presenting an unverified plan.</p>
      <table className="chaos-t">
        <thead><tr><th>Partner</th><th>Added latency</th><th>Errors</th><th>Reject confirmations</th></tr></thead>
        <tbody>
          {PARTNERS.map((p) => {
            const c = current[p] ?? { latency_ms: 0, error_rate: 0 };
            return (
              <tr key={p}>
                <td>{p}</td>
                <td>
                  <select aria-label={`${p} latency`} value={c.latency_ms} onChange={(e) => send({ partner: p, latency_ms: Number(e.target.value), error_rate: c.error_rate }, `${p}: +${e.target.value} ms`)}>
                    {[0, 150, 400, 900, 1600].map((v) => <option key={v} value={v}>{v === 0 ? "none" : `+${v} ms`}</option>)}
                  </select>
                </td>
                <td>
                  <select aria-label={`${p} error rate`} value={c.error_rate} onChange={(e) => send({ partner: p, latency_ms: c.latency_ms, error_rate: Number(e.target.value) }, `${p}: ${Number(e.target.value) * 100}% errors`)}>
                    {[0, 0.3, 1].map((v) => <option key={v} value={v}>{v === 0 ? "none" : v === 1 ? "always down" : "30%"}</option>)}
                  </select>
                </td>
                <td>
                  {["stays", "wallet", "dining"].includes(p) && (
                    <input type="checkbox" aria-label={`${p} rejects confirmations`} checked={failConfirm.includes(p)}
                      onChange={(e) => send({ partner: p, latency_ms: c.latency_ms, error_rate: c.error_rate, fail_confirm: e.target.checked }, `${p} ${e.target.checked ? "rejects" : "accepts"} confirmations`)} />
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className="chaos-actions">
        <button className="btn btn-danger" disabled={!bookedOutboundId} onClick={() => send({ cancel: bookedOutboundId }, "outbound flight cancelled by carrier")}>
          Cancel my booked outbound flight
        </button>
        <button className="btn" onClick={() => send({ reset: true }, "partners restored")}>Restore all partners</button>
      </div>
      {!bookedOutboundId && <p className="small">Book a trip first to test disruption handling.</p>}
    </div>
  );
}
