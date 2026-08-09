import { useEffect, useRef, useState } from "react";

import {
  asPlaylistRemovalConfirmation,
  cancelReorganize,
  fetchOnboardingAnalysis,
  fetchReorganizeStatus,
  runReorganizeMatchBatch,
  submitOnboardingSelection,
  triggerReorganize,
  type AddedPlaylist,
  type PlaylistProposal,
  type YouTubePlaylist,
} from "../api/client";
import { ALERT_BANNER, CARD, INPUT, PRIMARY_BUTTON, SECONDARY_BUTTON } from "../styles";

// U2: how often the reorganize screen polls for newly-clustered suggestions
// once a session is triggered.
const REORGANIZE_POLL_INTERVAL_MS = 2000;
// U4: matching runs as repeated batch calls (same shape as "Load next 50
// songs") until the session's snapshot is fully matched -- capped so a
// backend bug can't spin the browser tab forever.
const MAX_MATCHING_BATCHES = 500;

type ClusteringStatus = "idle" | "in_progress" | "done" | "stalled";

// F5: the user picks which playlists exist FIRST (AI-proposed candidates and/
// or their own custom description) — only after this selection completes
// does backlog classification (U4's backfill) run.
export function Onboarding({ onComplete }: { onComplete?: () => void }) {
  const [proposals, setProposals] = useState<PlaylistProposal[]>([]);
  const [existingPlaylists, setExistingPlaylists] = useState<YouTubePlaylist[]>([]);
  const [addedPlaylists, setAddedPlaylists] = useState<AddedPlaylist[]>([]);
  const [accepted, setAccepted] = useState<Set<string>>(new Set());
  // Every playlist this app knows about (already-added or still just sitting
  // on YouTube) gets one checkbox, keyed by `added:<id>` / `existing:<playlist_id>`
  // -- checked means "this app manages it." Already-added ones start checked
  // (unchecking removes them from local tracking); existing ones start
  // unchecked (checking adopts them). See handleFinish for how each key maps
  // back to an adopt/remove request.
  const [checkedPlaylistKeys, setCheckedPlaylistKeys] = useState<Set<string>>(new Set());
  const [customName, setCustomName] = useState("");
  const [customDescription, setCustomDescription] = useState("");
  const [customAdditions, setCustomAdditions] = useState<{ name: string; description: string }[]>(
    [],
  );
  const [done, setDone] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // R1-R3: "Reorganize My Library" -- fetches the complete liked-songs
  // library and streams AI-clustered suggestions in as background batches
  // complete, replacing the static analysis-time proposal snapshot above
  // once triggered.
  const [reorganizeSessionId, setReorganizeSessionId] = useState<number | null>(null);
  const [clusteringStatus, setClusteringStatus] = useState<ClusteringStatus>("idle");
  const [reorganizeTriggering, setReorganizeTriggering] = useState(false);
  const [matchingInProgress, setMatchingInProgress] = useState(false);
  // Batches already run this "Finish setup" -- surfaced as "songs classified
  // so far" progress instead of a static "Matching your library…" label.
  const [matchedSoFar, setMatchedSoFar] = useState(0);
  const [cancelling, setCancelling] = useState(false);

  const mountedRef = useRef(true);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  function stopPolling() {
    if (pollRef.current !== null) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }

  useEffect(() => {
    mountedRef.current = true;
    void fetchOnboardingAnalysis()
      .then((analysis) => {
        if (!mountedRef.current) return;
        setProposals(analysis.proposals);
        setExistingPlaylists(analysis.existing_playlists);
        setAddedPlaylists(analysis.added_playlists);
        setCheckedPlaylistKeys(
          new Set(analysis.added_playlists.map((p) => `added:${p.id}`)),
        );
      })
      .finally(() => {
        if (mountedRef.current) setLoading(false);
      });
    return () => {
      mountedRef.current = false;
      stopPolling();
    };
  }, []);

  async function pollReorganizeStatus(sessionId: number) {
    const status = await fetchReorganizeStatus(sessionId);
    if (!mountedRef.current) return;
    setClusteringStatus(status.clustering_status as ClusteringStatus);
    // Append-only: never re-order or replace proposals already shown from an
    // earlier poll, even as later batches complete.
    setProposals((prev) => {
      const known = new Set(prev.map((p) => p.name));
      const additions = status.proposals
        .filter((p) => !known.has(p.name))
        .map((p) => ({ ...p, confidence: 1 }));
      return additions.length > 0 ? [...prev, ...additions] : prev;
    });
    if (status.clustering_status === "done" || status.clustering_status === "stalled") {
      stopPolling();
    }
  }

  async function handleTriggerReorganize() {
    setReorganizeTriggering(true);
    setError(null);
    try {
      const result = await triggerReorganize();
      if (!mountedRef.current) return;
      // A retry that reuses the same (e.g. stalled) session appends to what
      // it already found rather than discarding it -- only a genuinely new
      // session supersedes the prior proposals (the static analysis-time
      // snapshot on first trigger, AE4, or a just-cancelled session's).
      if (result.session_id !== reorganizeSessionId) {
        setProposals([]);
      }
      setReorganizeSessionId(result.session_id);
      setClusteringStatus(result.clustering_status as ClusteringStatus);
      stopPolling();
      pollRef.current = setInterval(() => void pollReorganizeStatus(result.session_id), REORGANIZE_POLL_INTERVAL_MS);
    } catch (err) {
      if (mountedRef.current) setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (mountedRef.current) setReorganizeTriggering(false);
    }
  }

  function toggleAccepted(name: string) {
    setAccepted((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  }

  function togglePlaylistKey(key: string) {
    setCheckedPlaylistKeys((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function addCustomPlaylist(event: React.FormEvent) {
    event.preventDefault();
    if (!customName || !customDescription) return;
    setCustomAdditions((prev) => [...prev, { name: customName, description: customDescription }]);
    setCustomName("");
    setCustomDescription("");
  }

  // U4: runs session-scoped matching batches (same shape as "Load next 50
  // songs") until the whole snapshot has been matched, so the Review Queue
  // has something to show as soon as the reorganize screen hands off.
  async function runMatchingToCompletion(sessionId: number) {
    setMatchedSoFar(0);
    for (let i = 0; i < MAX_MATCHING_BATCHES; i++) {
      const result = await runReorganizeMatchBatch(sessionId);
      if (!mountedRef.current) return;
      setMatchedSoFar((prev) => prev + result.processed);
      if (result.matching_complete || !result.ran) return;
    }
  }

  // Lets the user abandon this session instead of being forced to finish
  // it -- every non-terminal item it already produced is discarded server-
  // side (never written to YouTube); the screen reverts to its
  // pre-reorganize state so a fresh trigger starts clean.
  async function handleCancelReorganize() {
    if (reorganizeSessionId === null) return;
    if (
      !window.confirm(
        "Cancel this reorganize session? Any suggestions or approvals from it will be discarded " +
          "-- nothing has been written to YouTube yet.",
      )
    ) {
      return;
    }
    setCancelling(true);
    setError(null);
    try {
      await cancelReorganize(reorganizeSessionId);
      if (!mountedRef.current) return;
      stopPolling();
      setReorganizeSessionId(null);
      setClusteringStatus("idle");
      setProposals([]);
    } catch (err) {
      if (mountedRef.current) setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (mountedRef.current) setCancelling(false);
    }
  }

  async function handleFinish() {
    const acceptedProposals = proposals
      .filter((p) => accepted.has(p.name))
      .map((p) => ({ name: p.name, theme: p.theme }));
    const adoptedPlaylists = existingPlaylists
      .filter((p) => checkedPlaylistKeys.has(`existing:${p.playlist_id}`))
      .map((p) => ({ playlist_id: p.playlist_id, name: p.title }));
    const removedPlaylistIds = addedPlaylists
      .filter((p) => !checkedPlaylistKeys.has(`added:${p.id}`))
      .map((p) => p.id);

    setError(null);
    // KTD7: a removal with non-terminal review work still referencing it is
    // rejected (409) one playlist at a time -- confirm and resubmit until
    // either every removal is confirmed or the user declines one.
    let confirmedRemovedPlaylistIds: number[] = [];
    for (let attempt = 0; attempt <= removedPlaylistIds.length; attempt++) {
      try {
        await submitOnboardingSelection(
          acceptedProposals,
          customAdditions,
          adoptedPlaylists,
          removedPlaylistIds,
          confirmedRemovedPlaylistIds,
        );
        break;
      } catch (err) {
        const confirmation = asPlaylistRemovalConfirmation(err);
        if (!confirmation) {
          setError(err instanceof Error ? err.message : String(err));
          return;
        }
        const proceed = window.confirm(
          `"${confirmation.playlist_name}" still has ${confirmation.pending_count} pending ` +
            "review item(s) referencing it. Remove it anyway? Its songs and the playlist itself " +
            "stay on YouTube untouched, but those pending items will lose their playlist.",
        );
        if (!proceed) return;
        confirmedRemovedPlaylistIds = [...confirmedRemovedPlaylistIds, confirmation.playlist_id];
      }
    }

    if (reorganizeSessionId !== null) {
      setMatchingInProgress(true);
      try {
        await runMatchingToCompletion(reorganizeSessionId);
      } finally {
        if (mountedRef.current) setMatchingInProgress(false);
      }
    }

    if (!mountedRef.current) return;
    setDone(true);
    // Calling onComplete() synchronously here batches with setDone(true) into
    // one React update, so App.tsx switches pages before the "done" message
    // below ever paints -- give it a moment on screen first.
    setTimeout(() => onComplete?.(), 1500);
  }

  if (done) {
    return (
      <p
        role="status"
        className="rounded-2xl border border-slate-200 bg-white px-5 py-8 text-center text-sm text-slate-700 shadow-sm"
      >
        Playlists created — the backlog will now be organized for review.
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-2xl font-semibold text-slate-900">Set up your playlists</h1>

      {error && (
        <p role="alert" className={ALERT_BANNER}>
          {error}
        </p>
      )}

      {loading && <p className="py-8 text-center text-sm text-slate-500">Loading…</p>}

      {!loading && (
        <>
          <section className={CARD}>
            <div className="flex items-center justify-between gap-3">
              <div>
                <h2 className="text-sm font-semibold text-slate-700">Reorganize your library</h2>
                <p className="mt-1 text-sm text-slate-500">
                  Re-fetches your complete liked-songs library and re-clusters new-playlist
                  suggestions from scratch — run it any time, even after a previous setup.
                </p>
              </div>
              <button
                onClick={() => void handleTriggerReorganize()}
                disabled={reorganizeTriggering || clusteringStatus === "in_progress"}
                className={SECONDARY_BUTTON}
              >
                {reorganizeTriggering || clusteringStatus === "in_progress"
                  ? "Fetching your library…"
                  : "Reorganize My Library"}
              </button>
            </div>
            {clusteringStatus === "stalled" && (
              <p className="mt-3 text-sm text-amber-700">
                No new suggestions have come in for a while — the background job may have
                stalled.{" "}
                <button
                  onClick={() => void handleTriggerReorganize()}
                  className="font-medium underline"
                >
                  Try again
                </button>
                .
              </p>
            )}
            {reorganizeSessionId !== null && (
              <p className="mt-3 text-sm text-slate-500">
                Session open.{" "}
                <button
                  onClick={() => void handleCancelReorganize()}
                  disabled={cancelling}
                  className="font-medium text-rose-600 underline disabled:opacity-50"
                >
                  {cancelling ? "Cancelling…" : "Cancel this session"}
                </button>
              </p>
            )}
          </section>

          <section className={CARD}>
            <h2 className="mb-3 text-sm font-semibold text-slate-700">
              Your YouTube Music playlists
            </h2>
            <p className="mb-3 text-sm text-slate-500">
              Checked playlists are managed by this app — new songs can be automatically
              matched into them, and you can approve or drag songs into them from the Review
              Queue. Uncheck one to stop managing it (its songs and the playlist itself stay
              on YouTube untouched); check one to start.
            </p>
            {addedPlaylists.length === 0 && existingPlaylists.length === 0 && (
              <p className="text-sm text-slate-500">
                No playlists found on your YouTube Music account.
              </p>
            )}
            <ul className="flex flex-col gap-2">
              {addedPlaylists.map((playlist) => {
                const key = `added:${playlist.id}`;
                const isChecked = checkedPlaylistKeys.has(key);
                return (
                  <li key={key}>
                    <label
                      className={`flex cursor-pointer items-start gap-3 rounded-xl border px-3 py-2 text-sm transition-colors ${
                        isChecked
                          ? "border-accent bg-indigo-50"
                          : "border-slate-200 hover:border-slate-300"
                      }`}
                    >
                      <input
                        type="checkbox"
                        checked={isChecked}
                        onChange={() => togglePlaylistKey(key)}
                        className="mt-1 accent-accent"
                      />
                      <strong className="text-slate-900">{playlist.name}</strong>
                    </label>
                  </li>
                );
              })}
              {existingPlaylists.map((playlist) => {
                const key = `existing:${playlist.playlist_id}`;
                const isChecked = checkedPlaylistKeys.has(key);
                return (
                  <li key={key}>
                    <label
                      className={`flex cursor-pointer items-start gap-3 rounded-xl border px-3 py-2 text-sm transition-colors ${
                        isChecked
                          ? "border-accent bg-indigo-50"
                          : "border-slate-200 hover:border-slate-300"
                      }`}
                    >
                      <input
                        type="checkbox"
                        checked={isChecked}
                        onChange={() => togglePlaylistKey(key)}
                        className="mt-1 accent-accent"
                      />
                      <strong className="text-slate-900">{playlist.title}</strong>
                    </label>
                  </li>
                );
              })}
            </ul>
          </section>

          <section className={CARD}>
            <h2 className="mb-3 text-sm font-semibold text-slate-700">Suggested new playlists</h2>
            {proposals.length === 0 && clusteringStatus !== "in_progress" && (
              <p className="text-sm text-slate-500">
                {reorganizeSessionId !== null
                  ? "No new playlist suggestions this run."
                  : "No new-playlist suggestions found."}
              </p>
            )}
            {clusteringStatus === "in_progress" && (
              <p className="text-sm text-slate-500">
                Clustering your library…
                {proposals.length > 0 && ` (${proposals.length} suggestion(s) found so far)`}
              </p>
            )}
            <ul className="flex flex-col gap-2">
              {proposals.map((proposal) => {
                const isAccepted = accepted.has(proposal.name);
                return (
                  <li key={proposal.name}>
                    <label
                      className={`flex cursor-pointer items-start gap-3 rounded-xl border px-3 py-2 text-sm transition-colors ${
                        isAccepted
                          ? "border-accent bg-indigo-50"
                          : "border-slate-200 hover:border-slate-300"
                      }`}
                    >
                      <input
                        type="checkbox"
                        checked={isAccepted}
                        onChange={() => toggleAccepted(proposal.name)}
                        className="mt-1 accent-accent"
                      />
                      <span>
                        <strong className="text-slate-900">{proposal.name}</strong>{" "}
                        <span className="text-slate-500">
                          — {proposal.theme} (~{proposal.song_count} songs)
                        </span>
                      </span>
                    </label>
                  </li>
                );
              })}
            </ul>
          </section>

          <section className={CARD}>
            <h2 className="mb-3 text-sm font-semibold text-slate-700">Add your own playlist</h2>
            <form onSubmit={addCustomPlaylist} className="flex flex-col gap-3">
              <label className="flex flex-col gap-1 text-sm text-slate-600">
                Name
                <input
                  value={customName}
                  onChange={(event) => setCustomName(event.target.value)}
                  className={INPUT}
                />
              </label>
              <label className="flex flex-col gap-1 text-sm text-slate-600">
                Description
                <textarea
                  value={customDescription}
                  onChange={(event) => setCustomDescription(event.target.value)}
                  className={INPUT}
                />
              </label>
              <button type="submit" className={SECONDARY_BUTTON}>
                Add
              </button>
            </form>
            {customAdditions.length > 0 && (
              <ul className="mt-3 flex flex-wrap gap-2">
                {customAdditions.map((addition) => (
                  <li
                    key={addition.name}
                    className="rounded-full bg-slate-100 px-3 py-1 text-xs font-medium text-slate-700"
                  >
                    {addition.name}
                  </li>
                ))}
              </ul>
            )}
          </section>

          {reorganizeSessionId !== null && !matchingInProgress && (
            <p className="text-sm text-slate-500">
              A reorganize session is open — songs you approve on the Review Queue screen won't be
              written to YouTube until you click "Finish &amp; Apply" there.
            </p>
          )}
          <button
            onClick={() => void handleFinish()}
            disabled={matchingInProgress}
            className={PRIMARY_BUTTON}
          >
            {matchingInProgress
              ? `Matching your library… (${matchedSoFar} song(s) classified)`
              : "Finish setup"}
          </button>
        </>
      )}
    </div>
  );
}
