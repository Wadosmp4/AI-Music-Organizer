import { useEffect, useState } from "react";

import {
  approveItem,
  fetchAuthStatus,
  fetchReviewQueue,
  moveItem,
  rejectItem,
  type AuthStatus,
  type ReviewQueueItem,
} from "../api/client";
import { BpmAttribution } from "../components/BpmAttribution";
import { ConnectionHealthBanners } from "../components/ConnectionHealthBanners";

function groupByPlaylist(items: ReviewQueueItem[]): Map<number | null, ReviewQueueItem[]> {
  const groups = new Map<number | null, ReviewQueueItem[]>();
  for (const item of items) {
    const key = item.playlist_id;
    const existing = groups.get(key) ?? [];
    existing.push(item);
    groups.set(key, existing);
  }
  return groups;
}

// Explicit thresholds, each branch a complete literal class string -- a
// Tailwind class built by interpolating a color name (e.g. `bg-${color}-100`)
// isn't detected by Tailwind's build-time class scanner and gets silently
// dropped from the production bundle.
function ConfidenceBadge({ confidence }: { confidence: number | null }) {
  if (confidence === null) {
    return (
      <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-500">
        —
      </span>
    );
  }
  const colorClasses =
    confidence >= 0.8
      ? "bg-emerald-100 text-emerald-700"
      : confidence >= 0.5
        ? "bg-amber-100 text-amber-700"
        : "bg-rose-100 text-rose-700";
  return (
    <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${colorClasses}`}>
      {confidence.toFixed(2)}
    </span>
  );
}

const PILL_BUTTON = "rounded-full px-3 py-1 text-sm font-medium transition-colors";

export function ReviewQueue() {
  const [items, setItems] = useState<ReviewQueueItem[]>([]);
  const [authStatus, setAuthStatus] = useState<AuthStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  async function load() {
    try {
      const [queue, status] = await Promise.all([fetchReviewQueue(), fetchAuthStatus()]);
      setItems(queue.filter((item) => item.status === "pending"));
      setAuthStatus(status);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  async function handleApprove(item: ReviewQueueItem) {
    await approveItem(item.id, item.version);
    setItems((prev) => prev.filter((i) => i.id !== item.id));
  }

  async function handleReject(item: ReviewQueueItem) {
    await rejectItem(item.id, item.version);
    setItems((prev) => prev.filter((i) => i.id !== item.id));
  }

  async function handleMove(item: ReviewQueueItem, newPlaylistId: number) {
    await moveItem(item.id, item.version, newPlaylistId);
    setItems((prev) => prev.filter((i) => i.id !== item.id));
  }

  const groups = groupByPlaylist(items);

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-2xl font-semibold text-slate-900">Review Queue</h1>
      <ConnectionHealthBanners authStatus={authStatus} />
      {error && (
        <p role="alert" className="rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
          {error}
        </p>
      )}
      {loading && <p className="py-8 text-center text-sm text-slate-500">Loading…</p>}
      {!loading && !error && items.length === 0 && (
        <p className="py-8 text-center text-sm text-slate-500">Nothing to review right now.</p>
      )}
      {[...groups.entries()].map(([playlistId, groupItems]) => (
        <section
          key={playlistId ?? "unassigned"}
          className="rounded-2xl border border-slate-200 bg-white shadow-sm"
        >
          <h2 className="border-b border-slate-100 px-5 py-3 text-sm font-semibold text-slate-700">
            Playlist #{playlistId ?? "unassigned"}
          </h2>
          <ul className="divide-y divide-slate-100">
            {groupItems.map((item) => (
              <li
                key={item.id}
                data-testid={`queue-item-${item.id}`}
                className="flex flex-col gap-2 px-5 py-4"
              >
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <strong className="text-slate-900">{item.title}</strong>{" "}
                    <span className="text-slate-500">— {item.artist}</span>
                  </div>
                  <ConfidenceBadge confidence={item.confidence} />
                </div>
                {item.explanation && (
                  <div className="text-sm text-slate-500">
                    <em className="not-italic font-medium text-slate-600">
                      {item.explanation.signal}
                    </em>
                    : {item.explanation.detail}
                  </div>
                )}
                {item.explanation?.bpm_source === "measured" && <BpmAttribution />}
                <div className="flex gap-2 pt-1">
                  <button
                    onClick={() => void handleApprove(item)}
                    className={`${PILL_BUTTON} bg-emerald-100 text-emerald-700 hover:bg-emerald-200`}
                  >
                    Approve
                  </button>
                  <button
                    onClick={() => void handleReject(item)}
                    className={`${PILL_BUTTON} bg-rose-100 text-rose-700 hover:bg-rose-200`}
                  >
                    Reject
                  </button>
                  <button
                    onClick={() => {
                      const target = window.prompt("Move to playlist id:");
                      if (target) void handleMove(item, Number(target));
                    }}
                    className={`${PILL_BUTTON} bg-slate-100 text-slate-700 hover:bg-slate-200`}
                  >
                    Move
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
