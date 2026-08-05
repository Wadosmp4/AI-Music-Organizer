import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as client from "../src/api/client";
import { ReviewQueue } from "../src/pages/ReviewQueue";

vi.mock("../src/api/client");

const baseAuthStatus: client.AuthStatus = {
  write_path: { status: "ok", reason: null },
  detection_path: { status: "ok", reason: null },
  llm: { status: "ok", reason: null },
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
    ...overrides,
  };
}

beforeEach(() => {
  vi.resetAllMocks();
});

describe("ReviewQueue", () => {
  it("removes an item from the visible queue after approving it", async () => {
    const item = makeItem();
    vi.mocked(client.fetchReviewQueue).mockResolvedValue([item]);
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);
    vi.mocked(client.approveItem).mockResolvedValue({ ...item, status: "approved" });

    render(<ReviewQueue />);

    await screen.findByTestId("queue-item-1");

    const user = userEvent.setup();
    await user.click(screen.getByText("Approve"));

    await waitFor(() => {
      expect(screen.queryByTestId("queue-item-1")).not.toBeInTheDocument();
    });
    expect(client.approveItem).toHaveBeenCalledWith(1, 1);
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
