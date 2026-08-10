import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as client from "../src/api/client";
import { ReviewQueue } from "../src/pages/ReviewQueue";

vi.mock("../src/api/client");

const baseAuthStatus: client.AuthStatus = {
  write_path: { status: "ok", reason: null },
  detection_path: { status: "ok", reason: null },
  youtube_detection: { status: "ok", reason: null },
  llm_description_match: { status: "ok", reason: null },
  llm_clustering: { status: "ok", reason: null },
  lastfm: { status: "ok", reason: null },
};

function makeItem(overrides: Partial<client.ReviewQueueItem> = {}): client.ReviewQueueItem {
  return {
    id: 1,
    library_item_id: 1,
    playlist_id: 10,
    status: "pending",
    version: 1,
    confidence: 0.8,
    explanation: { signal: "artist_similarity", detail: "matched" },
    title: "Test Song",
    artist: "Test Artist",
    playlist_name: "Test Playlist",
    reorganize_session_id: null,
    ...overrides,
  };
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(client.fetchPlaylists).mockResolvedValue([]);
  // Polled unconditionally on every mount (no session id to gate it, unlike
  // matching) -- every test needs a default so an unmocked call doesn't
  // resolve to undefined and throw when the page reads its fields.
  vi.mocked(client.fetchIngestionStatus).mockResolvedValue({
    ingestion_status: "idle",
    ingestion_processed_count: 0,
    ingestion_total_count: 0,
  });
  // A session id written by one test must not leak into the next and get
  // restored on mount unexpectedly (mirrors Onboarding.test.tsx).
  localStorage.clear();
});

function makeReorganizeStatus(
  overrides: Partial<client.ReorganizeStatus> = {},
): client.ReorganizeStatus {
  return {
    session_id: 42,
    clustering_status: "done",
    proposals: [],
    enriched_count: 0,
    total_count: 0,
    matching_status: "idle",
    matched_count: 0,
    ...overrides,
  };
}

