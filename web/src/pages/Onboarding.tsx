import { useEffect, useState } from "react";

import {
  fetchOnboardingAnalysis,
  submitOnboardingSelection,
  type ExistingPlaylist,
  type PlaylistProposal,
} from "../api/client";

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

  useEffect(() => {
    void fetchOnboardingAnalysis().then((analysis) => {
      setProposals(analysis.proposals);
      setExistingPlaylists(analysis.existing_playlists);
    });
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
    onComplete?.();
  }

  if (done) {
    return <p role="status">Playlists created — the backlog will now be organized for review.</p>;
  }

  return (
    <div className="onboarding">
      <h1>Set up your playlists</h1>

      <section>
        <h2>Your existing playlists</h2>
        <ul>
          {existingPlaylists.map((playlist) => (
            <li key={playlist.id}>{playlist.name}</li>
          ))}
        </ul>
      </section>

      <section>
        <h2>Suggested new playlists</h2>
        {proposals.length === 0 && <p>No new-playlist suggestions found.</p>}
        <ul>
          {proposals.map((proposal) => (
            <li key={proposal.name}>
              <label>
                <input
                  type="checkbox"
                  checked={accepted.has(proposal.name)}
                  onChange={() => toggleAccepted(proposal.name)}
                />
                <strong>{proposal.name}</strong> — {proposal.theme} (~{proposal.song_count} songs)
              </label>
            </li>
          ))}
        </ul>
      </section>

      <section>
        <h2>Add your own playlist</h2>
        <form onSubmit={addCustomPlaylist}>
          <label>
            Name
            <input value={customName} onChange={(event) => setCustomName(event.target.value)} />
          </label>
          <label>
            Description
            <textarea
              value={customDescription}
              onChange={(event) => setCustomDescription(event.target.value)}
            />
          </label>
          <button type="submit">Add</button>
        </form>
        <ul>
          {customAdditions.map((addition) => (
            <li key={addition.name}>{addition.name}</li>
          ))}
        </ul>
      </section>

      <button onClick={() => void handleFinish()}>Finish setup</button>
    </div>
  );
}
