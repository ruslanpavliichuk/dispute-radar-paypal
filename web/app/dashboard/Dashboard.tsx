"use client";

import { useCallback, useEffect, useState } from "react";
import { AllCommunityModule, ModuleRegistry, themeQuartz, type ColDef, type ICellRendererParams } from "ag-grid-community";
import { AgGridReact } from "ag-grid-react";
import { api, getToken, gbp, tokenKey } from "../lib";

ModuleRegistry.registerModules([AllCommunityModule]);

type Reason = { feature?: string; text?: string; value?: unknown; contribution: number };
type Proposal = {
  id: number; agent: "response" | "prevention"; subject_id: string; status: string; action: string;
  amount_minor: number | null; currency: string | null; claim_type: string | null; win_prob: number | null;
  ev: Record<string, number> | null; defaulted: string[] | null; reasons: Reason[] | null;
  evidence: unknown; draft: string | null; draft_source: string | null; created_at: string; error: string | null;
};
type Attention = { paypal_order_id: string; status: string; amount_minor: number; currency: string; created_at: string };

// risk for prevention rows (stored in ev.risk), win chance for dispute rows
const score = (p: Proposal) => (p.agent === "prevention" ? p.ev?.risk ?? null : p.win_prob);

function ScoreBar({ value, bad }: { value: number | null; bad: boolean }) {
  if (value == null) return null;
  const pct = Math.round(value * 100);
  const hue = bad ? 0 : 140; // red for risk, green for win chance
  return (
    <div className="flex h-full items-center gap-2" aria-label={`${pct} percent`}>
      <div className="h-2 w-16 rounded bg-neutral-200">
        <div className="h-2 rounded" style={{ width: `${pct}%`, background: `hsl(${hue} 65% 42%)` }} />
      </div>
      <span className="tabular-nums">{pct}%</span>
    </div>
  );
}

const reasonText = (r: Reason) => `${r.feature ?? r.text} ${r.contribution >= 0 ? "+" : ""}${r.contribution.toFixed(2)}`;
const topReasons = (p: Proposal) => (p.reasons ?? []).slice(0, 2).map(reasonText).join(", ");

function cols(kind: "prevention" | "response"): ColDef<Proposal>[] {
  const risk = kind === "prevention";
  return [
    { field: "subject_id", headerName: risk ? "Order" : "Dispute", minWidth: 150 },
    { field: "status", width: 110 },
    { field: "action", width: 150 },
    { headerName: risk ? "Dispute risk" : "Win chance", width: 150, valueGetter: (p) => (p.data ? score(p.data) : null),
      cellRenderer: (p: ICellRendererParams<Proposal>) => <ScoreBar value={p.value} bad={risk} /> },
    { headerName: "Why (SHAP)", flex: 1, minWidth: 200, valueGetter: (p) => (p.data ? topReasons(p.data) : "") },
    { headerName: "Amount", width: 110, valueGetter: (p) => gbp(p.data?.amount_minor ?? null, p.data?.currency ?? "GBP") },
    { field: "created_at", headerName: "Created", width: 170, valueFormatter: (p) => new Date(p.value).toLocaleString() },
  ];
}

const theme = themeQuartz;

