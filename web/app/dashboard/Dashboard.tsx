"use client";

import { useCallback, useEffect, useState } from "react";
import { AllCommunityModule, ModuleRegistry, type ColDef, type GridApi, type ICellRendererParams } from "ag-grid-community";
import { AgGridReact } from "ag-grid-react";
import { AgCharts } from "ag-charts-react";
import { AllCommunityModule as ChartsModule, ModuleRegistry as ChartsRegistry } from "ag-charts-community";
import { api, getToken, gbp, tokenKey } from "../lib";
import Nav from "../Nav";
import { brand, chartTheme, gridTheme, STATUS_CHIP } from "../theme";

ModuleRegistry.registerModules([AllCommunityModule]);
ChartsRegistry.registerModules([ChartsModule]);

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
  return (
    <div className="flex h-full items-center gap-2" aria-label={`${pct} percent`}>
      <div className="h-1.5 w-16 rounded-full bg-[#E5E9F0]">
        <div className="h-1.5 rounded-full" style={{ width: `${pct}%`, background: bad ? brand.red : brand.green }} />
      </div>
      <span className="tabular-nums text-[#001C64]">{pct}%</span>
    </div>
  );
}

function StatusBadge({ value }: { value: string }) {
  const c = STATUS_CHIP[value] ?? STATUS_CHIP.REJECTED;
  return <span className="inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-semibold" style={{ background: c.bg, color: c.fg }}>
    <span aria-hidden className="h-1.5 w-1.5 rounded-full" style={{ background: c.fg }} />{value.charAt(0) + value.slice(1).toLowerCase()}</span>;
}

const reasonText = (r: Reason) => `${r.feature ?? r.text} ${r.contribution >= 0 ? "+" : ""}${r.contribution.toFixed(2)}`;
const topReasons = (p: Proposal) => (p.reasons ?? []).slice(0, 2).map(reasonText).join(", ");

function cols(kind: "prevention" | "response"): ColDef<Proposal>[] {
  const risk = kind === "prevention";
  return [
    { field: "subject_id", headerName: risk ? "Order" : "Dispute", minWidth: 150 },
    { field: "status", width: 130, cellRenderer: (p: ICellRendererParams<Proposal>) => <StatusBadge value={p.value} />, filter: true },
    { field: "action", width: 150 },
    { headerName: risk ? "Dispute risk" : "Win chance", width: 150, valueGetter: (p) => (p.data ? score(p.data) : null), filter: "agNumberColumnFilter", sort: "desc" as const,
      tooltipValueGetter: (p) => (p.data?.reasons ?? []).map(reasonText).join("\n") || "No reasons stored",
      cellRenderer: (p: ICellRendererParams<Proposal>) => <ScoreBar value={p.value} bad={risk} /> },
    { headerName: "Why (SHAP)", flex: 1, minWidth: 200, tooltipValueGetter: (p) => (p.data?.reasons ?? []).map(reasonText).join("\n"), valueGetter: (p) => (p.data ? topReasons(p.data) : "") },
    { headerName: "Amount", width: 110, valueGetter: (p) => gbp(p.data?.amount_minor ?? null, p.data?.currency ?? "GBP") },
    { field: "created_at", headerName: "Created", width: 190, valueFormatter: (p) => new Date(p.value).toLocaleString() },
  ];
}

const STATE_KEY = "dr_grid_state";
const saveState = (kind: string, api: GridApi) => { try { localStorage.setItem(`${STATE_KEY}_${kind}`, JSON.stringify(api.getColumnState())); } catch {} };
const restoreState = (kind: string, api: GridApi) => {
  try { const raw = localStorage.getItem(`${STATE_KEY}_${kind}`); if (raw) api.applyColumnState({ state: JSON.parse(raw), applyOrder: true }); } catch {}
};

function Stat({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="px-6 py-5">
      <div className="text-sm text-[#5C6B8A]">{label}</div>
      <div className="mt-1 text-3xl font-semibold tabular-nums tracking-tight" style={{ color: tone ?? brand.ink }}>{value}</div>
    </div>
  );
}

