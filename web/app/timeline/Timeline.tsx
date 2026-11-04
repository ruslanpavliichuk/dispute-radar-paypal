"use client";

import dynamic from "next/dynamic";
import { useEffect, useMemo, useState } from "react";
import { api, getToken } from "../lib";
import { toTimeline, type Proposal } from "./model";

// Bryntum reads `window` when it loads, so it is only ever loaded in the browser
const SchedulerView = dynamic(() => import("./SchedulerView"), { ssr: false, loading: () => <p className="p-4">Loading timeline…</p> });

export default function Timeline() {
  const [token, setToken] = useState("");
  const [rows, setRows] = useState<Proposal[] | null>(null);
  const [err, setErr] = useState("");

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

  return (
    <main className="flex h-screen flex-col bg-white text-neutral-900">
      <header className="flex flex-wrap items-center gap-4 border-b p-3">
        <h1 className="text-lg font-bold">Deadline timeline</h1>
        <a className="text-blue-700 underline" href="/dashboard">Dashboard</a>
        <a className="text-blue-700 underline" href="/evaluation">Evaluation</a>
        <a className="text-blue-700 underline" href="/studio">Studio</a>
        <span className="text-sm text-neutral-600">Blue bar: dispute opened to response due (red once overdue). Points: what the agent did.</span>
      </header>
      <div className="min-h-0 flex-1">
        {!token ? <p className="p-4">Unlock the <a className="text-blue-700 underline" href="/dashboard">dashboard</a> first: the timeline reads the same proposals.</p>
          : err ? <p role="alert" className="p-4 text-red-700">{err}</p>
          : !rows ? <p className="p-4">Loading…</p>
          : !view ? <p className="p-4">No proposals yet. Open a sandbox dispute and the agent will add it here.</p>
          : <SchedulerView {...view} />}
      </div>
    </main>
  );
}
