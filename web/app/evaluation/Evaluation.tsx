"use client";

import { useEffect, useState } from "react";
import { AgCharts } from "ag-charts-react";
import { AllCommunityModule, ModuleRegistry } from "ag-charts-community";
import type { AgCartesianChartOptions } from "ag-charts-community";

ModuleRegistry.registerModules([AllCommunityModule]);

type Point = { mean_pred: number; obs_rate: number; n: number };
type Metrics = Record<string, number>;
type Stage = { by_threshold: { threshold: number; precision: number; recall: number }[] };
type Run = { provider: string; model: string; metrics: Record<string, Metrics>; calibration: Record<string, Point[]>;
  brier_diff_llm_minus_model: { mean: number; ci95: [number, number] } };
type Data = {
  risk: { calibration: Point[]; metrics: Metrics; thresholds: Record<string, Stage> };
  outcome: { calibration: Point[]; metrics: Metrics };
  benchmark: { n: number; caveat: string; runs: Record<string, Run> };
  decision: { value: number; fee: number; offer_share_pct: number; curve: { p: number; fight: number; offer: number; accept: number }[] };
};

const diagonal = { type: "line" as const, data: [{ x: 0, y: 0 }, { x: 1, y: 1 }], xKey: "x", yKey: "y", yName: "Perfect", marker: { enabled: false }, stroke: "#888", lineDash: [4, 4] };
const pct = { min: 0, max: 1, label: { formatter: ({ value }: { value: number }) => `${Math.round(value * 100)}%` } };

function calibration(title: string, lines: { name: string; pts: Point[] }[]): AgCartesianChartOptions {
  return {
    title: { text: title },
    series: [diagonal, ...lines.map((l) => ({ type: "line" as const, data: l.pts, xKey: "mean_pred", yKey: "obs_rate", yName: l.name }))],
    axes: { x: { type: "number", position: "bottom", title: { text: "Predicted" }, ...pct }, y: { type: "number", position: "left", title: { text: "Observed" }, ...pct } },
  };
}

function Card({ options, note }: { options: AgCartesianChartOptions; note?: string }) {
  return (
    <section className="rounded border p-3">
      <div style={{ height: 320 }}><AgCharts options={options} /></div>
      {note && <p className="mt-2 text-sm text-neutral-600">{note}</p>}
    </section>
  );
}

export default function Evaluation() {
  const [d, setD] = useState<Data | null>(null);
  const [err, setErr] = useState("");
  const [stage, setStage] = useState("");

  useEffect(() => {
    fetch("/api/evaluation").then((r) => (r.ok ? r.json() : Promise.reject(new Error(`request failed (${r.status})`))))
      .then((x: Data) => { setD(x); setStage(Object.keys(x.risk.thresholds)[0]); }).catch((e: Error) => setErr(e.message));
  }, []);

  if (err) return <main className="min-h-screen bg-white p-6 text-red-700" role="alert">{err}</main>;
  if (!d) return <main className="min-h-screen bg-white p-6 text-neutral-900">Loading…</main>;

  const run = Object.values(d.benchmark.runs)[0];
  const stageRows = d.risk.thresholds[stage]?.by_threshold ?? [];
  const bench = Object.entries(run.metrics).map(([name, m]) => ({ name, brier: m.brier }));
  const diff = run.brier_diff_llm_minus_model;

  return (
    <main className="min-h-screen bg-white p-6 text-neutral-900">
      <header className="mb-6 flex items-center gap-4">
        <h1 className="text-2xl font-bold">Evaluation</h1>
        <a className="text-blue-700 underline" href="/dashboard">Dashboard</a>
      </header>
      <p className="mb-6 max-w-3xl text-sm text-neutral-700">
        Every chart is drawn from the saved model cards. Dispute labels come from our own generator rules, so these
        numbers show the models recover known structure, not that they predict PayPal&apos;s decisions.
      </p>
      <div className="grid gap-6 lg:grid-cols-2">
        <Card options={calibration("Dispute risk: calibration (test)", [{ name: "Risk model", pts: d.risk.calibration }])}
          note={`PR-AUC ${d.risk.metrics.pr_auc.toFixed(2)} against a base rate of ${(d.risk.metrics.base_rate * 100).toFixed(1)}%. Brier ${d.risk.metrics.brier.toFixed(3)}.`} />
        <Card options={calibration("Win chance: model vs an LLM's guess", [
          { name: "Trained model", pts: run.calibration.outcome_model }, { name: `LLM (${run.model})`, pts: run.calibration.llm }])}
          note="Below the dashed line the predictor claims more than it delivers. The LLM says about 8% where sellers win about 36%." />
        <section className="rounded border p-3">
          <label className="mb-2 block text-sm">Risk stage{" "}
            <select value={stage} onChange={(e) => setStage(e.target.value)} className="rounded border px-1">
              {Object.keys(d.risk.thresholds).map((s) => <option key={s}>{s}</option>)}
            </select>
          </label>
          <div style={{ height: 320 }}>
            <AgCharts options={{
              title: { text: "Precision and recall by threshold" }, data: stageRows,
              series: [{ type: "line", xKey: "threshold", yKey: "precision", yName: "Precision" }, { type: "line", xKey: "threshold", yKey: "recall", yName: "Recall" }],
              axes: { x: { type: "number", position: "bottom", title: { text: "Risk threshold" } }, y: { type: "number", position: "left", ...pct } },
            }} />
          </div>
        </section>
        <Card options={{
          title: { text: `LLM benchmark: Brier score, lower is better (${d.benchmark.n} disputes)` }, data: bench,
          series: [{ type: "bar", xKey: "name", yKey: "brier", yName: "Brier" }],
        }}
          note={`Model beats the LLM by ${diff.mean.toFixed(3)} Brier (95% interval ${diff.ci95[0].toFixed(3)} to ${diff.ci95[1].toFixed(3)}). One provider, one prompt, one run. ${d.benchmark.caveat}`} />
        <Card options={{
          title: { text: `Fight vs offer: value kept on a ${d.decision.value.toFixed(0)} dispute` }, data: d.decision.curve,
          series: ["fight", "offer", "accept"].map((k) => ({ type: "line" as const, xKey: "p", yKey: k, yName: k, marker: { enabled: false } })),
          axes: { x: { type: "number", position: "bottom", title: { text: "Chance of winning" }, ...pct }, y: { type: "number", position: "left", title: { text: "Expected value" } } },
        }}
          note={`Fee ${d.decision.fee.toFixed(0)} if a chargeback is lost, offer gives ${d.decision.offer_share_pct}% back. Fighting wins once the chance is high enough.`} />
      </div>
    </main>
  );
}
