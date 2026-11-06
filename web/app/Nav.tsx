"use client";

import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

const LINKS = [["/dashboard", "Dashboard"], ["/timeline", "Timeline"], ["/evaluation", "Evaluation"], ["/studio", "Studio"], ["/", "Shop"]];

/** Shared top bar for the seller pages. PayPal-inspired colours only: no PayPal logo or wordmark. */
export default function Nav({ title, children }: { title: string; children?: ReactNode }) {
  const path = usePathname();
  return (
    <header className="flex flex-wrap items-center gap-x-6 gap-y-2 bg-[#003087] px-5 py-3 text-white shadow">
      <div className="flex items-baseline gap-3">
        <span className="text-lg font-bold tracking-tight">Dispute Radar</span>
        <span className="text-sm text-white/70">{title}</span>
      </div>
      <nav className="flex gap-1 text-sm" aria-label="Pages">
        {LINKS.map(([href, label]) => (
          <a key={href} href={href} aria-current={path === href ? "page" : undefined}
            className={`rounded-md px-3 py-1.5 hover:bg-white/10 ${path === href ? "border-b-2 border-[#FFC439] font-semibold" : "text-white/85"}`}>
            {label}
          </a>
        ))}
      </nav>
      <div className="ml-auto flex items-center gap-2">{children}</div>
    </header>
  );
}
