import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as client from "../src/api/client";
import { ReviewQueue } from "../src/pages/ReviewQueue";

vi.mock("../src/api/client");

// jsdom's DataTransfer doesn't persist data between dispatched events, so
// tests carry their own in-memory store matching the real setData/getData
// contract the component relies on.
function makeDataTransfer() {
  const store = new Map<string, string>();
  return {
    setData: (type: string, value: string) => store.set(type, value),
    getData: (type: string) => store.get(type) ?? "",
    dropEffect: "move",
    effectAllowed: "move",
  } as unknown as DataTransfer;
}

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
    ...overrides,
  };
}

beforeEach(() => {
  vi.resetAllMocks();
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

  it("moves a song to another playlist by dragging it onto that playlist's section", async () => {
    const dragged = makeItem({ id: 1, playlist_id: 10 });
    const other = makeItem({
      id: 2,
      library_item_id: 2,
      playlist_id: 20,
      title: "Other Song",
    });
    vi.mocked(client.fetchReviewQueue).mockResolvedValue([dragged, other]);
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);
    vi.mocked(client.moveItem).mockResolvedValue({ ...dragged, status: "moved", playlist_id: 20 });

    render(<ReviewQueue />);
    await screen.findByTestId("queue-item-1");

    const dataTransfer = makeDataTransfer();
    const draggedRow = screen.getByTestId("queue-item-1");
    const targetSection = screen.getByTestId("queue-item-2").closest("section")!;

    fireEvent.dragStart(draggedRow, { dataTransfer });
    fireEvent.drop(targetSection, { dataTransfer });

    await waitFor(() => {
      expect(screen.queryByTestId("queue-item-1")).not.toBeInTheDocument();
    });
    expect(client.moveItem).toHaveBeenCalledWith(1, 1, 20);
  });

  it("does not call moveItem when dropping a song onto its own playlist", async () => {
    const item = makeItem({ id: 1, playlist_id: 10 });
    vi.mocked(client.fetchReviewQueue).mockResolvedValue([item]);
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);

    render(<ReviewQueue />);
    await screen.findByTestId("queue-item-1");

    const dataTransfer = makeDataTransfer();
    const row = screen.getByTestId("queue-item-1");
    const section = row.closest("section")!;

    fireEvent.dragStart(row, { dataTransfer });
    fireEvent.drop(section, { dataTransfer });

    expect(client.moveItem).not.toHaveBeenCalled();
    expect(screen.getByTestId("queue-item-1")).toBeInTheDocument();
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
});
