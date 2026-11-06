"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { AgStudioAiModule, createAiHarness, type AgStudioApi } from "ag-studio";
import { AgStudio, AgStudioProvider } from "ag-studio-react";
import Nav from "../Nav";
import { api, getToken } from "../lib";
import { radarAgent, type Row } from "./radar";

type Reason = { feature?: string; text?: string; contribution: number };
type Proposal = {
  id: number; agent: string; subject_id: string; status: string; action: string; amount_minor: number | null;
  win_prob: number | null; ev: Record<string, number> | null; reasons: Reason[] | null; created_at: string; draft_source: string | null;
};
type Evaluation = { risk: { calibration: Cal[] }; outcome: { calibration: Cal[] }; benchmark: { runs: Record<string, { calibration: Record<string, Cal[]> }> } };
type Cal = { mean_pred: number; obs_rate: number; n: number };

const toRow = (p: Proposal): Row => ({
  id: p.id, agent: p.agent, subject: p.subject_id, status: p.status, action: p.action,
  amount: (p.amount_minor ?? p.ev?.value_minor ?? 0) / 100,
  score: (p.agent === "prevention" ? p.ev?.risk : p.win_prob) ?? 0,
  why: (p.reasons ?? []).slice(0, 2).map((r) => `${r.feature ?? r.text} ${r.contribution >= 0 ? "+" : ""}${r.contribution.toFixed(2)}`).join(", "),
  created_day: p.created_at.slice(0, 10), draft_source: p.draft_source ?? "none",
});

export default function Studio() {
  const [rows, setRows] = useState<Row[] | null>(null);
  const [cal, setCal] = useState<Record<string, number | string>[]>([]);
  const [err, setErr] = useState("");
  const [token, setToken] = useState("");
  const rowsRef = useRef<Row[]>([]);

  // sessionStorage only exists in the browser; read after mount so server and client markup match
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { setToken(getToken()); }, []);
  useEffect(() => {
    if (!token) return;
    Promise.all([api<Proposal[]>("/api/proposals"), api<Evaluation>("/api/evaluation")])
      .then(([p, e]) => {
        const r = p.map(toRow);
        rowsRef.current = r; setRows(r);
        const bench = Object.values(e.benchmark.runs)[0].calibration;
        const rowsOf = (model: string, pts: Cal[]) => pts.map((c) => ({ model, predicted: c.mean_pred, observed: c.obs_rate, cases: c.n }));
        setCal([...rowsOf("Dispute risk model", e.risk.calibration), ...rowsOf("Win chance model", e.outcome.calibration), ...rowsOf("LLM guess", bench.llm)]);
      })
      .catch((x: Error) => setErr(x.message));
  }, [token]);

  const data = useMemo(() => rows && ({ sources: [
    { id: "proposals", name: "Proposals", data: rows },
    { id: "calibration", name: "Calibration", data: cal },
  ] }), [rows, cal]);

  if (!token) return <main className="min-h-screen bg-white p-6">Unlock the <a className="text-blue-700 underline" href="/dashboard">dashboard</a> first: Studio reads the same proposals.</main>;
  if (err) return <main className="min-h-screen bg-white p-6 text-red-700" role="alert">{err}</main>;
  if (!data) return <main className="min-h-screen bg-white p-6">Loading…</main>;

  return (
    <main className="flex h-screen flex-col bg-white text-neutral-900">
      <Nav title="Studio" />
      <p className="border-b bg-white px-5 py-2 text-sm text-neutral-600">Drag fields onto the canvas, or ask Radar in the assistant panel.</p>
      {/* AG Studio fills its parent, so the parent needs a definite size: absolute inside a relative flex item */}
      <div className="relative min-h-0 flex-1">
       <div className="absolute inset-0">
        <AgStudioProvider licenseKey={process.env.NEXT_PUBLIC_AG_STUDIO_LICENSE ?? ""} modules={[AgStudioAiModule]}>
          <AgStudio data={data} mode="edit"
            // shortcut: ag-studio 3.1.0 types this callback with the DOM-free core API; in the browser it is the full AgStudioApi. Drop the cast when the typings agree.
            ai={({ api: studio }) => createAiHarness(studio as unknown as AgStudioApi, () => ({
              agents: [radarAgent(() => rowsRef.current)], primary: "radar",
              promptStarters: [{ label: "What is waiting?", prompt: "What is waiting for me?" }, { label: "Money at stake", prompt: "How much money is at stake?" }, { label: "Riskiest orders", prompt: "Which orders are riskiest?" }],
            }))} />
        </AgStudioProvider>
       </div>
      </div>
    </main>
  );
}
