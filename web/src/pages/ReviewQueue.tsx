import { useEffect, useState } from "react";

import {
  approveItem,
  fetchAuthStatus,
  fetchReviewQueue,
  moveItem,
  rejectItem,
  type AuthStatus,
  type ReviewQueueItem,
} from "../api/client";
import { BpmAttribution } from "../components/BpmAttribution";
import { ConnectionHealthBanners } from "../components/ConnectionHealthBanners";

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

export function ReviewQueue() {
  const [items, setItems] = useState<ReviewQueueItem[]>([]);
  const [authStatus, setAuthStatus] = useState<AuthStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    try {
      const [queue, status] = await Promise.all([fetchReviewQueue(), fetchAuthStatus()]);
      // Only pending items are actionable — write_pending/approved/moved/
      // rejected/stale items are no longer visible here (R11-R13).
      setItems(queue.filter((item) => item.status === "pending"));
      setAuthStatus(status);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  useEffect(() => {
    void load();
  }, []);

  async function handleApprove(item: ReviewQueueItem) {
    await approveItem(item.id, item.version);
    setItems((prev) => prev.filter((i) => i.id !== item.id));
  }

  async function handleReject(item: ReviewQueueItem) {
    await rejectItem(item.id, item.version);
    setItems((prev) => prev.filter((i) => i.id !== item.id));
  }

  async function handleMove(item: ReviewQueueItem, newPlaylistId: number) {
    await moveItem(item.id, item.version, newPlaylistId);
    setItems((prev) => prev.filter((i) => i.id !== item.id));
  }

  const groups = groupByPlaylist(items);

  return (
    <div className="review-queue">
      <h1>Review Queue</h1>
      <ConnectionHealthBanners authStatus={authStatus} />
      {error && <p role="alert">{error}</p>}
      {items.length === 0 && <p>Nothing to review right now.</p>}
      {[...groups.entries()].map(([playlistId, groupItems]) => (
        <section key={playlistId ?? "unassigned"}>
          <h2>Playlist #{playlistId ?? "unassigned"}</h2>
          <ul>
            {groupItems.map((item) => (
              <li key={item.id} data-testid={`queue-item-${item.id}`}>
                <div>
                  <strong>Confidence:</strong> {item.confidence?.toFixed(2) ?? "—"}
                </div>
                {item.explanation && (
                  <div className="explanation">
                    <em>{item.explanation.signal}</em>: {item.explanation.detail}
                  </div>
                )}
                {item.explanation?.bpm_source === "measured" && <BpmAttribution />}
                <button onClick={() => void handleApprove(item)}>Approve</button>
                <button onClick={() => void handleReject(item)}>Reject</button>
                <button
                  onClick={() => {
                    const target = window.prompt("Move to playlist id:");
                    if (target) void handleMove(item, Number(target));
                  }}
                >
                  Move
                </button>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
