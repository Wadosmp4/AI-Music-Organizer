import { useEffect, useState } from "react";

import { checkForNewSongs, createPlaylist, fetchAuthStatus, type AuthStatus } from "../api/client";
import { ConnectionHealthBanners } from "../components/ConnectionHealthBanners";

export function Settings() {
  const [authStatus, setAuthStatus] = useState<AuthStatus | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    void fetchAuthStatus().then(setAuthStatus);
  }, []);

  async function handleCreatePlaylist(event: React.FormEvent) {
    event.preventDefault();
    const result = await createPlaylist(name, description);
    setMessage(
      `Created "${result.playlist.name}" — ${result.review_queue_items_created} songs proposed for review.`,
    );
    setName("");
    setDescription("");
  }

  async function handleCheckForNewSongs() {
    const result = await checkForNewSongs();
    setMessage(
      result.ran
        ? `Check complete (${result.mode}): ${result.new_songs_found} new song(s), ${result.queue_items_created} added to review queue.`
        : "Finish onboarding before checking for new songs.",
    );
  }

  return (
    <div className="settings">
      <h1>Settings</h1>
      <ConnectionHealthBanners authStatus={authStatus} />

      <section>
        <h2>Connection health</h2>
        {authStatus && (
          <ul>
            <li>Write path (playlist edits): {authStatus.write_path.status}</li>
            <li>Detection path (new likes): {authStatus.detection_path.status}</li>
            <li>Last ingestion check: {authStatus.youtube_detection.status}</li>
            <li>LLM classification: {authStatus.llm.status}</li>
            <li>Last.fm genre lookup: {authStatus.lastfm.status}</li>
            <li>GetSongBPM tempo lookup: {authStatus.getsongbpm.status}</li>
          </ul>
        )}
      </section>

      <section>
        <h2>Check for new songs</h2>
        <button onClick={() => void handleCheckForNewSongs()}>Check now</button>
      </section>

      <section>
        <h2>Create a playlist by description</h2>
        <form onSubmit={(event) => void handleCreatePlaylist(event)}>
          <label>
            Name
            <input value={name} onChange={(event) => setName(event.target.value)} required />
          </label>
          <label>
            Description
            <textarea
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              required
            />
          </label>
          <button type="submit">Create</button>
        </form>
      </section>

      {message && <p role="status">{message}</p>}
    </div>
  );
}