describe("ReviewQueue", () => {
  it("shows the playlist name in the section header, not just its id", async () => {
    const item = makeItem({ playlist_id: 10, playlist_name: "J-Pop & J-Rock Anthems" });
    vi.mocked(client.fetchReviewQueue).mockResolvedValue([item]);
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);

    render(<ReviewQueue />);

    await screen.findByTestId("queue-item-1");
    expect(screen.getByText("J-Pop & J-Rock Anthems")).toBeInTheDocument();
    expect(screen.queryByText("Playlist #10")).not.toBeInTheDocument();
  });

  it("falls back to the playlist id when no name is available, and labels unassigned songs", async () => {
    const named = makeItem({ id: 1, playlist_id: 10, playlist_name: null });
    const unassigned = makeItem({
      id: 2,
      library_item_id: 2,
      playlist_id: null,
      playlist_name: null,
    });
    vi.mocked(client.fetchReviewQueue).mockResolvedValue([named, unassigned]);
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);

    render(<ReviewQueue />);

    await screen.findByTestId("queue-item-1");
    expect(screen.getByText("Playlist #10")).toBeInTheDocument();
    expect(screen.getByText("Unassigned")).toBeInTheDocument();
  });

  it("shows the song title and artist for each queue item", async () => {
    const item = makeItem({ title: "Unravel", artist: "TK from Ling Tosite Sigure" });
    vi.mocked(client.fetchReviewQueue).mockResolvedValue([item]);
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);

    render(<ReviewQueue />);

    const listItem = await screen.findByTestId("queue-item-1");
    expect(listItem).toHaveTextContent("Unravel");
    expect(listItem).toHaveTextContent("TK from Ling Tosite Sigure");
  });

  it("approves every song in a playlist group when Approve all is clicked", async () => {
    const first = makeItem({ id: 1, playlist_id: 10, version: 1 });
    const second = makeItem({ id: 2, library_item_id: 2, playlist_id: 10, version: 3 });
    vi.mocked(client.fetchReviewQueue).mockResolvedValue([first, second]);
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);
    vi.mocked(client.approveItem).mockResolvedValue({ ...first, status: "approved" });

    render(<ReviewQueue />);
    await screen.findByTestId("queue-item-1");

    const user = userEvent.setup();
    await user.click(screen.getByText("Approve all (2)"));

    await waitFor(() => {
      expect(screen.queryByTestId("queue-item-1")).not.toBeInTheDocument();
      expect(screen.queryByTestId("queue-item-2")).not.toBeInTheDocument();
    });
    expect(client.approveItem).toHaveBeenCalledWith(1, 1);
    expect(client.approveItem).toHaveBeenCalledWith(2, 3);
  });

  it("keeps songs that failed to approve visible after a partial Approve all failure", async () => {
    const succeeds = makeItem({ id: 1, playlist_id: 10, version: 1 });
    const fails = makeItem({ id: 2, library_item_id: 2, playlist_id: 10, version: 1 });
    vi.mocked(client.fetchReviewQueue).mockResolvedValue([succeeds, fails]);
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);
    vi.mocked(client.approveItem).mockImplementation((id) =>
      id === 1
        ? Promise.resolve({ ...succeeds, status: "approved" })
        : Promise.reject(new Error("network error")),
    );

    render(<ReviewQueue />);
    await screen.findByTestId("queue-item-1");

    const user = userEvent.setup();
    await user.click(screen.getByText("Approve all (2)"));

    await waitFor(() => {
      expect(screen.queryByTestId("queue-item-1")).not.toBeInTheDocument();
    });
    expect(screen.getByTestId("queue-item-2")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("1 of 2 song(s) failed to approve");
  });

  it("removes an item from the visible queue after rejecting it", async () => {
    const item = makeItem();
    vi.mocked(client.fetchReviewQueue).mockResolvedValue([item]);
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);
    vi.mocked(client.rejectItem).mockResolvedValue({ ...item, status: "rejected" });

    render(<ReviewQueue />);

    await screen.findByTestId("queue-item-1");

    const user = userEvent.setup();
    await user.click(screen.getByText("Reject"));

    await waitFor(() => {
      expect(screen.queryByTestId("queue-item-1")).not.toBeInTheDocument();
    });
    expect(client.rejectItem).toHaveBeenCalledWith(1, 1);
  });

  it("moves an unassigned song into the chosen playlist's group, still pending approval", async () => {
    const item = makeItem({ id: 1, playlist_id: null, playlist_name: null });
    vi.mocked(client.fetchReviewQueue).mockResolvedValue([item]);
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);
    vi.mocked(client.fetchPlaylists).mockResolvedValue([
      { id: 20, name: "Chill", description: null, rule: null, source: null },
    ]);
    // Same id back -- a reassignment of the existing row, not a new one.
    vi.mocked(client.addToPlaylist).mockResolvedValue({
      ...item,
      playlist_id: 20,
      playlist_name: "Chill",
      status: "pending",
    });

    render(<ReviewQueue />);
    await screen.findByTestId("queue-item-1");
    expect(screen.getByText("Unassigned")).toBeInTheDocument();

    const user = userEvent.setup();
    await user.selectOptions(screen.getByLabelText('Add "Test Song" to a playlist'), "20");

    expect(client.addToPlaylist).toHaveBeenCalledWith(1, 1, 20);
    await waitFor(() => {
      expect(screen.getByText("Chill")).toBeInTheDocument();
    });
    // No eager write -- it's just re-grouped, still visible and pending.
    expect(screen.getByTestId("queue-item-1")).toBeInTheDocument();
    expect(screen.queryByText("Unassigned")).not.toBeInTheDocument();
  });

  it("adds an independent pending candidate for an already-assigned song without touching the original", async () => {
    const item = makeItem({ id: 1, playlist_id: 10, playlist_name: "Rock", library_item_id: 1 });
    vi.mocked(client.fetchReviewQueue).mockResolvedValue([item]);
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);
    vi.mocked(client.fetchPlaylists).mockResolvedValue([
      { id: 10, name: "Rock", description: null, rule: null, source: null },
      { id: 20, name: "Chill", description: null, rule: null, source: null },
    ]);
    // A different id -- an independent second candidate row.
    vi.mocked(client.addToPlaylist).mockResolvedValue({
      ...item,
      id: 2,
      playlist_id: 20,
      playlist_name: "Chill",
    });

    render(<ReviewQueue />);
    await screen.findByTestId("queue-item-1");

    const user = userEvent.setup();
    await user.selectOptions(screen.getByLabelText('Add "Test Song" to a playlist'), "20");

    expect(client.addToPlaylist).toHaveBeenCalledWith(1, 1, 20);
    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent(
        'Added "Test Song" to Chill — awaiting its own approval.',
      );
    });
    // Both the original (still under Rock) and the new candidate (under
    // Chill) are visible -- neither was written or removed.
    expect(screen.getByTestId("queue-item-1")).toBeInTheDocument();
    expect(screen.getByTestId("queue-item-2")).toBeInTheDocument();
    expect(screen.getByText("Rock")).toBeInTheDocument();
    expect(screen.getByText("Chill")).toBeInTheDocument();
  });

  it("does not offer a song's own playlist as an add-to-playlist option", async () => {
    const item = makeItem({ id: 1, playlist_id: 10, playlist_name: "Rock" });
    vi.mocked(client.fetchReviewQueue).mockResolvedValue([item]);
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);
    vi.mocked(client.fetchPlaylists).mockResolvedValue([
      { id: 10, name: "Rock", description: null, rule: null, source: null },
      { id: 20, name: "Chill", description: null, rule: null, source: null },
    ]);

    render(<ReviewQueue />);
    await screen.findByTestId("queue-item-1");

    const select = screen.getByLabelText('Add "Test Song" to a playlist');
    expect(screen.queryByRole("option", { name: "Rock" })).not.toBeInTheDocument();
    expect(select).toHaveTextContent("Chill");
  });

  it("triggers a background ingestion run and shows a completion message once it finishes", async () => {
    const item = makeItem();
    vi.mocked(client.fetchReviewQueue).mockResolvedValue([item]);
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);
    vi.mocked(client.checkForNewSongs).mockResolvedValue({
      ran: true,
      mode: "triggered",
      ingestion_status: "in_progress",
    });

    render(<ReviewQueue />);
    await screen.findByTestId("queue-item-1");

    vi.mocked(client.fetchIngestionStatus).mockResolvedValueOnce({
      ingestion_status: "done",
      ingestion_processed_count: 2,
      ingestion_total_count: 2,
    });

    const user = userEvent.setup();
    await user.click(screen.getByText("Load new songs"));

    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent(
        "Loaded 2 new song(s) into the review queue.",
      );
    });
    // The queue reloads once the run completes, on top of the initial mount fetch.
    expect(client.fetchReviewQueue).toHaveBeenCalledTimes(2);
  });

  it("shows live ingestion progress while a background run is in progress", async () => {
    vi.mocked(client.fetchReviewQueue).mockResolvedValue([]);
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);
    vi.mocked(client.fetchIngestionStatus).mockResolvedValue({
      ingestion_status: "in_progress",
      ingestion_processed_count: 30,
      ingestion_total_count: 120,
    });

    render(<ReviewQueue />);

    const progressBar = await screen.findByRole("progressbar", { name: "New song loading progress" });
    expect(progressBar).toHaveAttribute("value", "30");
    expect(progressBar).toHaveAttribute("max", "120");
    expect(screen.getByText("30 / 120 songs (25%)")).toBeInTheDocument();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("shows a friendly message instead of triggering when onboarding isn't finished", async () => {
    vi.mocked(client.fetchReviewQueue).mockResolvedValue([]);
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);
    vi.mocked(client.checkForNewSongs).mockResolvedValue({
      ran: false,
      mode: "waiting_for_onboarding",
      ingestion_status: "idle",
    });

    render(<ReviewQueue />);
    await screen.findByText("Nothing to review right now.");

    const user = userEvent.setup();
    await user.click(screen.getByText("Load new songs"));

    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent(
        "Finish onboarding before loading new songs.",
      );
    });
  });

  it("resets the backlog and reloads the queue after confirming", async () => {
    const item = makeItem();
    vi.mocked(client.fetchReviewQueue).mockResolvedValue([item]);
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);
    vi.mocked(client.resetBacklog).mockResolvedValue({ library_items_cleared: 3 });
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);

    render(<ReviewQueue />);
    await screen.findByTestId("queue-item-1");

    const user = userEvent.setup();
    await user.click(screen.getByText("Restart batches from the beginning"));

    expect(confirmSpy).toHaveBeenCalled();
    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent(
        'Reset 3 song(s) — click "Load new songs" to start batching from the beginning again.',
      );
    });
    expect(client.resetBacklog).toHaveBeenCalled();
    // The queue reloads after resetting, on top of the initial mount fetch.
    expect(client.fetchReviewQueue).toHaveBeenCalledTimes(2);

    confirmSpy.mockRestore();
  });

  it("does not reset the backlog when the confirmation is declined", async () => {
    const item = makeItem();
    vi.mocked(client.fetchReviewQueue).mockResolvedValue([item]);
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);

    render(<ReviewQueue />);
    await screen.findByTestId("queue-item-1");

    const user = userEvent.setup();
    await user.click(screen.getByText("Restart batches from the beginning"));

    expect(client.resetBacklog).not.toHaveBeenCalled();

    confirmSpy.mockRestore();
  });

  it("shows only the write-path banner when just the write path is degraded", async () => {
    vi.mocked(client.fetchReviewQueue).mockResolvedValue([]);
    vi.mocked(client.fetchAuthStatus).mockResolvedValue({
      ...baseAuthStatus,
      write_path: { status: "needs_reconnect", reason: "cookie expired" },
    });

    render(<ReviewQueue />);

    await screen.findByTestId("write-path-banner");
    expect(screen.queryByTestId("detection-path-banner")).not.toBeInTheDocument();
  });

  it("shows only the detection-path banner when just the detection path is degraded", async () => {
    vi.mocked(client.fetchReviewQueue).mockResolvedValue([]);
    vi.mocked(client.fetchAuthStatus).mockResolvedValue({
      ...baseAuthStatus,
      detection_path: { status: "needs_reconnect", reason: "token revoked" },
    });

    render(<ReviewQueue />);

    await screen.findByTestId("detection-path-banner");
    expect(screen.queryByTestId("write-path-banner")).not.toBeInTheDocument();
  });

  it("shows the reorganize session banner while a non-terminal session item exists, and hides it once it's terminal", async () => {
    const sessionItem = makeItem({ id: 1, status: "pending", reorganize_session_id: 42 });
    vi.mocked(client.fetchReviewQueue).mockResolvedValue([sessionItem]);
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);
    vi.mocked(client.rejectItem).mockResolvedValue({ ...sessionItem, status: "rejected" });

    render(<ReviewQueue />);
    await screen.findByTestId("reorganize-session-banner");
    expect(screen.getByText("Finish & Apply")).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByText("Reject"));

    await waitFor(() => {
      expect(screen.queryByTestId("reorganize-session-banner")).not.toBeInTheDocument();
    });
  });

  it("does not show the reorganize session banner when no item belongs to an open session", async () => {
    const item = makeItem({ id: 1, status: "pending", reorganize_session_id: null });
    vi.mocked(client.fetchReviewQueue).mockResolvedValue([item]);
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);

    render(<ReviewQueue />);
    await screen.findByTestId("queue-item-1");

    expect(screen.queryByTestId("reorganize-session-banner")).not.toBeInTheDocument();
  });

  it("triggers Finish & Apply, polls until it finishes, and keeps the failure summary visible alongside a still-open banner", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const pendingItem = makeItem({ id: 1, status: "pending", reorganize_session_id: 42 });
      vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);
      vi.mocked(client.fetchReviewQueue)
        .mockResolvedValueOnce([pendingItem])
        // Post-apply reload: this item's write failed and reverted to
        // approved_pending_apply -- the session is still open (non-terminal).
        .mockResolvedValueOnce([{ ...pendingItem, status: "approved_pending_apply" }]);
      vi.mocked(client.triggerFinishAndApply).mockResolvedValue({
        session_id: 42,
        apply_status: "in_progress",
      });
      vi.mocked(client.fetchApplyStatus)
        .mockResolvedValueOnce({ session_id: 42, apply_status: "in_progress", apply_last_result: null })
        .mockResolvedValueOnce({
          session_id: 42,
          apply_status: "idle",
          apply_last_result: { succeeded: 1, failed: 1, failed_item_ids: [2], remaining: 1 },
        });

      render(<ReviewQueue />);
      await screen.findByTestId("reorganize-session-banner");

      const user = userEvent.setup({ delay: null });
      await user.click(screen.getByText("Finish & Apply"));

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1500);
      });

      await waitFor(() => {
        expect(screen.getByTestId("apply-result")).toHaveTextContent("1 song(s) added");
      });
      expect(screen.getByTestId("apply-result")).toHaveTextContent(
        "1 failed and will retry on the next apply",
      );
      // The reverted item is still non-terminal, so the banner stays up too.
      expect(screen.getByTestId("reorganize-session-banner")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("names the failed songs in the apply-result banner when they're still in the queue", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const pendingItem = makeItem({
        id: 2,
        title: "Song That Failed",
        status: "pending",
        reorganize_session_id: 42,
      });
      vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);
      vi.mocked(client.fetchReviewQueue)
        .mockResolvedValueOnce([pendingItem])
        .mockResolvedValueOnce([{ ...pendingItem, status: "approved_pending_apply" }]);
      vi.mocked(client.triggerFinishAndApply).mockResolvedValue({
        session_id: 42,
        apply_status: "in_progress",
      });
      vi.mocked(client.fetchApplyStatus).mockResolvedValueOnce({
        session_id: 42,
        apply_status: "idle",
        apply_last_result: { succeeded: 0, failed: 1, failed_item_ids: [2], remaining: 1 },
      });

      render(<ReviewQueue />);
      await screen.findByTestId("reorganize-session-banner");

      const user = userEvent.setup({ delay: null });
      await user.click(screen.getByText("Finish & Apply"));

      await waitFor(() => {
        expect(screen.getByTestId("apply-result")).toHaveTextContent("Song That Failed");
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it("cancels an open reorganize session and hides the banner once its items are terminal", async () => {
    const sessionItem = makeItem({ id: 1, status: "pending", reorganize_session_id: 42 });
    vi.mocked(client.fetchReviewQueue)
      .mockResolvedValueOnce([sessionItem])
      .mockResolvedValueOnce([{ ...sessionItem, status: "rejected" }]);
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);
    vi.mocked(client.cancelReorganize).mockResolvedValue({
      session_id: 42,
      clustering_status: "cancelled",
    });
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);

    render(<ReviewQueue />);
    await screen.findByTestId("reorganize-session-banner");

    const user = userEvent.setup();
    await user.click(screen.getByText("Cancel session"));

    expect(confirmSpy).toHaveBeenCalled();
    expect(client.cancelReorganize).toHaveBeenCalledWith(42);
    await waitFor(() => {
      expect(screen.queryByTestId("reorganize-session-banner")).not.toBeInTheDocument();
    });

    confirmSpy.mockRestore();
  });

  it("does not cancel the session when the confirmation is declined", async () => {
    const sessionItem = makeItem({ id: 1, status: "pending", reorganize_session_id: 42 });
    vi.mocked(client.fetchReviewQueue).mockResolvedValue([sessionItem]);
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);

    render(<ReviewQueue />);
    await screen.findByTestId("reorganize-session-banner");

    const user = userEvent.setup();
    await user.click(screen.getByText("Cancel session"));

    expect(client.cancelReorganize).not.toHaveBeenCalled();
    expect(screen.getByTestId("reorganize-session-banner")).toBeInTheDocument();

    confirmSpy.mockRestore();
  });

  it("shows live matching progress for a session left running by Onboarding", async () => {
    localStorage.setItem("yt-music-organizer:reorganizeSessionId", "42");
    vi.mocked(client.fetchReviewQueue).mockResolvedValue([]);
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);
    vi.mocked(client.fetchReorganizeStatus).mockResolvedValue(
      makeReorganizeStatus({ matching_status: "in_progress", matched_count: 25, total_count: 100 }),
    );

    render(<ReviewQueue />);

    const progressBar = await screen.findByRole("progressbar", { name: "Library matching progress" });
    expect(progressBar).toHaveAttribute("value", "25");
    expect(progressBar).toHaveAttribute("max", "100");
    expect(screen.getByText("25 / 100 songs (25%)")).toBeInTheDocument();
    expect(client.fetchReorganizeStatus).toHaveBeenCalledWith(42);
  });

  it("hides the matching progress and reloads the queue once matching finishes", async () => {
    localStorage.setItem("yt-music-organizer:reorganizeSessionId", "42");
    const matchedItem = makeItem({ id: 1, reorganize_session_id: 42 });
    vi.mocked(client.fetchReviewQueue)
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([matchedItem]);
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);
    vi.mocked(client.fetchReorganizeStatus).mockResolvedValue(
      makeReorganizeStatus({ matching_status: "done", matched_count: 100, total_count: 100 }),
    );

    render(<ReviewQueue />);

    await screen.findByTestId("queue-item-1");
    expect(client.fetchReviewQueue).toHaveBeenCalledTimes(2);
    expect(screen.queryByTestId("matching-progress")).not.toBeInTheDocument();
  });

  it("stops tracking matching progress and clears the stored session id once it's cancelled", async () => {
    localStorage.setItem("yt-music-organizer:reorganizeSessionId", "42");
    const sessionItem = makeItem({ id: 1, status: "pending", reorganize_session_id: 42 });
    vi.mocked(client.fetchReviewQueue)
      .mockResolvedValueOnce([sessionItem])
      .mockResolvedValueOnce([{ ...sessionItem, status: "rejected" }]);
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);
    vi.mocked(client.fetchReorganizeStatus).mockResolvedValue(
      makeReorganizeStatus({ matching_status: "in_progress", matched_count: 10, total_count: 50 }),
    );
    vi.mocked(client.cancelReorganize).mockResolvedValue({
      session_id: 42,
      clustering_status: "cancelled",
    });
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);

    render(<ReviewQueue />);
    await screen.findByTestId("matching-progress");

    const user = userEvent.setup();
    await user.click(screen.getByText("Cancel session"));

    await waitFor(() => {
      expect(screen.queryByTestId("matching-progress")).not.toBeInTheDocument();
    });
    expect(localStorage.getItem("yt-music-organizer:reorganizeSessionId")).toBeNull();

    confirmSpy.mockRestore();
  });
});
