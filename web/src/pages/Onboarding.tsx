import { useEffect, useRef, useState } from "react";

import {
  asPlaylistRemovalConfirmation,
  cancelReorganize,
  fetchOnboardingPlaylists,
  fetchOnboardingProposals,
  fetchReorganizeStatus,
  submitOnboardingSelection,
  triggerOnboardingProposals,
  triggerReorganize,
  triggerReorganizeMatching,
  type AddedPlaylist,
  type PlaylistProposal,
  type YouTubePlaylist,
} from "../api/client";
import {
  clearStoredReorganizeSessionId,
  getStoredReorganizeSessionId,
  setStoredReorganizeSessionId,
} from "../reorganizeSession";
import { ALERT_BANNER, CARD, INPUT, PRIMARY_BUTTON, SECONDARY_BUTTON } from "../styles";

// U2: how often the reorganize screen polls for newly-clustered suggestions
// once a session is triggered.
const REORGANIZE_POLL_INTERVAL_MS = 2000;

// How often this page polls for progress on onboarding's own initial
// AI-suggested playlists (mirrors REORGANIZE_POLL_INTERVAL_MS) -- that
// generation now runs as a background task instead of one long request.
const PROPOSALS_POLL_INTERVAL_MS = 2000;

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
  // Split so the page can render playlists (a fast DB query + a plain
  // YouTube list) as soon as they arrive, instead of both being stuck
  // behind proposals (AI clustering, can take a long time for a big
  // library) on what used to be a single combined "Loading…" gate.
  const [playlistsLoading, setPlaylistsLoading] = useState(true);
  // Onboarding's own initial AI-suggested playlists now run as a
  // trigger+poll background task (mirrors Reorganize's clustering) instead
  // of one long request -- previously up to ~2 minutes of invisible work
  // behind a single "Loading…" spinner.
  const [proposalsStatus, setProposalsStatus] = useState<"idle" | "in_progress" | "done">("idle");
  const [proposalsProcessedCount, setProposalsProcessedCount] = useState(0);
  const [proposalsTotalCount, setProposalsTotalCount] = useState(0);
  const [error, setError] = useState<string | null>(null);

  // R1-R3: "Reorganize My Library" -- fetches the complete liked-songs
  // library and streams AI-clustered suggestions in as background batches
  // complete, replacing the static analysis-time proposal snapshot above
  // once triggered.
  const [reorganizeSessionId, setReorganizeSessionId] = useState<number | null>(null);
  const [clusteringStatus, setClusteringStatus] = useState<ClusteringStatus>("idle");
  const [reorganizeTriggering, setReorganizeTriggering] = useState(false);
  // Live enrichment progress (genre lookup per song) -- the slow phase
  // before any suggestion can appear, previously invisible to the user.
  const [enrichedCount, setEnrichedCount] = useState(0);
  const [totalCount, setTotalCount] = useState(0);
  const [cancelling, setCancelling] = useState(false);
  // Covers the *whole* handleFinish flow -- set synchronously on click,
  // before any await, so a double-click can't fire two overlapping calls
  // during the submitOnboardingSelection round-trip. That race used to let
  // two overlapping matching runs try to create the same LibraryItem
  // concurrently and crash with a UNIQUE constraint violation.
  const [finishing, setFinishing] = useState(false);

  const mountedRef = useRef(true);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const proposalsPollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  function stopPolling() {
    if (pollRef.current !== null) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }

  function stopProposalsPolling() {
    if (proposalsPollRef.current !== null) {
      clearInterval(proposalsPollRef.current);
      proposalsPollRef.current = null;
    }
  }

  useEffect(() => {
    mountedRef.current = true;
    // Read once, synchronously, before either async call below resolves --
    // deciding this after the fact (e.g. inside pollProposalsStatus's own
    // state update) would race against the session-restore poll below:
    // whichever resolved second would stomp on the other's proposals. A
    // restored session's own proposals are authoritative; onboarding's own
    // initial suggestions are only ever a starting point for when no
    // session is open.
    const storedSessionId = getStoredReorganizeSessionId();
    const hasStoredSession = storedSessionId !== null;

    void fetchOnboardingPlaylists()
      .then((playlists) => {
        if (!mountedRef.current) return;
        setExistingPlaylists(playlists.existing_playlists);
        setAddedPlaylists(playlists.added_playlists);
        setCheckedPlaylistKeys(
          new Set(playlists.added_playlists.map((p) => `added:${p.id}`)),
        );
      })
      .finally(() => {
        if (mountedRef.current) setPlaylistsLoading(false);
      });

    // A restored reorganize session's own proposals take over entirely (see
    // below) -- onboarding's own initial suggestions would just be ignored
    // in that case, so skip generating them at all rather than running a
    // real (LLM-cost) background job for nothing.
    if (!hasStoredSession) {
      // Check current status first rather than always triggering -- a fresh
      // trigger re-runs the whole clustering job from scratch, so a page
      // the user has already visited before (proposals_status "done", or
      // "in_progress" from an earlier visit still running) should just pick
      // up what's already there/in flight instead of redoing it. Only a
      // genuinely first-ever visit ("idle") kicks off a new run.
      void pollProposalsStatus().then((status) => {
        if (!mountedRef.current || status === undefined) return;
        if (status.proposals_status === "idle") {
          void triggerOnboardingProposals()
            .catch(() => {})
            .then(() => pollProposalsStatus())
            .then((polled) => {
              if (!mountedRef.current || polled === undefined) return;
              if (polled.proposals_status === "in_progress") {
                proposalsPollRef.current = setInterval(
                  () => void pollProposalsStatus(),
                  PROPOSALS_POLL_INTERVAL_MS,
                );
              }
            });
        } else if (status.proposals_status === "in_progress") {
          proposalsPollRef.current = setInterval(
            () => void pollProposalsStatus(),
            PROPOSALS_POLL_INTERVAL_MS,
          );
        }
      });
    } else {
      // Nothing to wait on in this branch -- treat it as immediately done
      // so the "no suggestions" empty state (gated on proposalsStatus ===
      // "done") isn't stuck waiting on a fetch that will never happen.
      setProposalsStatus("done");
    }

    // Resume a reorganize session left running when the user last navigated
    // away from this screen -- the server-side background job never
    // stopped, only this component's own state did (App.tsx unmounts this
    // whole page on tab switch).
    if (hasStoredSession) {
      const sessionId = storedSessionId;
      setReorganizeSessionId(sessionId);
      void pollReorganizeStatus(sessionId)
        .then((status) => {
          if (!mountedRef.current || status === undefined) return;
          if (status.clustering_status === "cancelled") {
            clearStoredReorganizeSessionId();
            setReorganizeSessionId(null);
            setClusteringStatus("idle");
            return;
          }
          if (status.clustering_status === "in_progress") {
            pollRef.current = setInterval(
              () => void pollReorganizeStatus(sessionId),
              REORGANIZE_POLL_INTERVAL_MS,
            );
          }
        })
        .catch(() => {
          // Session no longer resolvable (e.g. deleted server-side) --
          // stop treating it as open rather than polling a dead id forever.
          if (mountedRef.current) {
            clearStoredReorganizeSessionId();
            setReorganizeSessionId(null);
          }
        });
    }

    return () => {
      mountedRef.current = false;
      stopPolling();
      stopProposalsPolling();
    };
  }, []);

  async function pollReorganizeStatus(sessionId: number) {
    const status = await fetchReorganizeStatus(sessionId);
    if (!mountedRef.current) return status;
    setClusteringStatus(status.clustering_status as ClusteringStatus);
    setEnrichedCount(status.enriched_count);
    setTotalCount(status.total_count);
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
    return status;
  }

  async function pollProposalsStatus() {
    const status = await fetchOnboardingProposals();
    if (!mountedRef.current) return status;
    setProposalsStatus(status.proposals_status as "idle" | "in_progress" | "done");
    setProposalsProcessedCount(status.proposals_processed_count);
    setProposalsTotalCount(status.proposals_total_count);
    setProposals(status.proposals);
    if (status.proposals_status !== "in_progress") {
      stopProposalsPolling();
    }
    return status;
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
        setEnrichedCount(0);
        setTotalCount(0);
      }
      setReorganizeSessionId(result.session_id);
      setClusteringStatus(result.clustering_status as ClusteringStatus);
      setStoredReorganizeSessionId(result.session_id);
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
    // Custom additions share the same checked-by-default, index-keyed
    // convention as the existing/added playlist lists -- index is stable
    // here since entries are only ever appended, never removed/reordered.
    setCheckedPlaylistKeys((prev) => new Set(prev).add(`custom:${customAdditions.length}`));
    setCustomAdditions((prev) => [...prev, { name: customName, description: customDescription }]);
    setCustomName("");
    setCustomDescription("");
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
      clearStoredReorganizeSessionId();
      setReorganizeSessionId(null);
      setClusteringStatus("idle");
      setProposals([]);
      setEnrichedCount(0);
      setTotalCount(0);
    } catch (err) {
      if (mountedRef.current) setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (mountedRef.current) setCancelling(false);
    }
  }

  async function handleFinish() {
    if (finishing) return;
    setFinishing(true);
    try {
      await runFinish();
    } finally {
      if (mountedRef.current) setFinishing(false);
    }
  }

  async function runFinish() {
    const acceptedProposals = proposals
      .filter((p) => accepted.has(p.name))
      .map((p) => ({ name: p.name, theme: p.theme }));
    const adoptedPlaylists = existingPlaylists
      .filter((p) => checkedPlaylistKeys.has(`existing:${p.playlist_id}`))
      .map((p) => ({ playlist_id: p.playlist_id, name: p.title }));
    const removedPlaylistIds = addedPlaylists
      .filter((p) => !checkedPlaylistKeys.has(`added:${p.id}`))
      .map((p) => p.id);
    const checkedCustomAdditions = customAdditions.filter((_, index) =>
      checkedPlaylistKeys.has(`custom:${index}`),
    );

    setError(null);
    // KTD7: a removal with non-terminal review work still referencing it is
    // rejected (409) one playlist at a time -- confirm and resubmit until
    // either every removal is confirmed or the user declines one.
    let confirmedRemovedPlaylistIds: number[] = [];
    for (let attempt = 0; attempt <= removedPlaylistIds.length; attempt++) {
      try {
        await submitOnboardingSelection(
          acceptedProposals,
          checkedCustomAdditions,
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
      try {
        await triggerReorganizeMatching(reorganizeSessionId);
      } catch {
        // Best-effort: matching couldn't be triggered (e.g. already running
        // from an earlier attempt at this same session). Not fatal to
        // finishing setup -- the Review Queue page will still show whatever
        // matching progress already exists, and the user can retry there.
      }
    }

    if (!mountedRef.current) return;
    setDone(true);
    // Calling onComplete() synchronously here batches with setDone(true) into
    // one React update, so App.tsx switches pages before the "done" message
    // below ever paints -- give it a moment on screen first. Matching itself
    // now runs entirely server-side (U4 follow-up), so this no longer waits
    // on it -- the Review Queue page picks up its live progress instead.
    setTimeout(() => onComplete?.(), 1500);
  }

  // Every added playlist keeps the section it originated from (checked)
  // instead of collapsing into the generic list once it's a real playlist
  // -- otherwise accepting a proposal or adding a custom playlist during one
  // visit makes it look like it "moved" into Your YouTube Music playlists
  // (and unchecked, since a fresh AI re-cluster doesn't know it was already
  // accepted) the next time this page mounts.
  const addedProposalPlaylists = addedPlaylists.filter((p) => p.source === "proposal");
  const addedCustomPlaylists = addedPlaylists.filter((p) => p.source === "custom");
  const genericAddedPlaylists = addedPlaylists.filter(
    (p) => p.source !== "proposal" && p.source !== "custom",
  );
  // A freshly clustered suggestion can reproduce a name that's already an
  // accepted (now real) playlist -- don't show it a second time, unchecked,
  // next to its own already-added self above.
  const addedPlaylistNames = new Set(addedPlaylists.map((p) => p.name.toLowerCase()));
  const visibleProposals = proposals.filter((p) => !addedPlaylistNames.has(p.name.toLowerCase()));

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

      {playlistsLoading && <p className="py-8 text-center text-sm text-slate-500">Loading…</p>}

      {!playlistsLoading && (
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
            {clusteringStatus === "in_progress" && totalCount > 0 && (
              <div className="mt-3">
                <div className="flex items-center justify-between text-xs text-slate-500">
                  <span>
                    {enrichedCount < totalCount
                      ? "Processing your liked songs…"
                      : "Grouping songs into playlist suggestions…"}
                  </span>
                  <span>
                    {enrichedCount} / {totalCount} songs (
                    {Math.round((enrichedCount / totalCount) * 100)}%)
                  </span>
                </div>
                <progress
                  role="progressbar"
                  aria-label="Library processing progress"
                  className="mt-1 h-2 w-full accent-accent"
                  value={enrichedCount}
                  max={totalCount}
                />
              </div>
            )}
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
            {genericAddedPlaylists.length === 0 && existingPlaylists.length === 0 && (
              <p className="text-sm text-slate-500">
                No playlists found on your YouTube Music account.
              </p>
            )}
            <ul className="flex flex-col gap-2">
              {genericAddedPlaylists.map((playlist) => {
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
            {proposalsStatus === "in_progress" && proposalsTotalCount > 0 && (
              <div className="mb-3">
                <div className="flex items-center justify-between text-xs text-slate-500">
                  <span>Generating suggestions from your library…</span>
                  <span>
                    {proposalsProcessedCount} / {proposalsTotalCount} songs (
                    {Math.round((proposalsProcessedCount / proposalsTotalCount) * 100)}%)
                  </span>
                </div>
                <progress
                  role="progressbar"
                  aria-label="Suggested playlist generation progress"
                  className="mt-1 h-2 w-full accent-accent"
                  value={proposalsProcessedCount}
                  max={proposalsTotalCount}
                />
              </div>
            )}
            {proposalsStatus !== "done" && proposalsTotalCount === 0 && (
              <p className="text-sm text-slate-500">Loading suggestions…</p>
            )}
            {proposalsStatus === "done" &&
              visibleProposals.length === 0 &&
              addedProposalPlaylists.length === 0 &&
              clusteringStatus !== "in_progress" && (
                <p className="text-sm text-slate-500">
                  {reorganizeSessionId !== null
                    ? "No new playlist suggestions this run."
                    : "No new-playlist suggestions found."}
                </p>
              )}
            {clusteringStatus === "in_progress" && (
              <p className="text-sm text-slate-500">
                Clustering your library…
                {visibleProposals.length > 0 && ` (${visibleProposals.length} suggestion(s) found so far)`}
              </p>
            )}
            <ul className="flex flex-col gap-2">
              {addedProposalPlaylists.map((playlist) => {
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
                      <span>
                        <strong className="text-slate-900">{playlist.name}</strong>{" "}
                        {playlist.description && (
                          <span className="text-slate-500">— {playlist.description}</span>
                        )}
                      </span>
                    </label>
                  </li>
                );
              })}
              {[...visibleProposals]
                .sort((a, b) => b.song_count - a.song_count)
                .map((proposal) => {
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
            {(customAdditions.length > 0 || addedCustomPlaylists.length > 0) && (
              <ul className="mt-3 flex flex-col gap-2">
                {addedCustomPlaylists.map((playlist) => {
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
                        <span>
                          <strong className="text-slate-900">{playlist.name}</strong>{" "}
                          {playlist.description && (
                            <span className="text-slate-500">— {playlist.description}</span>
                          )}
                        </span>
                      </label>
                    </li>
                  );
                })}
                {customAdditions.map((addition, index) => {
                  const key = `custom:${index}`;
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
                        <span>
                          <strong className="text-slate-900">{addition.name}</strong>{" "}
                          <span className="text-slate-500">— {addition.description}</span>
                        </span>
                      </label>
                    </li>
                  );
                })}
              </ul>
            )}
          </section>

          {reorganizeSessionId !== null && (
            <p className="text-sm text-slate-500">
              A reorganize session is open — songs you approve on the Review Queue screen won't be
              written to YouTube until you click "Finish &amp; Apply" there.
            </p>
          )}
          <button
            onClick={() => void handleFinish()}
            disabled={finishing}
            className={PRIMARY_BUTTON}
          >
            {finishing ? "Finishing setup…" : "Finish setup"}
          </button>
        </>
      )}
    </div>
  );
}
