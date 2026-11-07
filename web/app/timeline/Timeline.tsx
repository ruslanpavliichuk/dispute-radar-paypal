"use client";

import dynamic from "next/dynamic";
import { useEffect, useMemo, useState } from "react";
import { api, getToken } from "../lib";
import Nav from "../Nav";
import { toTimeline, type Proposal } from "./model";

// Bryntum reads `window` when it loads, so it is only ever loaded in the browser
const SchedulerView = dynamic(() => import("./SchedulerView"), { ssr: false, loading: () => <p className="p-4">Loading timeline…</p> });

export default function Timeline() {
  const [token, setToken] = useState("");
  const [rows, setRows] = useState<Proposal[] | null>(null);
  const [err, setErr] = useState("");
  const [picked, setPicked] = useState<string | null>(null);

  // sessionStorage only exists in the browser; read after mount so server and client markup match
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { setToken(getToken()); }, []);
  useEffect(() => {
    if (!token) return;
    const load = () => api<Proposal[]>("/api/proposals").then(setRows).catch((e: Error) => setErr(e.message));
    const first = setTimeout(load, 0);
    const t = setInterval(load, 15000);
    return () => { clearTimeout(first); clearInterval(t); };
  }, [token]);

  const view = useMemo(() => (rows?.length ? toTimeline(rows) : null), [rows]);

  const sel = view?.events.find((e) => e.id === picked);

  return (
    <main className="flex h-screen flex-col bg-white text-neutral-900">
      <Nav title="Deadline timeline" />
      <p className="border-b bg-white px-5 py-2 text-sm text-neutral-600">Blue bar: dispute opened to response due (red once overdue). Dots: what the agent did. Click any bar or dot for details.</p>
      <div className="min-h-0 flex-1">
        {!token ? <p className="p-4">Unlock the <a className="text-blue-700 underline" href="/dashboard">dashboard</a> first: the timeline reads the same proposals.</p>
          : err ? <p role="alert" className="p-4 text-red-700">{err}</p>
          : !rows ? <p className="p-4">Loading…</p>
          : !view ? <p className="p-4">No proposals yet. Open a sandbox dispute and the agent will add it here.</p>
          : <SchedulerView {...view} onPick={setPicked} />}
      </div>
      {sel && (
        <aside role="dialog" aria-label="Timeline detail" className="fixed bottom-6 right-6 z-50 w-80 rounded-2xl border border-neutral-200 bg-white p-4 shadow-xl">
          <button className="float-right text-xl leading-none text-neutral-500" aria-label="Close" onClick={() => setPicked(null)}>×</button>
          <p className="font-semibold text-[#001C64]">{sel.name}</p>
          <p className="text-xs text-neutral-500">{sel.at}</p>
          <p className="mt-2 text-sm">{sel.detail}</p>
        </aside>
      )}
    </main>
  );
}