export default function Dashboard() {
  const [token, setToken] = useState("");
  const [rows, setRows] = useState<Proposal[]>([]);
  const [attention, setAttention] = useState<Attention[]>([]);
  const [open, setOpen] = useState<Proposal | null>(null);
  const [note, setNote] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [p, a] = await Promise.all([api<Proposal[]>("/api/proposals"), api<Attention[]>("/api/orders/attention")]);
      setRows(p); setAttention(a); setMsg("");
    } catch (e) { setMsg((e as Error).message); }
  }, []);

  // sessionStorage only exists in the browser, so it is read after mount to keep server and client markup equal
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { setToken(getToken()); }, []);
  useEffect(() => {
    if (!token) return;
    const first = setTimeout(load, 0);
    const t = setInterval(load, 10000);
    return () => { clearTimeout(first); clearInterval(t); };
  }, [token, load]);

  const select = (p: Proposal) => { setOpen(p); setNote(p.draft ?? ""); setMsg(""); };
  const decide = async (what: "approve" | "reject") => {
    if (!open) return;
    setBusy(true);
    try {
      const edited = what === "approve" && note !== (open.draft ?? "");
      const updated = await api<Proposal>(`/api/proposals/${open.id}/${what}`, {
        method: "POST", body: edited ? JSON.stringify({ note }) : "{}" });
      setOpen(updated); setMsg(`${what === "approve" ? "Approved" : "Rejected"}: status ${updated.status}`);
      await load();
    } catch (e) { setMsg((e as Error).message); } finally { setBusy(false); }
  };

  const grid = (kind: "prevention" | "response", title: string) => (
    <section className="mb-8">
      <h2 className="mb-2 text-lg font-semibold">{title}</h2>
      <div style={{ height: 280 }}>
        <AgGridReact<Proposal> theme={theme} rowData={rows.filter((r) => r.agent === kind)} columnDefs={cols(kind)}
          getRowId={(p) => String(p.data.id)} rowSelection={{ mode: "singleRow", checkboxes: false, enableClickSelection: true }}
          onRowClicked={(e) => e.data && select(e.data)} overlayNoRowsTemplate="Nothing proposed yet" />
      </div>
    </section>
  );

  return (
    <main className="min-h-screen bg-white p-6 text-neutral-900">
      <header className="mb-6 flex flex-wrap items-center gap-4">
        <h1 className="text-2xl font-bold">Seller dashboard</h1>
        <a className="text-blue-700 underline" href="/evaluation">Evaluation</a>
        <form className="ml-auto flex gap-2" onSubmit={(e) => { e.preventDefault(); try { sessionStorage.setItem(tokenKey, (new FormData(e.currentTarget).get("t") as string) ?? ""); } catch {} setToken(getToken()); }}>
          <input name="t" type="password" placeholder="Admin token" aria-label="Admin token" className="rounded border px-2 py-1" />
          <button className="rounded bg-neutral-900 px-3 py-1 text-white">Unlock</button>
        </form>
      </header>
      {!token ? <p>Enter the admin token to see proposals.</p> : (
        <>
          {msg && !open && <p role="alert" className="mb-3 text-red-700">{msg}</p>}
          {grid("prevention", "Orders: prevention proposals")}
          {grid("response", "Disputes: response proposals")}
          <section>
            <h2 className="mb-2 text-lg font-semibold">Orders needing attention</h2>
            <div style={{ height: 200 }}>
              <AgGridReact<Attention> theme={theme} rowData={attention} overlayNoRowsTemplate="None"
                columnDefs={[{ field: "paypal_order_id", headerName: "Order", flex: 1 }, { field: "status" },
                  { headerName: "Amount", valueGetter: (p) => gbp(p.data?.amount_minor ?? null, p.data?.currency) },
                  { field: "created_at", headerName: "Created", valueFormatter: (p) => new Date(p.value).toLocaleString() }]} />
            </div>
          </section>
        </>
      )}
      {open && (
        <aside role="dialog" aria-label="Proposal" className="fixed inset-y-0 right-0 w-full max-w-md overflow-y-auto border-l bg-white p-5 shadow-xl">
          <button className="float-right text-xl" aria-label="Close" onClick={() => setOpen(null)}>×</button>
          <h2 className="text-lg font-semibold">{open.agent === "prevention" ? "Order" : "Dispute"} {open.subject_id}</h2>
          <p className="mb-3 text-sm text-neutral-600">{open.action} · {open.status}{open.amount_minor != null && ` · ${gbp(open.amount_minor, open.currency ?? "GBP")}`}</p>
          <ScoreBar value={score(open)} bad={open.agent === "prevention"} />
          {open.ev && <p className="mt-2 text-sm">{open.agent === "prevention"
            ? `Expected loss if nothing is done: ${gbp(open.ev.expected_loss_minor, open.currency ?? "GBP")}`
            : `Expected value: fight ${gbp(open.ev.fight, open.currency ?? "GBP")} · offer ${gbp(open.ev.offer, open.currency ?? "GBP")} · accept ${gbp(open.ev.accept, open.currency ?? "GBP")}`}</p>}
          <h3 className="mt-4 font-medium">Why</h3>
          <ul className="list-disc pl-5 text-sm">{(open.reasons ?? []).map((r, i) => <li key={i}>{reasonText(r)}</li>)}</ul>
          {!!open.defaulted?.length && <p className="mt-2 text-sm text-amber-700">Guessed (no data): {open.defaulted.join(", ")}</p>}
          <h3 className="mt-4 font-medium">Draft ({open.draft_source ?? "none"})</h3>
          <textarea className="mt-1 h-40 w-full rounded border p-2 text-sm" value={note} onChange={(e) => setNote(e.target.value)} disabled={open.status !== "PROPOSED"} aria-label="Draft text" />
          {msg && <p role="alert" className="mt-2 text-sm">{msg}</p>}
          {open.error && <p className="mt-2 text-sm text-red-700">{open.error}</p>}
          <div className="mt-4 flex gap-3">
            <button disabled={busy || open.status !== "PROPOSED"} onClick={() => decide("approve")} className="rounded bg-green-700 px-4 py-2 text-white disabled:opacity-40">Approve and send</button>
            <button disabled={busy || open.status !== "PROPOSED"} onClick={() => decide("reject")} className="rounded border px-4 py-2 disabled:opacity-40">Reject</button>
          </div>
        </aside>
      )}
    </main>
  );
}
