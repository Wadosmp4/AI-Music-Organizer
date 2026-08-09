import { useEffect, useMemo, useRef, useState } from "react";

import {
  addToPlaylist,
  approveItem,
  checkForNewSongs,
  fetchApplyStatus,
  fetchAuthStatus,
  fetchPlaylists,
  fetchReviewQueue,
  rejectItem,
  resetBacklog,
  triggerFinishAndApply,
  type AddedPlaylist,
  type ApplyLastResult,
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

// KTD1/KTD7: a session-tagged item still counts as "open" work while it's
// pending review or decided-but-not-yet-written -- once it's approved,
// moved, rejected, or stale it's terminal and no longer keeps the session's
// banner (or Finish & Apply control) visible.
const NON_TERMINAL_SESSION_STATUSES = new Set(["pending", "approved_pending_apply"]);

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

const APPLY_POLL_INTERVAL_MS = 1500;

export function ReviewQueue() {
  // Unfiltered result of the last fetch -- `items` (below) is the
  // pending-only subset actually rendered for review; the full set is kept
  // around so an open reorganize session (U6/U7) can be detected even while
  // its approved_pending_apply items are hidden from the review list.
  const [allItems, setAllItems] = useState<ReviewQueueItem[]>([]);
  const [playlists, setPlaylists] = useState<AddedPlaylist[]>([]);
  const [authStatus, setAuthStatus] = useState<AuthStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [checking, setChecking] = useState(false);
  const [checkMessage, setCheckMessage] = useState<string | null>(null);
  const [resetting, setResetting] = useState(false);
  const [applying, setApplying] = useState(false);
  const [applyResult, setApplyResult] = useState<ApplyLastResult | null>(null);
  const mountedRef = useRef(true);

  const items = useMemo(() => allItems.filter((item) => item.status === "pending"), [allItems]);

  // The most recent still-open reorganize session, if any -- derived from
  // the full item set (not just the visible pending ones) so approve/apply
  // decisions already made this session still count toward "open".
  const openReorganizeSessionId = useMemo(() => {
    const openItem = allItems.find(
      (item) => item.reorganize_session_id !== null && NON_TERMINAL_SESSION_STATUSES.has(item.status),
    );
    return openItem?.reorganize_session_id ?? null;
  }, [allItems]);

  async function loadQueue() {
    try {
      const [queue, status, playlistList] = await Promise.all([
        fetchReviewQueue(),
        fetchAuthStatus(),
        fetchPlaylists(),
      ]);
      if (!mountedRef.current) return;
      setAllItems(queue);
      setAuthStatus(status);
      setPlaylists(playlistList);
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
    setAllItems((prev) => prev.filter((i) => !succeededIds.has(i.id)));
    const failedCount = results.length - succeededIds.size;
    if (failedCount > 0) {
      setError(
        `${failedCount} of ${groupItems.length} song(s) failed to approve — the rest were approved; click Approve all again to retry the remainder.`,
      );
    }
  }

  async function handleReject(item: ReviewQueueItem) {
    await rejectItem(item.id, item.version);
    setAllItems((prev) => prev.filter((i) => i.id !== item.id));
  }

  // Never an eager write -- the song is only ever queued as a pending
  // candidate in the target playlist's group, awaiting that group's own
  // Approve-all (mirrors ReviewQueueService.add_to_playlist). An unassigned
  // item's own row is reassigned in place (same id back); an already-placed
  // item gets an independent second pending row (a different id), which is
  // added to the list alongside the untouched original.
  async function handleAddToPlaylist(item: ReviewQueueItem, targetPlaylistId: number, targetName: string) {
    const result = await addToPlaylist(item.id, item.version, targetPlaylistId);
    if (!mountedRef.current) return;
    if (result.id === item.id) {
      setAllItems((prev) => prev.map((i) => (i.id === item.id ? result : i)));
    } else {
      setAllItems((prev) => [...prev, result]);
    }
    setCheckMessage(`Added "${item.title}" to ${targetName} — awaiting its own approval.`);
  }

  // U6: triggers Finish & Apply for the open session and polls until the
  // background run finishes, then reloads the queue so terminal items drop
  // off (or, on a partial failure, stay approved_pending_apply for retry).
  async function handleFinishAndApply() {
    if (openReorganizeSessionId === null) return;
    setApplying(true);
    setError(null);
    try {
      await triggerFinishAndApply(openReorganizeSessionId);
      for (;;) {
        const status = await fetchApplyStatus(openReorganizeSessionId);
        if (!mountedRef.current) return;
        if (status.apply_status !== "in_progress") {
          setApplyResult(status.apply_last_result);
          await loadQueue();
          return;
        }
        await sleep(APPLY_POLL_INTERVAL_MS);
      }
    } catch (err) {
      if (mountedRef.current) setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (mountedRef.current) setApplying(false);
    }
  }

  const groups = useMemo(() => groupByPlaylist(items), [items]);

  // A song already has a pending/approved candidate in these playlists (its
  // original suggestion plus any manual adds) -- offering them again in its
  // own "Add to playlist" dropdown would just create a same-song duplicate
  // the backend would silently collapse anyway (see add_to_playlist).
  const claimedPlaylistIdsByLibraryItem = useMemo(() => {
    const map = new Map<number, Set<number>>();
    for (const it of items) {
      if (it.playlist_id === null) continue;
      const claimed = map.get(it.library_item_id) ?? new Set<number>();
      claimed.add(it.playlist_id);
      map.set(it.library_item_id, claimed);
    }
    return map;
  }, [items]);

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
      {openReorganizeSessionId !== null && (
        <div
          data-testid="reorganize-session-banner"
          className="flex items-center justify-between gap-3 rounded-2xl border border-indigo-200 bg-indigo-50 px-4 py-3 text-sm text-indigo-900"
        >
          <span>
            A reorganize session is open — approvals here are saved locally until you apply them.
          </span>
          <button
            onClick={() => void handleFinishAndApply()}
            disabled={applying}
            className="w-fit rounded-full bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
          >
            {applying ? "Applying…" : "Finish & Apply"}
          </button>
        </div>
      )}
      {applyResult && (
        <div
          role="status"
          data-testid="apply-result"
          className="flex items-center justify-between gap-3 rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm text-slate-700 shadow-sm"
        >
          <span>
            Finish & Apply: {applyResult.succeeded} song(s) added
            {applyResult.failed > 0
              ? `, ${applyResult.failed} failed and will retry on the next apply.`
              : "."}
          </span>
          <button
            onClick={() => setApplyResult(null)}
            className="text-slate-500 hover:text-slate-700"
          >
            Dismiss
          </button>
        </div>
      )}
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
          className="rounded-2xl border border-slate-200 bg-white shadow-sm"
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
                className="flex flex-col gap-2 px-5 py-4"
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
                <div className="flex items-center gap-2 pt-1">
                  <button
                    onClick={() => void handleReject(item)}
                    className={`${PILL_BUTTON} bg-rose-100 text-rose-700 hover:bg-rose-200`}
                  >
                    Reject
                  </button>
                  <select
                    aria-label={`Add "${item.title}" to a playlist`}
                    value=""
                    onChange={(event) => {
                      const targetId = Number(event.target.value);
                      const target = playlists.find((p) => p.id === targetId);
                      if (target) void handleAddToPlaylist(item, target.id, target.name);
                    }}
                    className="rounded-full border border-slate-200 bg-white px-3 py-1 text-sm font-medium text-slate-700 hover:border-slate-300"
                  >
                    <option value="">+ Add to playlist</option>
                    {playlists
                      .filter((p) => !claimedPlaylistIdsByLibraryItem.get(item.library_item_id)?.has(p.id))
                      .map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.name}
                        </option>
                      ))}
                  </select>
                </div>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
