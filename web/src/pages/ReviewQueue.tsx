import { useEffect, useMemo, useRef, useState } from "react";

import {
  approveItem,
  checkForNewSongs,
  fetchAuthStatus,
  fetchReviewQueue,
  moveItem,
  rejectItem,
  resetBacklog,
  type AuthStatus,
  type ReviewQueueItem,
} from "../api/client";
import { BpmAttribution } from "../components/BpmAttribution";
import { ConnectionHealthBanners } from "../components/ConnectionHealthBanners";
import { ALERT_BANNER } from "../styles";

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

// The drag payload's mime type -- a plain string key, not a real media type,
// but "text/plain" is the one type every browser reliably carries through a
// same-page HTML5 drag/drop without extra permissions.
const DRAG_MIME_TYPE = "text/plain";

interface DragPayload {
  id: number;
  version: number;
}

export function ReviewQueue() {
  const [items, setItems] = useState<ReviewQueueItem[]>([]);
  const [authStatus, setAuthStatus] = useState<AuthStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [checking, setChecking] = useState(false);
  const [checkMessage, setCheckMessage] = useState<string | null>(null);
  const [resetting, setResetting] = useState(false);
  const [dragOverPlaylistId, setDragOverPlaylistId] = useState<number | null>(null);
  const mountedRef = useRef(true);

  async function loadQueue() {
    try {
      const [queue, status] = await Promise.all([fetchReviewQueue(), fetchAuthStatus()]);
      if (!mountedRef.current) return;
      setItems(queue.filter((item) => item.status === "pending"));
      setAuthStatus(status);
    } catch (err) {
      if (!mountedRef.current) return;
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }

  useEffect(() => {
    mountedRef.current = true;
    void loadQueue();
    return () => {
      mountedRef.current = false;
    };
  }, []);

  async function handleCheckForNewSongs() {
    setChecking(true);
    try {
      const result = await checkForNewSongs();
      if (!mountedRef.current) return;
      setCheckMessage(
        result.ran
          ? `Loaded ${result.new_songs_found} new song(s), ${result.queue_items_created} added to review queue.`
          : "Finish onboarding before loading new songs.",
      );
      await loadQueue();
    } finally {
      if (mountedRef.current) setChecking(false);
    }
  }

  async function handleResetBacklog() {
    if (
      !window.confirm(
        "Restart batches from the beginning? Songs already approved or moved stay untouched, " +
          "but every other song in the review queue will be cleared and reclassified from scratch.",
      )
    ) {
      return;
    }
    setResetting(true);
    try {
      const result = await resetBacklog();
      if (!mountedRef.current) return;
      setCheckMessage(
        `Reset ${result.library_items_cleared} song(s) — click "Load next 50 songs" to start batching from the beginning again.`,
      );
      await loadQueue();
    } finally {
      if (mountedRef.current) setResetting(false);
    }
  }

  async function handleApproveAll(groupItems: ReviewQueueItem[]) {
    const results = await Promise.allSettled(
      groupItems.map((item) => approveItem(item.id, item.version)),
    );
    const succeededIds = new Set(
      groupItems.filter((_, i) => results[i].status === "fulfilled").map((item) => item.id),
    );
    setItems((prev) => prev.filter((i) => !succeededIds.has(i.id)));
    const failedCount = results.length - succeededIds.size;
    if (failedCount > 0) {
      setError(
        `${failedCount} of ${groupItems.length} song(s) failed to approve — the rest were approved; click Approve all again to retry the remainder.`,
      );
    }
  }

  async function handleReject(item: ReviewQueueItem) {
    await rejectItem(item.id, item.version);
    setItems((prev) => prev.filter((i) => i.id !== item.id));
  }

  async function handleMove(id: number, version: number, newPlaylistId: number) {
    await moveItem(id, version, newPlaylistId);
    setItems((prev) => prev.filter((i) => i.id !== id));
  }

  function handleDropOnPlaylist(event: React.DragEvent, playlistId: number) {
    event.preventDefault();
    setDragOverPlaylistId(null);
    const raw = event.dataTransfer.getData(DRAG_MIME_TYPE);
    if (!raw) return;
    const { id, version } = JSON.parse(raw) as DragPayload;
    const dragged = items.find((item) => item.id === id);
    if (!dragged || dragged.playlist_id === playlistId) return;
    void handleMove(id, version, playlistId);
  }

  const groups = useMemo(() => groupByPlaylist(items), [items]);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold text-slate-900">Review Queue</h1>
        <div className="flex gap-2">
          <button
            onClick={() => void handleResetBacklog()}
            disabled={resetting}
            className="rounded-full bg-slate-100 px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-200 disabled:opacity-50"
          >
            {resetting ? "Resetting…" : "Restart batches from the beginning"}
          </button>
          <button
            onClick={() => void handleCheckForNewSongs()}
            disabled={checking}
            className="rounded-full bg-slate-100 px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-200 disabled:opacity-50"
          >
            {checking ? "Loading…" : "Load next 50 songs"}
          </button>
        </div>
      </div>
      <ConnectionHealthBanners authStatus={authStatus} />
      {checkMessage && (
        <p
          role="status"
          className="rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm text-slate-700 shadow-sm"
        >
          {checkMessage}
        </p>
      )}
      {error && (
        <p role="alert" className={ALERT_BANNER}>
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
          onDragOver={(event) => {
            if (playlistId === null) return;
            event.preventDefault();
            event.dataTransfer.dropEffect = "move";
          }}
          onDragEnter={() => {
            if (playlistId !== null) setDragOverPlaylistId(playlistId);
          }}
          onDragLeave={() => {
            if (playlistId !== null) setDragOverPlaylistId((prev) => (prev === playlistId ? null : prev));
          }}
          onDrop={(event) => {
            if (playlistId !== null) handleDropOnPlaylist(event, playlistId);
          }}
          className={`rounded-2xl border bg-white shadow-sm transition-colors ${
            dragOverPlaylistId === playlistId && playlistId !== null
              ? "border-accent bg-indigo-50"
              : "border-slate-200"
          }`}
        >
          <div className="flex items-center justify-between border-b border-slate-100 px-5 py-3">
            <h2 className="text-sm font-semibold text-slate-700">
              {playlistId === null ? "Unassigned" : (groupItems[0]?.playlist_name ?? `Playlist #${playlistId}`)}
            </h2>
            {playlistId !== null && (
              <button
                onClick={() => void handleApproveAll(groupItems)}
                className={`${PILL_BUTTON} bg-emerald-100 text-emerald-700 hover:bg-emerald-200`}
              >
                Approve all ({groupItems.length})
              </button>
            )}
          </div>
          <ul className="divide-y divide-slate-100">
            {groupItems.map((item) => (
              <li
                key={item.id}
                data-testid={`queue-item-${item.id}`}
                draggable
                onDragStart={(event) => {
                  event.dataTransfer.effectAllowed = "move";
                  event.dataTransfer.setData(
                    DRAG_MIME_TYPE,
                    JSON.stringify({ id: item.id, version: item.version } satisfies DragPayload),
                  );
                }}
                className="flex cursor-grab flex-col gap-2 px-5 py-4 active:cursor-grabbing"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
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
                    onClick={() => void handleReject(item)}
                    className={`${PILL_BUTTON} bg-rose-100 text-rose-700 hover:bg-rose-200`}
                  >
                    Reject
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
