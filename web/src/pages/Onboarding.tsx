import { useEffect, useState } from "react";

import {
  fetchOnboardingAnalysis,
  submitOnboardingSelection,
  type ExistingPlaylist,
  type PlaylistProposal,
} from "../api/client";
import { CARD, INPUT, PRIMARY_BUTTON, SECONDARY_BUTTON } from "../styles";

// F5: the user picks which playlists exist FIRST (AI-proposed candidates and/
// or their own custom description) — only after this selection completes
// does backlog classification (U4's backfill) run.
export function Onboarding({ onComplete }: { onComplete?: () => void }) {
  const [proposals, setProposals] = useState<PlaylistProposal[]>([]);
  const [existingPlaylists, setExistingPlaylists] = useState<ExistingPlaylist[]>([]);
  const [accepted, setAccepted] = useState<Set<string>>(new Set());
  const [customName, setCustomName] = useState("");
  const [customDescription, setCustomDescription] = useState("");
  const [customAdditions, setCustomAdditions] = useState<{ name: string; description: string }[]>(
    [],
  );
  const [done, setDone] = useState(false);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    void fetchOnboardingAnalysis()
      .then((analysis) => {
        if (cancelled) return;
        setProposals(analysis.proposals);
        setExistingPlaylists(analysis.existing_playlists);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  function toggleAccepted(name: string) {
    setAccepted((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
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

  async function handleFinish() {
    const acceptedProposals = proposals
      .filter((p) => accepted.has(p.name))
      .map((p) => ({ name: p.name, theme: p.theme }));
    await submitOnboardingSelection(acceptedProposals, customAdditions);
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

      {loading && <p className="py-8 text-center text-sm text-slate-500">Loading…</p>}

      {!loading && (
        <>
          <section className={CARD}>
            <h2 className="mb-3 text-sm font-semibold text-slate-700">Your existing playlists</h2>
            <ul className="flex flex-col gap-1">
              {existingPlaylists.map((playlist) => (
                <li key={playlist.id} className="text-sm text-slate-700">
                  {playlist.name}
                </li>
              ))}
            </ul>
          </section>

          <section className={CARD}>
            <h2 className="mb-3 text-sm font-semibold text-slate-700">Suggested new playlists</h2>
            {proposals.length === 0 && (
              <p className="text-sm text-slate-500">No new-playlist suggestions found.</p>
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

          <button onClick={() => void handleFinish()} className={PRIMARY_BUTTON}>
            Finish setup
          </button>
        </>
      )}
    </div>
  );
}
