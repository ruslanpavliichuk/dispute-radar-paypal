"use client";

import { BryntumScheduler } from "@bryntum/scheduler-react";
import "@bryntum/scheduler/scheduler.css";
import "@bryntum/scheduler/svalbard-light.css";
import type { Ev, Resource } from "./model";

export default function SchedulerView({ resources, events, start, end, onPick }: { resources: Resource[]; events: Ev[]; start: Date; end: Date; onPick: (id: string) => void }) {
  return (
    <BryntumScheduler
      readOnly // approvals happen on the dashboard; the timeline only shows
      startDate={start}
      endDate={end}
      viewPreset="dayAndWeek"
      rowHeight={44}
      barMargin={8}
      eventStyle="filled"
      columns={[{ text: "Case", field: "name", width: 260 }]}
      resources={resources}
      events={events}
      timeRangesFeature={{ showCurrentTimeLine: true }}
      eventTooltipFeature={false} // the click panel below the chart replaces it
      eventRenderer={({ eventRecord }) => (eventRecord.isMilestone ? "" : eventRecord.name)} // points are dots only; their text lives in the click panel
      onEventClick={({ eventRecord }) => onPick(String(eventRecord.id))}
    />
  );
}
