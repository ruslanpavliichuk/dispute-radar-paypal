import { clientToolRunner, type AgAiEvent, type AgAiRunInput } from "ag-studio";

export type Row = {
  id: number; agent: string; subject: string; status: string; action: string; amount: number;
  score: number; why: string; created_day: string; draft_source: string;
};

const pct = (n: number) => `${Math.round(n * 100)}%`;
const money = (n: number) => `£${n.toFixed(2)}`;
const line = (r: Row) => `${r.subject} (${r.action}, ${pct(r.score)}${r.why ? `, ${r.why}` : ""})`;

/** Answers from the proposals table only: no LLM, no PayPal call, nothing it says changes data. */
export function answer(question: string, rows: Row[]): string {
  const q = question.toLowerCase();
  const waiting = rows.filter((r) => r.status === "PROPOSED");
  const orders = waiting.filter((r) => r.agent === "prevention").sort((a, b) => b.score - a.score);
  const disputes = waiting.filter((r) => r.agent === "response").sort((a, b) => b.score - a.score);
  if (!rows.length) return "There are no proposals yet. Simulate a shipping delay or open a sandbox dispute and I will have something to say.";
  if (/(loss|exposure|money|at stake|worth)/.test(q))
    return `Waiting for you: ${waiting.length} proposals. Orders at risk are worth ${money(orders.reduce((t, r) => t + r.amount, 0))} and the disputes ${money(disputes.reduce((t, r) => t + r.amount, 0))}.`;
  if (/(dispute|win|fight|offer|accept|claim)/.test(q))
    return disputes.length ? `Disputes waiting (${disputes.length}), best win chance first:\n${disputes.slice(0, 5).map((r) => `- ${line(r)}`).join("\n")}` : "No disputes are waiting for a decision.";
  if (/(risk|order|late|ship|track|refund)/.test(q))
    return orders.length ? `Orders waiting (${orders.length}), riskiest first:\n${orders.slice(0, 5).map((r) => `- ${line(r)}`).join("\n")}` : "No order is waiting for a decision.";
  if (/(wait|pending|approve|todo|first|next|what)/.test(q)) {
    const top = [...orders, ...disputes].slice(0, 3);
    return top.length ? `${waiting.length} proposals need you. Start with:\n${top.map((r) => `- ${line(r)}`).join("\n")}` : "Nothing is waiting for you.";
  }
  return "Ask me what is waiting, which orders are riskiest, how the disputes look, or how much money is at stake. I only read the proposals table, and approving happens on the dashboard.";
}

export function radarAgent(getRows: () => Row[]) {
  return clientToolRunner({
    id: "radar",
    name: "Radar",
    description: "Reads the seller's proposals and says what needs attention.",
    run: async function* (input: AgAiRunInput): AsyncGenerator<AgAiEvent> {
      const last = [...input.messages].reverse().find((m) => m.role === "user");
      const text = typeof last?.content === "string" ? last.content : "";
      yield { type: "RUN_STARTED", threadId: input.threadId, runId: input.runId };
      yield { type: "TEXT_MESSAGE_CHUNK", messageId: `radar-${input.runId}`, role: "assistant", delta: answer(text, getRows()) };
      yield { type: "RUN_FINISHED", threadId: input.threadId, runId: input.runId };
    },
  });
}
