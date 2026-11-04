export type Proposal = {
  id: number; agent: string; subject_id: string; status: string; action: string; amount_minor: number | null;
  currency: string | null; created_at: string; decided_at: string | null; sent_at: string | null;
  opened_at: string | null; due_at: string | null;
};

export type Resource = { id: string; name: string };
export type Ev = { id: string; resourceId: string; name: string; startDate: Date; endDate: Date; eventColor: string; iconCls?: string };

const DAY = 86_400_000;
const STATUS_COLOUR: Record<string, string> = { PROPOSED: "orange", SENDING: "blue", SENT: "green", FAILED: "red", REJECTED: "gray" };

/** One row per case. Disputes get a bar from opened to response-due; every agent step is a point on the row. */
export function toTimeline(proposals: Proposal[], now = new Date()): { resources: Resource[]; events: Ev[]; start: Date; end: Date } {
  const resources: Resource[] = [];
  const events: Ev[] = [];
  const seen = new Set<string>();
  for (const p of proposals) {
    const isDispute = p.agent === "response";
    const rid = isDispute ? p.subject_id : p.subject_id.split(":")[0];
    if (!seen.has(rid)) { seen.add(rid); resources.push({ id: rid, name: `${isDispute ? "Dispute" : "Order"} ${rid}` }); }
    const point = (suffix: string, name: string, at: string | null, colour: string) => {
      if (at) { const d = new Date(at); events.push({ id: `${p.id}-${suffix}`, resourceId: rid, name, startDate: d, endDate: d, eventColor: colour }); }
    };
    if (isDispute && p.opened_at && p.due_at) {
      const overdue = p.status === "PROPOSED" && new Date(p.due_at) < now;
      events.push({ id: `${p.id}-window`, resourceId: rid, name: `Respond by ${new Date(p.due_at).toLocaleDateString()}`,
        startDate: new Date(p.opened_at), endDate: new Date(p.due_at), eventColor: overdue ? "red" : "blue" });
    }
    point("proposed", `Proposed: ${p.action}`, p.created_at, "orange");
    point("decided", p.status === "REJECTED" ? "Rejected" : "Approved", p.decided_at, STATUS_COLOUR[p.status] ?? "gray");
    point("sent", p.status === "FAILED" ? "Send failed" : "Sent to PayPal", p.sent_at, STATUS_COLOUR[p.status] ?? "gray");
  }
  const times = events.flatMap((e) => [e.startDate.getTime(), e.endDate.getTime()]).concat(now.getTime());
  return { resources, events, start: new Date(Math.min(...times) - DAY), end: new Date(Math.max(...times) + 2 * DAY) };
}
