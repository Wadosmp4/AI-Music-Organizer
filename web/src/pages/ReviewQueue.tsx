import { useEffect, useMemo, useRef, useState } from "react";

import {
  addToPlaylist,
  approveItem,
  cancelReorganize,
  checkForNewSongs,
  fetchApplyStatus,
  fetchAuthStatus,
  fetchIngestionStatus,
  fetchPlaylists,
  fetchReorganizeStatus,
  fetchReviewQueue,
  rejectItem,
  resetBacklog,
  triggerFinishAndApply,
  type AddedPlaylist,
  type ApplyLastResult,
  type AuthStatus,
  type ReviewQueueItem,
} from "../api/client";
import { ConnectionHealthBanners } from "../components/ConnectionHealthBanners";
import {
  addSkippedPlaylistId,
  clearSkippedPlaylistIds,
  clearStoredCurrentPlaylistId,
  getSkippedPlaylistIds,
  getStoredCurrentPlaylistId,
  removeSkippedPlaylistId,
  setStoredCurrentPlaylistId,
} from "../organizeSkipState";
import { clearStoredReorganizeSessionId, getStoredReorganizeSessionId } from "../reorganizeSession";
import { ALERT_BANNER, CARD } from "../styles";

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

// KD2/KTD5: the guided sequence shows exactly one playlist's group at a
// time, in groupByPlaylist's existing (creation/selection) order -- no new
// sort. `pinnedPlaylistId` (set only by jumping to a playlist directly from
// the revisit list) overrides that order when it still resolves to a live
// group; otherwise the first entry not in the this-pass skip set wins. If
// every remaining group has been skipped, skipping is never allowed to hide
// all pending work forever -- fall back to the very first entry so the
// sequence just cycles back to it.
function pickCurrentEntry(
  entries: [number | null, ReviewQueueItem[]][],
  skippedPlaylistIds: Set<number>,
  pinnedPlaylistId: number | null,
): [number | null, ReviewQueueItem[]] | null {
  if (pinnedPlaylistId !== null) {
    const pinned = entries.find(([id]) => id === pinnedPlaylistId);
    if (pinned) return pinned;
  }
  const firstUnskipped = entries.find(([id]) => id === null || !skippedPlaylistIds.has(id));
  return firstUnskipped ?? entries[0] ?? null;
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

// Mirrors Onboarding's REORGANIZE_POLL_INTERVAL_MS -- how often this page
// polls for matching progress once a reorganize session was left running by
// Onboarding (U4 follow-up: matching now runs as its own background task, so
// this page can pick up its progress independently of whichever page
// triggered it).
const MATCHING_POLL_INTERVAL_MS = 2000;

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
  const [cancelling, setCancelling] = useState(false);
  const mountedRef = useRef(true);

  // Guided one-playlist-at-a-time Organize view (U5/KTD3): this-pass's
  // explicit skips, plus an optional pinned playlist id set only by jumping
  // to a playlist directly from the "still pending" list below. Both are
  // read from localStorage lazily on first render so a page switch (App.tsx
  // unmounts whichever page isn't active) doesn't reset guided-review
  // progress.
  const [skippedPlaylistIds, setSkippedPlaylistIds] = useState<Set<number>>(() =>
    getSkippedPlaylistIds(),
  );
  const [pinnedPlaylistId, setPinnedPlaylistId] = useState<number | null>(() =>
    getStoredCurrentPlaylistId(),
  );

  // Live matching progress (U4 follow-up): the session id comes from
  // localStorage rather than allItems/openReorganizeSessionId below, since
  // matching can be triggered on Onboarding and this page mounted before any
  // matched item exists yet to derive an "open session" from.
  const [matchingSessionId, setMatchingSessionId] = useState<number | null>(null);
  const [matchingStatus, setMatchingStatus] = useState("idle");
  const [matchedCount, setMatchedCount] = useState(0);
  const [matchingTotalCount, setMatchingTotalCount] = useState(0);
  const matchingPollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  function stopMatchingPolling() {
    if (matchingPollRef.current !== null) {
      clearInterval(matchingPollRef.current);
      matchingPollRef.current = null;
    }
  }

  async function pollMatchingStatus(sessionId: number) {
    const status = await fetchReorganizeStatus(sessionId);
    if (!mountedRef.current) return status;
    setMatchingStatus(status.matching_status);
    setMatchedCount(status.matched_count);
    setMatchingTotalCount(status.total_count);
    if (status.matching_status === "done") {
      stopMatchingPolling();
      // New items may have landed since the last full queue load.
      await loadQueue();
    }
    return status;
  }

  // Live ingestion-check progress: a background task now loops the "load
  // new songs" batch to completion server-side (mirrors matching's own
  // trigger+poll pattern) instead of the caller clicking "Load new songs"
  // repeatedly. Unlike matching, there's no session id to track -- it's a
  // single per-user status, so this page can always poll it on mount.
  const [ingestionStatus, setIngestionStatus] = useState("idle");
  const [ingestionProcessedCount, setIngestionProcessedCount] = useState(0);
  const [ingestionTotalCount, setIngestionTotalCount] = useState(0);
  const ingestionPollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  function stopIngestionPolling() {
    if (ingestionPollRef.current !== null) {
      clearInterval(ingestionPollRef.current);
      ingestionPollRef.current = null;
    }
  }

  async function pollIngestionStatus() {
    const status = await fetchIngestionStatus();
    if (!mountedRef.current) return status;
    setIngestionStatus(status.ingestion_status);
    setIngestionProcessedCount(status.ingestion_processed_count);
    setIngestionTotalCount(status.ingestion_total_count);
    if (status.ingestion_status === "done") {
      stopIngestionPolling();
      setCheckMessage(
        status.ingestion_total_count > 0
          ? `Loaded ${status.ingestion_total_count} new song(s) into the review queue.`
          : "No new songs found.",
      );
      await loadQueue();
    }
    return status;
  }

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

    // Resume tracking a matching run left in progress by Onboarding (or a
    // prior visit to this page) -- the background task itself never
    // stopped, only whichever page's state was watching it.
    const storedSessionId = getStoredReorganizeSessionId();
    if (storedSessionId !== null) {
      setMatchingSessionId(storedSessionId);
      void pollMatchingStatus(storedSessionId)
        .then((status) => {
          if (!mountedRef.current || status === undefined) return;
          if (status.matching_status === "in_progress") {
            matchingPollRef.current = setInterval(
              () => void pollMatchingStatus(storedSessionId),
              MATCHING_POLL_INTERVAL_MS,
            );
          }
        })
        .catch(() => {
          // Session no longer resolvable (e.g. deleted server-side) --
          // stop treating it as trackable rather than polling a dead id.
          if (mountedRef.current) setMatchingSessionId(null);
        });
    }

    // Resume tracking an ingestion-check run left in progress -- same
    // rationale, but there's no id to remember: it's always this user's one
    // status, so just check it on every mount.
    void pollIngestionStatus().then((status) => {
      if (!mountedRef.current || status === undefined) return;
      if (status.ingestion_status === "in_progress") {
        ingestionPollRef.current = setInterval(
          () => void pollIngestionStatus(),
          MATCHING_POLL_INTERVAL_MS,
        );
      }
    });

    return () => {
      mountedRef.current = false;
      stopMatchingPolling();
      stopIngestionPolling();
    };
  }, []);

  async function handleCheckForNewSongs() {
    setChecking(true);
    setError(null);
    try {
      const result = await checkForNewSongs();
      if (!mountedRef.current) return;
      if (!result.ran) {
        setCheckMessage("Finish onboarding before loading new songs.");
        return;
      }
      stopIngestionPolling();
      ingestionPollRef.current = setInterval(() => void pollIngestionStatus(), MATCHING_POLL_INTERVAL_MS);
      await pollIngestionStatus();
    } catch (err) {
      if (mountedRef.current) setError(err instanceof Error ? err.message : String(err));
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
        `Reset ${result.library_items_cleared} song(s) — click "Load new songs" to start batching from the beginning again.`,
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

  // Lets the user abandon this session instead of being forced to finish
  // it -- every non-terminal item it produced is discarded server-side
  // (never written to YouTube), so they simply drop off this list.
  async function handleCancelSession() {
    if (openReorganizeSessionId === null) return;
    if (
      !window.confirm(
        "Cancel this reorganize session? Its pending and not-yet-applied approvals will be " +
          "discarded -- nothing has been written to YouTube yet.",
      )
    ) {
      return;
    }
    setCancelling(true);
    setError(null);
    try {
      await cancelReorganize(openReorganizeSessionId);
      if (!mountedRef.current) return;
      // Cancelling only rejects this session's items server-side -- an
      // in-progress matching run keeps writing new ones in the background
      // (see run_reorganize_matching), so stop tracking/displaying its
      // progress here rather than showing a phantom banner for orphaned work.
      stopMatchingPolling();
      setMatchingSessionId(null);
      setMatchingStatus("idle");
      clearStoredReorganizeSessionId();
      await loadQueue();
    } catch (err) {
      if (mountedRef.current) setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (mountedRef.current) setCancelling(false);
    }
  }

  const groups = useMemo(() => groupByPlaylist(items), [items]);
  const groupEntries = useMemo(() => [...groups.entries()], [groups]);

  // R8: groupByPlaylist (above) already excludes any playlist with zero
  // pending items and recomputes live on every poll/reload, so a playlist
  // that just emptied out (or one that gained pending items again through
  // ongoing use) simply appears/disappears from `groupEntries` on its own --
  // no extra filtering needed here for the auto-skip/auto-recovery behavior.
  const currentEntry = useMemo(
    () => pickCurrentEntry(groupEntries, skippedPlaylistIds, pinnedPlaylistId),
    [groupEntries, skippedPlaylistIds, pinnedPlaylistId],
  );
  const otherEntries = useMemo(
    () => groupEntries.filter(([id]) => !currentEntry || id !== currentEntry[0]),
    [groupEntries, currentEntry],
  );

  // A pinned jump target that no longer resolves to a live group (its last
  // item was cleared while it was current, or some other tab/session
  // resolved it) shouldn't keep squatting in localStorage -- left alone, it
  // would wrongly resurrect itself as "current" if that same playlist later
  // gains new pending items through ongoing use, even though the user
  // already finished reviewing it.
  useEffect(() => {
    if (loading || pinnedPlaylistId === null) return;
    if (!groups.has(pinnedPlaylistId)) {
      clearStoredCurrentPlaylistId();
      setPinnedPlaylistId(null);
    }
  }, [loading, pinnedPlaylistId, groups]);

  // "This-pass" skip/pin state is scoped to a single sweep through the
  // queue -- once every playlist is resolved (nothing pending anywhere),
  // clear it so a future pass (e.g. after "Load new songs" finds more work)
  // starts from the top again instead of carrying over stale skips.
  useEffect(() => {
    if (loading || items.length > 0) return;
    if (skippedPlaylistIds.size === 0 && pinnedPlaylistId === null) return;
    clearSkippedPlaylistIds();
    clearStoredCurrentPlaylistId();
    setSkippedPlaylistIds(new Set());
    setPinnedPlaylistId(null);
  }, [loading, items.length, skippedPlaylistIds, pinnedPlaylistId]);

  // KD3/R7: defers the current playlist rather than forcing it clear first --
  // it stays in the queue (and reachable via the "still pending" list below),
  // just no longer the guided sequence's current stop until it's jumped to
  // directly or every other playlist runs out.
  function handleSkipCurrent() {
    if (!currentEntry) return;
    const [playlistId] = currentEntry;
    if (playlistId === null) return; // Unassigned has no id to record a skip against.
    addSkippedPlaylistId(playlistId);
    setSkippedPlaylistIds(getSkippedPlaylistIds());
    clearStoredCurrentPlaylistId();
    setPinnedPlaylistId(null);
  }

  // Lets a skipped (or not-yet-reached) playlist be visited directly from
  // the "still pending" list, overriding the normal creation-order sequence
  // until it resolves or is skipped again.
  function handleJumpToPlaylist(playlistId: number) {
    removeSkippedPlaylistId(playlistId);
    setSkippedPlaylistIds(getSkippedPlaylistIds());
    setStoredCurrentPlaylistId(playlistId);
    setPinnedPlaylistId(playlistId);
  }

  // Titles for the apply-result banner's failed songs (falls back to just
  // the count in the JSX below if none of them are still in allItems).
  const failedItemTitles = useMemo(() => {
    if (!applyResult || applyResult.failed_item_ids.length === 0) return [];
    const byId = new Map(allItems.map((item) => [item.id, item]));
    return applyResult.failed_item_ids
      .map((id) => byId.get(id)?.title)
      .filter((title): title is string => title !== undefined);
  }, [applyResult, allItems]);

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

  // R13: distinguishes "nothing pending, and the last load actually
  // finished" from the plain generic empty state below (e.g. before
  // onboarding is done, or mid-ingestion with no count in yet) -- this one
  // gets its own explicit "you're done" framing instead.
  const allCaughtUp = ingestionStatus === "done" && items.length === 0;

  // Guided view (KD2): render only the current entry's group, not every
  // group -- destructured once here rather than inline in the JSX below.
  const [currentPlaylistId, currentGroupItems] = currentEntry ?? [null, []];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold text-slate-900">Organize</h1>
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
            disabled={checking || ingestionStatus === "in_progress"}
            className="rounded-full bg-slate-100 px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-200 disabled:opacity-50"
          >
            {checking || ingestionStatus === "in_progress" ? "Loading…" : "Load new songs"}
          </button>
        </div>
      </div>
      <ConnectionHealthBanners authStatus={authStatus} />
      {ingestionStatus === "in_progress" && ingestionTotalCount > 0 && (
        <div
          data-testid="ingestion-progress"
          className="rounded-2xl border border-indigo-200 bg-indigo-50 px-4 py-3 text-sm text-indigo-900"
        >
          <div className="flex items-center justify-between text-xs text-indigo-700">
            <span>Loading new songs…</span>
            <span>
              {ingestionProcessedCount} / {ingestionTotalCount} songs (
              {Math.round((ingestionProcessedCount / ingestionTotalCount) * 100)}%)
            </span>
          </div>
          <progress
            role="progressbar"
            aria-label="New song loading progress"
            className="mt-1 h-2 w-full accent-accent"
            value={ingestionProcessedCount}
            max={ingestionTotalCount}
          />
        </div>
      )}
      {/* R14: the backend now reports "failed" (rather than hanging or
          silently reporting "done") for a genuinely failed run -- a healthy
          fetch that simply finds nothing new still ends "done", so this only
          fires when the run's own YouTube fetches actually broke. */}
      {ingestionStatus === "failed" && (
        <div
          data-testid="ingestion-failed"
          className="rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800"
        >
          <p>Loading new songs failed -- your YouTube connection may need attention.</p>
          <button
            onClick={() => void handleCheckForNewSongs()}
            disabled={checking}
            className="mt-1 font-medium underline disabled:opacity-50"
          >
            {checking ? "Loading…" : "Try again"}
          </button>
        </div>
      )}
      {matchingSessionId !== null && matchingStatus === "in_progress" && matchingTotalCount > 0 && (
        <div
          data-testid="matching-progress"
          className="rounded-2xl border border-indigo-200 bg-indigo-50 px-4 py-3 text-sm text-indigo-900"
        >
          <div className="flex items-center justify-between text-xs text-indigo-700">
            <span>Matching your library into playlists…</span>
            <span>
              {matchedCount} / {matchingTotalCount} songs (
              {Math.round((matchedCount / matchingTotalCount) * 100)}%)
            </span>
          </div>
          <progress
            role="progressbar"
            aria-label="Library matching progress"
            className="mt-1 h-2 w-full accent-accent"
            value={matchedCount}
            max={matchingTotalCount}
          />
        </div>
      )}
      {openReorganizeSessionId !== null && (
        <div
          data-testid="reorganize-session-banner"
          className="flex items-center justify-between gap-3 rounded-2xl border border-indigo-200 bg-indigo-50 px-4 py-3 text-sm text-indigo-900"
        >
          <span>
            A reorganize session is open — approvals here are saved locally until you apply them.
          </span>
          <div className="flex gap-2">
            <button
              onClick={() => void handleCancelSession()}
              disabled={applying || cancelling}
              className="w-fit rounded-full px-4 py-2 text-sm font-medium text-rose-700 hover:bg-rose-100 disabled:opacity-50"
            >
              {cancelling ? "Cancelling…" : "Cancel session"}
            </button>
            <button
              onClick={() => void handleFinishAndApply()}
              disabled={applying || cancelling}
              className="w-fit rounded-full bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
            >
              {applying ? "Applying…" : "Finish & Apply"}
            </button>
          </div>
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
              ? `, ${applyResult.failed} failed and will retry on the next apply` +
                (failedItemTitles.length > 0 ? ` (${failedItemTitles.join(", ")}).` : ".")
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
      {!loading && !error && allCaughtUp && (
        <p
          role="status"
          data-testid="all-caught-up"
          className="py-8 text-center text-sm text-slate-500"
        >
          You're all caught up — nothing pending review right now.
        </p>
      )}
      {!loading && !error && !allCaughtUp && items.length === 0 && (
        <p className="py-8 text-center text-sm text-slate-500">Nothing to review right now.</p>
      )}
      {!loading && currentEntry && (
        <section
          key={currentPlaylistId ?? "unassigned"}
          className="rounded-2xl border border-slate-200 bg-white shadow-sm"
        >
          <div className="flex items-center justify-between border-b border-slate-100 px-5 py-3">
            <h2 className="text-sm font-semibold text-slate-700">
              {currentPlaylistId === null
                ? "Unassigned"
                : (currentGroupItems[0]?.playlist_name ?? `Playlist #${currentPlaylistId}`)}
            </h2>
            <div className="flex gap-2">
              {currentPlaylistId !== null && (
                <button
                  onClick={() => void handleApproveAll(currentGroupItems)}
                  className={`${PILL_BUTTON} bg-emerald-100 text-emerald-700 hover:bg-emerald-200`}
                >
                  Approve all ({currentGroupItems.length})
                </button>
              )}
              {/* KD3/R7: explicit, per-playlist -- a playlist with many
                  pending items shouldn't block progress through the rest
                  of the guided sequence. */}
              {currentPlaylistId !== null && (
                <button
                  onClick={handleSkipCurrent}
                  className={`${PILL_BUTTON} bg-slate-100 text-slate-600 hover:bg-slate-200`}
                >
                  Skip for now
                </button>
              )}
            </div>
          </div>
          <ul className="divide-y divide-slate-100">
            {currentGroupItems.map((item) => (
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
      )}
      {!loading && otherEntries.length > 0 && (
        <section data-testid="organize-revisit-list" className={CARD}>
          <h2 className="mb-3 text-sm font-semibold text-slate-700">Still pending review</h2>
          <ul className="flex flex-col gap-2">
            {otherEntries.map(([playlistId, groupItems]) => (
              <li key={playlistId ?? "unassigned"}>
                {playlistId === null ? (
                  <div className="flex items-center justify-between gap-3 rounded-xl border border-slate-200 px-3 py-2 text-sm">
                    <strong className="text-slate-900">Unassigned</strong>
                    <span className="text-slate-500">{groupItems.length} pending</span>
                  </div>
                ) : (
                  <button
                    data-testid={`revisit-${playlistId}`}
                    onClick={() => handleJumpToPlaylist(playlistId)}
                    className="flex w-full items-center justify-between gap-3 rounded-xl border border-slate-200 px-3 py-2 text-left text-sm transition-colors hover:border-slate-300"
                  >
                    <strong className="text-slate-900">
                      {groupItems[0]?.playlist_name ?? `Playlist #${playlistId}`}
                    </strong>
                    <span className="text-slate-500">{groupItems.length} pending</span>
                  </button>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
