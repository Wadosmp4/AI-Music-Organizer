import { useEffect, useState, type ReactNode } from "react";

import { fetchAuthStatus, type AuthStatus, type StatusEntry } from "../api/client";

export type Page = "review" | "settings" | "onboarding";

const NAV_ITEMS: { page: Page; label: string }[] = [
  { page: "review", label: "Review Queue" },
  { page: "onboarding", label: "Onboarding" },
  { page: "settings", label: "Settings" },
];

// KTD3: one status-color mapping app-wide -- shared with Settings.tsx's
// connection-health rows so both surfaces can never drift apart.
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

function StatusDot({ label, status }: { label: string; status: StatusEntry["status"] }) {
  const text = `${label}: ${STATUS_LABELS[status]}`;
  return (
    <span
      className={`inline-block h-2.5 w-2.5 rounded-full ${STATUS_DOT_CLASSES[status]}`}
      role="img"
      aria-label={text}
      title={text}
    />
  );
}

export function Layout({
  page,
  onPageChange,
  children,
}: {
  page: Page;
  onPageChange: (page: Page) => void;
  children: ReactNode;
}) {
  // Independent from each page's own fetchAuthStatus() call (U2 Approach step
  // 3) -- ReviewQueue relies on its own call to populate its banners in
  // isolation in tests, and Settings has its own OAuth-redirect refetch.
  const [authStatus, setAuthStatus] = useState<AuthStatus | null>(null);

  useEffect(() => {
    void fetchAuthStatus().then(setAuthStatus);
  }, []);

  return (
    <div className="min-h-screen bg-slate-50">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-4xl items-center justify-between gap-4 px-6 py-3">
          <span className="text-lg font-semibold text-accent">Music Organizer</span>
          <nav className="flex gap-1 rounded-full bg-slate-100 p-1">
            {NAV_ITEMS.map((item) => (
              <button
                key={item.page}
                onClick={() => onPageChange(item.page)}
                className={`rounded-full px-4 py-1.5 text-sm font-medium transition-colors ${
                  page === item.page
                    ? "bg-accent text-white"
                    : "text-slate-600 hover:text-slate-900"
                }`}
              >
                {item.label}
              </button>
            ))}
          </nav>
          <div className="flex items-center gap-2">
            {authStatus && (
              <>
                <StatusDot label="Write path" status={authStatus.write_path.status} />
                <StatusDot label="Detection path" status={authStatus.detection_path.status} />
              </>
            )}
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-4xl px-6 py-8">{children}</main>
    </div>
  );
}
