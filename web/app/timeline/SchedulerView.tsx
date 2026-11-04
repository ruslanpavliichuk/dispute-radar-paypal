"use client";

import { BryntumScheduler } from "@bryntum/scheduler-react";
import "@bryntum/scheduler/scheduler.css";
import "@bryntum/scheduler/svalbard-light.css";
import type { Ev, Resource } from "./model";

export default function SchedulerView({ resources, events, start, end }: { resources: Resource[]; events: Ev[]; start: Date; end: Date }) {
  return (
    <BryntumScheduler
      readOnly // approvals happen on the dashboard; the timeline only shows
      startDate={start}
      endDate={end}
      viewPreset="dayAndWeek"
      rowHeight={44}
      barMargin={8}
      columns={[{ text: "Case", field: "name", width: 260 }]}
      resources={resources}
      events={events}
      timeRangesFeature={{ showCurrentTimeLine: true }}
      eventTooltipFeature
    />
  );
}