const Card = ({ title, aside, children }: { title: string; aside?: React.ReactNode; children: React.ReactNode }) => (
  <section className="mb-6 overflow-hidden rounded-2xl border border-[#E5E9F0] bg-white">
    <div className="flex items-center justify-between px-6 pt-5 pb-2">
      <h2 className="text-lg font-semibold text-[#001C64]">{title}</h2>{aside}
    </div>
    {children}
  </section>
);

// the drawer chart: what each choice keeps, so the seller sees why the agent leans one way
function EvChart({ p }: { p: Proposal }) {
  if (p.agent !== "response" || !p.ev) return null;
  const cur = p.currency ?? "GBP";
  const data = (["fight", "offer", "accept"] as const).map((k) => ({ action: k, kept: (p.ev?.[k] ?? 0) / 100 }));
  return (
    <div style={{ height: 180 }} className="mt-2">
      <AgCharts options={{ theme: chartTheme, data, legend: { enabled: false }, title: { text: "Value kept per choice" },
        series: [{ type: "bar", xKey: "action", yKey: "kept", tooltip: { renderer: ({ datum }) => ({ title: String(datum.action), data: [{ label: "Kept", value: gbp(Math.round(datum.kept * 100), cur) }] }) } }] }} />
    </div>
  );
}

export default function Dashboard() {
  const [token, setToken] = useState("");
  const [rows, setRows] = useState<Proposal[]>([]);
  const [attention, setAttention] = useState<Attention[]>([]);
  const [open, setOpen] = useState<Proposal | null>(null);
  const [note, setNote] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [quick, setQuick] = useState("");
  const [onlyPending, setOnlyPending] = useState(false);

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

  const shown = (kind: "prevention" | "response") => rows.filter((r) => r.agent === kind && (!onlyPending || r.status === "PROPOSED"));
  const pending = rows.filter((r) => r.status === "PROPOSED");
  const atRisk = pending.filter((r) => r.agent === "prevention").reduce((t, r) => t + (r.ev?.expected_loss_minor ?? 0), 0);

  const grid = (kind: "prevention" | "response", title: string) => (
    <Card title={title} aside={<span className="text-sm text-[#5C6B8A]">{shown(kind).length} items</span>}>
      <div style={{ height: Math.min(Math.max(shown(kind).length, 2), 8) * 45 + 60 }}>
        <AgGridReact<Proposal> theme={gridTheme} rowData={shown(kind)} columnDefs={cols(kind)} quickFilterText={quick} tooltipShowDelay={300}
          onGridReady={(e) => restoreState(kind, e.api)} onColumnMoved={(e) => saveState(kind, e.api)} onColumnResized={(e) => e.finished && saveState(kind, e.api)} onSortChanged={(e) => saveState(kind, e.api)}
          getRowId={(p) => String(p.data.id)} rowSelection={{ mode: "singleRow", checkboxes: false, enableClickSelection: true }}
          onRowClicked={(e) => e.data && select(e.data)} overlayNoRowsTemplate="Nothing proposed yet" />
      </div>
    </Card>
  );

  const what = (p: Proposal) => ({ goodwill_refund: "Offer a goodwill refund", upload_tracking: "Upload tracking to PayPal", make_offer: "Offer a partial refund", accept_claim: "Accept the claim",
    provide_evidence: "Send evidence", send_message: "Message the buyer", fight: "Fight the dispute" } as Record<string, string>)[p.action] ?? p.action;

  return (
    <main className="min-h-screen bg-[#F5F7FA] text-[#001C64]">
      <Nav title="Seller dashboard">
        <form className="flex items-center gap-2" onSubmit={(e) => { e.preventDefault(); try { sessionStorage.setItem(tokenKey, ((new FormData(e.currentTarget).get("t") as string) ?? "").trim()); } catch {} setToken(getToken()); }}>
          <input name="t" type="password" placeholder="Admin token" aria-label="Admin token" className="w-40 rounded-full border border-[#9AA5B8] bg-white px-4 py-1.5 text-sm text-[#001C64] outline-none placeholder:text-[#5C6B8A] focus:border-[#0070E0] focus:ring-2 focus:ring-[#0070E0]/30" />
          <button className="rounded-full bg-[#0070E0] px-5 py-1.5 text-sm font-semibold text-white transition-colors hover:bg-[#003087]">Unlock</button>
        </form>
      </Nav>
      <div className="mx-auto max-w-6xl px-6 py-8">
      {!token ? <p className="rounded-2xl border border-[#E5E9F0] bg-white p-6">Enter the admin token to see your proposals.</p> : (
        <>
          <h1 className="mb-1 text-3xl font-semibold tracking-tight">Dispute activity</h1>
          <p className="mb-6 text-[#5C6B8A]">What the agents found and what needs your approval.</p>
          {msg && !open && <p role="alert" className="mb-4 rounded-xl bg-[#FBE1E3] px-4 py-3 text-[#8F1521]">{msg}</p>}
          <div className="mb-6 grid grid-cols-2 divide-x divide-y divide-[#E5E9F0] overflow-hidden rounded-2xl border border-[#E5E9F0] bg-white md:grid-cols-4 md:divide-y-0">
            <Stat label="Waiting for you" value={String(pending.length)} tone={pending.length ? "#7A4B00" : undefined} />
            <Stat label="Expected loss at stake" value={gbp(atRisk)} tone={atRisk ? brand.red : undefined} />
            <Stat label="Sent to PayPal" value={String(rows.filter((r) => r.status === "SENT").length)} tone={brand.green} />
            <Stat label="Orders needing attention" value={String(attention.length)} tone={attention.length ? brand.red : undefined} />
          </div>
          <Card title="Needs your approval">
            {pending.length === 0 ? <p className="px-6 pb-6 text-[#5C6B8A]">You are all caught up.</p> : (
              <ul>{pending.slice(0, 5).map((p) => (
                <li key={p.id} className="flex items-center gap-4 border-t border-[#E5E9F0] px-6 py-3">
                  <span aria-hidden className="grid h-10 w-10 shrink-0 place-items-center rounded-full bg-[#DCEBFC] text-sm font-bold text-[#003087]">{p.agent === "prevention" ? "O" : "D"}</span>
                  <div className="min-w-0 flex-1">
                    <div className="truncate font-medium">{what(p)}</div>
                    <div className="truncate text-sm text-[#5C6B8A]">{p.agent === "prevention" ? "Order" : "Dispute"} {p.subject_id.split(":")[0]} · {new Date(p.created_at).toLocaleDateString()}</div>
                  </div>
                  {p.amount_minor != null && <span className="font-semibold tabular-nums">{gbp(p.amount_minor, p.currency ?? "GBP")}</span>}
                  <button onClick={() => select(p)} className="rounded-full border border-[#0070E0] px-4 py-1.5 text-sm font-semibold text-[#0070E0] transition-colors hover:bg-[#F0F6FE]">Review</button>
                </li>))}</ul>)}
          </Card>
          <div className="mb-4 flex flex-wrap items-center gap-4">
            <input value={quick} onChange={(e) => setQuick(e.target.value)} placeholder="Search proposals" aria-label="Search proposals" className="w-64 rounded-full border border-[#9AA5B8] bg-white px-4 py-2 text-sm outline-none placeholder:text-[#5C6B8A] focus:border-[#0070E0] focus:ring-2 focus:ring-[#0070E0]/30" />
            <label className="flex cursor-pointer items-center gap-2 text-sm"><input type="checkbox" className="h-4 w-4 accent-[#0070E0]" checked={onlyPending} onChange={(e) => setOnlyPending(e.target.checked)} /> Needs my approval only</label>
          </div>
          {grid("prevention", "Orders: prevention proposals")}
          {grid("response", "Disputes: response proposals")}
          <Card title="Orders needing attention">
            <div style={{ height: 200 }}>
              <AgGridReact<Attention> theme={gridTheme} rowData={attention} overlayNoRowsTemplate="None"
                columnDefs={[{ field: "paypal_order_id", headerName: "Order", flex: 1 }, { field: "status" },
                  { headerName: "Amount", valueGetter: (p) => gbp(p.data?.amount_minor ?? null, p.data?.currency) },
                  { field: "created_at", headerName: "Created", valueFormatter: (p) => new Date(p.value).toLocaleString() }]} />
            </div>
          </Card>
        </>
      )}
      </div>
      {open && (<>
        <div className="fixed inset-0 z-40 bg-[#001C64]/40" onClick={() => setOpen(null)} aria-hidden />
        <aside role="dialog" aria-label="Proposal" className="fixed inset-y-0 right-0 z-50 w-full max-w-md overflow-y-auto rounded-l-2xl bg-white p-6 shadow-2xl">
          <button className="float-right grid h-9 w-9 place-items-center rounded-full text-2xl leading-none text-[#5C6B8A] hover:bg-[#F5F7FA]" aria-label="Close" onClick={() => setOpen(null)}>×</button>
          <StatusBadge value={open.status} />
          <h2 className="mt-3 text-xl font-semibold">{what(open)}</h2>
          <p className="mb-4 text-sm text-[#5C6B8A]">{open.agent === "prevention" ? "Order" : "Dispute"} {open.subject_id}{open.amount_minor != null && ` · ${gbp(open.amount_minor, open.currency ?? "GBP")}`}</p>
          <div className="rounded-xl bg-[#F5F7FA] p-4">
            <div className="mb-1 text-sm text-[#5C6B8A]">{open.agent === "prevention" ? "Dispute risk" : "Win chance"}</div>
            <ScoreBar value={score(open)} bad={open.agent === "prevention"} />
            {open.ev && <p className="mt-3 text-sm">{open.agent === "prevention"
              ? `Expected loss if nothing is done: ${gbp(open.ev.expected_loss_minor, open.currency ?? "GBP")}`
              : `Kept if you fight ${gbp(open.ev.fight, open.currency ?? "GBP")}, offer ${gbp(open.ev.offer, open.currency ?? "GBP")}, accept ${gbp(open.ev.accept, open.currency ?? "GBP")}`}</p>}
          </div>
          <EvChart p={open} />
          <h3 className="mt-5 font-semibold">Why the agent suggests this</h3>
          <ul className="mt-1 space-y-1 text-sm">{(open.reasons ?? []).map((r, i) => <li key={i} className="flex justify-between border-b border-[#E5E9F0] py-1"><span>{r.feature ?? r.text}</span><span className="tabular-nums text-[#5C6B8A]">{r.contribution >= 0 ? "+" : ""}{r.contribution.toFixed(2)}</span></li>)}</ul>
          {!!open.defaulted?.length && <p className="mt-2 rounded-lg bg-[#FFF1D6] px-3 py-2 text-sm text-[#7A4B00]">Guessed (no data): {open.defaulted.join(", ")}</p>}
          <h3 className="mt-5 font-semibold">Message <span className="font-normal text-[#5C6B8A]">({open.draft_source ?? "none"})</span></h3>
          <textarea className="mt-1 h-40 w-full rounded-xl border border-[#9AA5B8] p-3 text-sm outline-none focus:border-[#0070E0] focus:ring-2 focus:ring-[#0070E0]/30 disabled:bg-[#F5F7FA]" value={note} onChange={(e) => setNote(e.target.value)} disabled={open.status !== "PROPOSED"} aria-label="Draft text" />
          {msg && <p role="alert" className="mt-2 text-sm">{msg}</p>}
          {open.error && <p className="mt-2 rounded-lg bg-[#FBE1E3] px-3 py-2 text-sm text-[#8F1521]">{open.error}</p>}
          <div className="mt-5 flex gap-3">
            <button disabled={busy || open.status !== "PROPOSED"} onClick={() => decide("approve")} className="rounded-full bg-[#0070E0] px-6 py-2.5 font-semibold text-white transition-colors hover:bg-[#003087] disabled:cursor-not-allowed disabled:opacity-40">Approve and send</button>
            <button disabled={busy || open.status !== "PROPOSED"} onClick={() => decide("reject")} className="rounded-full border border-[#0070E0] px-6 py-2.5 font-semibold text-[#0070E0] transition-colors hover:bg-[#F0F6FE] disabled:cursor-not-allowed disabled:opacity-40">Reject</button>
          </div>
        </aside></>
      )}
    </main>
  );
}
