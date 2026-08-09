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
  llm_bpm_estimate: { status: "ok", reason: null },
  llm_description_match: { status: "ok", reason: null },
  llm_clustering: { status: "ok", reason: null },
  lastfm: { status: "ok", reason: null },
  getsongbpm: { status: "ok", reason: null },
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
});

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
      { id: 20, name: "Chill", description: null, rule: null },
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
      { id: 10, name: "Rock", description: null, rule: null },
      { id: 20, name: "Chill", description: null, rule: null },
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
      { id: 10, name: "Rock", description: null, rule: null },
      { id: 20, name: "Chill", description: null, rule: null },
    ]);

    render(<ReviewQueue />);
    await screen.findByTestId("queue-item-1");

    const select = screen.getByLabelText('Add "Test Song" to a playlist');
    expect(screen.queryByRole("option", { name: "Rock" })).not.toBeInTheDocument();
    expect(select).toHaveTextContent("Chill");
  });

  it("loads the next batch of songs and shows the result message", async () => {
    const item = makeItem();
    vi.mocked(client.fetchReviewQueue).mockResolvedValue([item]);
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);
    vi.mocked(client.checkForNewSongs).mockResolvedValue({
      ran: true,
      mode: "steady_state",
      new_songs_found: 2,
      queue_items_created: 1,
      backfill_complete: true,
    });

    render(<ReviewQueue />);
    await screen.findByTestId("queue-item-1");

    const user = userEvent.setup();
    await user.click(screen.getByText("Load next 50 songs"));

    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent(
        "Loaded 2 new song(s), 1 added to review queue.",
      );
    });
    // The queue reloads after checking, on top of the initial mount fetch.
    expect(client.fetchReviewQueue).toHaveBeenCalledTimes(2);
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
        'Reset 3 song(s) — click "Load next 50 songs" to start batching from the beginning again.',
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

  it("renders the BPM attribution credit only for a measured-tempo item", async () => {
    const measuredItem = makeItem({
      id: 1,
      explanation: { signal: "rule", detail: "bpm match", bpm: 128, bpm_source: "measured" },
    });
    const estimatedItem = makeItem({
      id: 2,
      library_item_id: 2,
      explanation: { signal: "rule", detail: "bpm match", bpm: 90, bpm_source: "estimated" },
    });
    vi.mocked(client.fetchReviewQueue).mockResolvedValue([measuredItem, estimatedItem]);
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);

    render(<ReviewQueue />);

    await screen.findByTestId("queue-item-1");
    const attributions = screen.getAllByTestId("bpm-attribution");
    expect(attributions).toHaveLength(1);
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
});
