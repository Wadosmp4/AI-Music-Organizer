import { useEffect, useState } from "react";

import { checkForNewSongs, createPlaylist, fetchAuthStatus, type AuthStatus } from "../api/client";
import { ConnectionHealthBanners } from "../components/ConnectionHealthBanners";

// Set by app/api/v1/auth_youtube.py's /callback redirect (?youtube_connect=...).
const YOUTUBE_CONNECT_MESSAGES: Record<string, string> = {
  success: "YouTube account connected.",
  denied: "YouTube connection cancelled — you can try again anytime.",
  state_mismatch: "YouTube connection failed a security check — please try again.",
  missing_code: "YouTube connection failed — please try again.",
  not_configured: "YouTube OAuth isn't configured (missing client id/secret in .env).",
  failed: "YouTube connection failed — please try again.",
};

export function Settings() {
  const [authStatus, setAuthStatus] = useState<AuthStatus | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    void fetchAuthStatus().then(setAuthStatus);

    const params = new URLSearchParams(window.location.search);
    const connectResult = params.get("youtube_connect");
    if (connectResult) {
      setMessage(YOUTUBE_CONNECT_MESSAGES[connectResult] ?? "YouTube connection attempt finished.");
      // Drop the query param so a page refresh doesn't re-show the message.
      window.history.replaceState(null, "", window.location.pathname);
      if (connectResult === "success") {
        void fetchAuthStatus().then(setAuthStatus);
      }
    }
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
            <li>LLM BPM estimate: {authStatus.llm_bpm_estimate.status}</li>
            <li>LLM description match: {authStatus.llm_description_match.status}</li>
            <li>LLM playlist clustering: {authStatus.llm_clustering.status}</li>
            <li>Last.fm genre lookup: {authStatus.lastfm.status}</li>
            <li>GetSongBPM tempo lookup: {authStatus.getsongbpm.status}</li>
          </ul>
        )}
        {/* Plain <a> (full-page navigation), not a fetch: Google's consent
            screen can only be reached by the browser actually navigating
            there, and the CSRF guard in app/main.py exempts GET anyway. */}
        <a href="/api/v1/auth/youtube/authorize">Connect Google account (new-likes detection)</a>
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
