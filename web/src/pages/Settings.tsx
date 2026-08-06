import { useEffect, useState } from "react";

import {
  createPlaylist,
  fetchAuthStatus,
  type AuthStatus,
  type StatusEntry,
} from "../api/client";
import { ConnectionHealthBanners } from "../components/ConnectionHealthBanners";
import { CARD, INPUT, PRIMARY_BUTTON, STATUS_DOT_CLASSES, STATUS_LABELS } from "../styles";

// Set by app/api/v1/auth_youtube.py's /callback redirect (?youtube_connect=...).
const YOUTUBE_CONNECT_MESSAGES: Record<string, string> = {
  success: "YouTube account connected.",
  denied: "YouTube connection cancelled — you can try again anytime.",
  state_mismatch: "YouTube connection failed a security check — please try again.",
  missing_code: "YouTube connection failed — please try again.",
  not_configured: "YouTube OAuth isn't configured (missing client id/secret in .env).",
  failed: "YouTube connection failed — please try again.",
};

function StatusRow({ label, status }: { label: string; status: StatusEntry["status"] }) {
  return (
    <li className="flex items-center gap-2 py-1.5 text-sm">
      <span
        className={`inline-block h-2.5 w-2.5 shrink-0 rounded-full ${STATUS_DOT_CLASSES[status]}`}
        role="img"
        aria-label={STATUS_LABELS[status]}
      />
      <span className="text-slate-700">{label}</span>
      <span className="text-slate-400">— {STATUS_LABELS[status]}</span>
    </li>
  );
}

export function Settings() {
  const [authStatus, setAuthStatus] = useState<AuthStatus | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    // Single fetch, not one-per-branch: the backend's /callback route fully
    // updates auth status before it redirects here, so this mount-time fetch
    // already reflects a just-completed connect -- a second post-redirect
    // fetch was redundant and could race this one, letting whichever
    // response arrived first win over the freshest data.
    void fetchAuthStatus().then((status) => {
      if (!cancelled) setAuthStatus(status);
    });

    const params = new URLSearchParams(window.location.search);
    const connectResult = params.get("youtube_connect");
    if (connectResult) {
      setMessage(YOUTUBE_CONNECT_MESSAGES[connectResult] ?? "YouTube connection attempt finished.");
      // Drop the query param so a page refresh doesn't re-show the message.
      window.history.replaceState(null, "", window.location.pathname);
    }

    return () => {
      cancelled = true;
    };
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

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-2xl font-semibold text-slate-900">Settings</h1>
      <ConnectionHealthBanners authStatus={authStatus} />

      <section className={CARD}>
        <h2 className="mb-3 text-sm font-semibold text-slate-700">Connection health</h2>
        {authStatus && (
          <ul className="divide-y divide-slate-100">
            <StatusRow label="Write path (playlist edits)" status={authStatus.write_path.status} />
            <StatusRow
              label="Detection path (new likes)"
              status={authStatus.detection_path.status}
            />
            <StatusRow label="Last ingestion check" status={authStatus.youtube_detection.status} />
            <StatusRow label="LLM BPM estimate" status={authStatus.llm_bpm_estimate.status} />
            <StatusRow
              label="LLM description match"
              status={authStatus.llm_description_match.status}
            />
            <StatusRow label="LLM playlist clustering" status={authStatus.llm_clustering.status} />
            <StatusRow label="Last.fm genre lookup" status={authStatus.lastfm.status} />
            <StatusRow label="GetSongBPM tempo lookup" status={authStatus.getsongbpm.status} />
          </ul>
        )}
        {/* Plain <a> (full-page navigation), not a fetch: Google's consent
            screen can only be reached by the browser actually navigating
            there, and the CSRF guard in app/main.py exempts GET anyway. */}
        <a href="/api/v1/auth/youtube/authorize" className={`mt-4 inline-block ${PRIMARY_BUTTON}`}>
          Connect Google account (new-likes detection)
        </a>
      </section>

      <section className={CARD}>
        <h2 className="mb-3 text-sm font-semibold text-slate-700">Create a playlist by description</h2>
        <form onSubmit={(event) => void handleCreatePlaylist(event)} className="flex flex-col gap-3">
          <label className="flex flex-col gap-1 text-sm text-slate-600">
            Name
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              required
              className={INPUT}
            />
          </label>
          <label className="flex flex-col gap-1 text-sm text-slate-600">
            Description
            <textarea
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              required
              className={INPUT}
            />
          </label>
          <button type="submit" className={PRIMARY_BUTTON}>
            Create
          </button>
        </form>
      </section>

      {message && (
        <p
          role="status"
          className="rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm text-slate-700 shadow-sm"
        >
          {message}
        </p>
      )}
    </div>
  );
}
