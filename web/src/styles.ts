import type { StatusEntry } from "./api/client";

// Shared Tailwind class-string tokens (KTD2's iOS-influenced visual language)
// so a future palette/shape tweak is a one-place edit, not a hunt across pages.
export const CARD = "rounded-2xl border border-slate-200 bg-white p-5 shadow-sm";

export const PRIMARY_BUTTON =
  "w-fit rounded-full bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700";

export const SECONDARY_BUTTON =
  "w-fit rounded-full bg-slate-100 px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-200";

export const INPUT =
  "rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-900 focus:border-accent focus:outline-none focus:ring-1 focus:ring-accent";

export const ALERT_BANNER = "rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800";

// KTD3: one status-color mapping app-wide -- Layout's navbar dots and
// Settings' connection-health rows both import from this neutral module
// (not from each other) so neither page-shell/page pair can drift apart.
export const STATUS_DOT_CLASSES: Record<StatusEntry["status"], string> = {
  ok: "bg-emerald-500",
  degraded: "bg-amber-500",
  needs_reconnect: "bg-rose-600",
};

export const STATUS_LABELS: Record<StatusEntry["status"], string> = {
  ok: "ok",
  degraded: "degraded",
  needs_reconnect: "needs reconnect",
};
