"use client";

import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

const LINKS = [["/dashboard", "Dashboard"], ["/timeline", "Timeline"], ["/evaluation", "Evaluation"], ["/studio", "Studio"], ["/", "Shop"]];

/** Shared top bar for the seller pages: white bar, navy brand, blue active underline. PayPal-inspired colours only: no PayPal logo or wordmark. */
export default function Nav({ title, children }: { title: string; children?: ReactNode }) {
  const path = usePathname();
  return (
    <header className="flex flex-wrap items-center gap-x-8 gap-y-2 border-b border-[#E5E9F0] bg-white px-6 py-3">
      <a href="/dashboard" className="flex items-center gap-2" aria-label="Dispute Radar home">
        <span aria-hidden className="grid h-7 w-7 place-items-center rounded-full bg-[#003087] text-xs font-bold text-white">DR</span>
        <span className="text-lg font-bold tracking-tight text-[#003087]">Dispute Radar</span>
        <span className="hidden text-sm text-[#5C6B8A] sm:inline">/ {title}</span>
      </a>
      <nav className="flex gap-1 text-sm" aria-label="Pages">
        {LINKS.map(([href, label]) => (
          <a key={href} href={href} aria-current={path === href ? "page" : undefined}
            className={`relative px-3 py-2 font-medium transition-colors hover:text-[#0070E0] ${path === href ? "text-[#003087] after:absolute after:inset-x-3 after:-bottom-3 after:h-0.5 after:bg-[#0070E0]" : "text-[#5C6B8A]"}`}>
            {label}
          </a>
        ))}
      </nav>
      <div className="ml-auto flex items-center gap-2">{children}</div>
    </header>
  );
}
