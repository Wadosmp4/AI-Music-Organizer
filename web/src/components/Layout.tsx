import { useEffect, useState, type ReactNode } from "react";

import {
  fetchAuthStatus,
  fetchIngestionStatus,
  fetchOnboardingProposals,
  fetchReorganizeStatus,
  fetchReviewQueue,
  type AuthStatus,
  type IngestionStatus,
  type OnboardingProposalsStatus,
  type ReorganizeStatus,
  type StatusEntry,
} from "../api/client";
import { getStoredReorganizeSessionId } from "../reorganizeSession";
import { STATUS_DOT_CLASSES, STATUS_LABELS } from "../styles";

export type Page = "playlists" | "organize" | "settings";

const NAV_ITEMS: { page: Page; label: string }[] = [
  { page: "playlists", label: "Playlists" },
  { page: "organize", label: "Organize" },
  { page: "settings", label: "Settings" },
];

// U7 (R10): how often the nav-bar status indicator re-polls background
// classification/matching progress -- mirrors ReviewQueue's own
// MATCHING_POLL_INTERVAL_MS so live progress reads the same cadence
// regardless of which page (or none of ReviewQueue's own polling) is mounted.
const BACKGROUND_STATUS_POLL_INTERVAL_MS = 2000;

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

// Swallows a rejection (or, in tests, an unmocked call resolving to
// `undefined` rather than a Promise) into `null` -- one background source
// being unreachable must not stop the other three from rendering.
async function safeFetch<T>(fn: () => Promise<T>): Promise<T | null> {
  try {
    return (await fn()) ?? null;
  } catch {
    return null;
  }
}

// U7 (R10/KTD4): the indicator's states, most urgent first -- a genuine
// failure (the literal "failed" status the backend now writes instead of
// hanging/"done" on a bad run) always outranks a live in-progress read,
// which in turn outranks the passive "ready to review" signal, which
// outranks rendering nothing at all when everything is idle.
interface BackgroundIndicator {
  tone: "failed" | "progress" | "ready";
  text: string;
}

function computeBackgroundIndicator(args: {
  ingestionStatus: string;
  ingestionProcessedCount: number;
  ingestionTotalCount: number;
  proposalsStatus: string;
  proposalsProcessedCount: number;
  proposalsTotalCount: number;
  matchingStatus: string;
  matchedCount: number;
  matchingTotalCount: number;
  pendingPlaylistCount: number;
}): BackgroundIndicator | null {
  const {
    ingestionStatus,
    ingestionProcessedCount,
    ingestionTotalCount,
    proposalsStatus,
    proposalsProcessedCount,
    proposalsTotalCount,
    matchingStatus,
    matchedCount,
    matchingTotalCount,
    pendingPlaylistCount,
  } = args;

  if (ingestionStatus === "failed" || proposalsStatus === "failed" || matchingStatus === "failed") {
    return { tone: "failed", text: "Background classification failed" };
  }
  if (ingestionStatus === "in_progress") {
    return { tone: "progress", text: `Classifying… ${ingestionProcessedCount}/${ingestionTotalCount}` };
  }
  if (proposalsStatus === "in_progress") {
    return {
      tone: "progress",
      text: `Analyzing library… ${proposalsProcessedCount}/${proposalsTotalCount}`,
    };
  }
  if (matchingStatus === "in_progress") {
    return { tone: "progress", text: `Matching… ${matchedCount}/${matchingTotalCount}` };
  }
  if (pendingPlaylistCount > 0) {
    return {
      tone: "ready",
      text: `${pendingPlaylistCount} playlist${pendingPlaylistCount === 1 ? "" : "s"} ready to review`,
    };
  }
  return null;
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
    let cancelled = false;
    void fetchAuthStatus().then((status) => {
      if (!cancelled) setAuthStatus(status);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  // U7 (R10): a small always-visible indicator of background
  // classification/matching progress -- polled from the shell itself so it
  // stays live regardless of which page is mounted, independently of each
  // page's own polling of the same underlying statuses (mirrors the
  // authStatus effect above being independent of each page's own
  // fetchAuthStatus() call).
  const [ingestion, setIngestion] = useState<IngestionStatus | null>(null);
  const [proposals, setProposals] = useState<OnboardingProposalsStatus | null>(null);
  const [matching, setMatching] = useState<ReorganizeStatus | null>(null);
  // Count of playlists with at least one pending review-queue item --
  // the "ready to review" signal (mirrors ReviewQueue's own groupByPlaylist
  // over pending items).
  const [pendingPlaylistCount, setPendingPlaylistCount] = useState(0);

  useEffect(() => {
    let cancelled = false;

    async function poll() {
      // Matching is session-scoped (unlike ingestion/proposals, which are
      // one status per user) -- only worth polling if a reorganize session
      // was left running (mirrors ReviewQueue's own resume-on-mount check).
      const matchingSessionId = getStoredReorganizeSessionId();
      const [ingestionResult, proposalsResult, matchingResult, queue] = await Promise.all([
        safeFetch(fetchIngestionStatus),
        safeFetch(fetchOnboardingProposals),
        matchingSessionId === null
          ? Promise.resolve(null)
          : safeFetch(() => fetchReorganizeStatus(matchingSessionId)),
        safeFetch(fetchReviewQueue),
      ]);
      if (cancelled) return;
      if (ingestionResult) setIngestion(ingestionResult);
      if (proposalsResult) setProposals(proposalsResult);
      if (matchingResult) setMatching(matchingResult);
      if (queue) {
        const pendingPlaylistIds = new Set(
          queue
            .filter((item) => item.status === "pending" && item.playlist_id !== null)
            .map((item) => item.playlist_id),
        );
        setPendingPlaylistCount(pendingPlaylistIds.size);
      }
    }

    void poll();
    const interval = setInterval(() => void poll(), BACKGROUND_STATUS_POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  const backgroundIndicator = computeBackgroundIndicator({
    ingestionStatus: ingestion?.ingestion_status ?? "idle",
    ingestionProcessedCount: ingestion?.ingestion_processed_count ?? 0,
    ingestionTotalCount: ingestion?.ingestion_total_count ?? 0,
    proposalsStatus: proposals?.proposals_status ?? "idle",
    proposalsProcessedCount: proposals?.proposals_processed_count ?? 0,
    proposalsTotalCount: proposals?.proposals_total_count ?? 0,
    matchingStatus: matching?.matching_status ?? "idle",
    matchedCount: matching?.matched_count ?? 0,
    matchingTotalCount: matching?.total_count ?? 0,
    pendingPlaylistCount,
  });

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
            {backgroundIndicator && (
              <span
                data-testid="background-status-indicator"
                role="status"
                className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                  backgroundIndicator.tone === "failed"
                    ? "bg-rose-100 text-rose-700"
                    : backgroundIndicator.tone === "ready"
                      ? "bg-emerald-100 text-emerald-700"
                      : "bg-indigo-100 text-indigo-700"
                }`}
              >
                {backgroundIndicator.text}
              </span>
            )}
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
