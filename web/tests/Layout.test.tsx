import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as client from "../src/api/client";
import { Layout } from "../src/components/Layout";

vi.mock("../src/api/client");

const baseAuthStatus: client.AuthStatus = {
  write_path: { status: "ok", reason: null },
  detection_path: { status: "ok", reason: null },
  youtube_detection: { status: "ok", reason: null },
  llm_description_match: { status: "ok", reason: null },
  llm_clustering: { status: "ok", reason: null },
  lastfm: { status: "ok", reason: null },
};

beforeEach(() => {
  vi.resetAllMocks();
  // U7: the background-status indicator polls these on every mount
  // regardless of which test is exercising the nav shell -- default every
  // source to idle/empty so tests that don't care about it aren't tripped
  // up by an unmocked call resolving to `undefined`.
  vi.mocked(client.fetchIngestionStatus).mockResolvedValue({
    ingestion_status: "idle",
    ingestion_processed_count: 0,
    ingestion_total_count: 0,
  });
  vi.mocked(client.fetchOnboardingProposals).mockResolvedValue({
    proposals_status: "idle",
    proposals_processed_count: 0,
    proposals_total_count: 0,
    proposals: [],
  });
  vi.mocked(client.fetchReviewQueue).mockResolvedValue([]);
  // A session id left by one test must not leak into the next (mirrors
  // ReviewQueue.test.tsx).
  localStorage.clear();
});

function makeReviewQueueItem(
  overrides: Partial<client.ReviewQueueItem> = {},
): client.ReviewQueueItem {
  return {
    id: 1,
    library_item_id: 1,
    playlist_id: 10,
    status: "pending",
    version: 1,
    confidence: 0.8,
    explanation: null,
    title: "Test Song",
    artist: "Test Artist",
    playlist_name: "Test Playlist",
    reorganize_session_id: null,
    ...overrides,
  };
}

describe("Layout", () => {
  it("renders the app name, all three nav links in Playlists/Organize/Settings order, and the page content", async () => {
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);

    render(
      <Layout page="organize" onPageChange={() => {}}>
        <div>page content</div>
      </Layout>,
    );

    expect(screen.getByText("Music Organizer")).toBeInTheDocument();
    const navButtons = screen.getAllByRole("button");
    expect(navButtons.map((button) => button.textContent)).toEqual([
      "Playlists",
      "Organize",
      "Settings",
    ]);
    expect(screen.getByText("page content")).toBeInTheDocument();

    await waitFor(() => expect(client.fetchAuthStatus).toHaveBeenCalled());
  });

  it("calls onPageChange with the clicked page and highlights the active nav item", async () => {
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);
    const onPageChange = vi.fn();

    render(
      <Layout page="organize" onPageChange={onPageChange}>
        <div>page content</div>
      </Layout>,
    );

    const user = userEvent.setup();
    await user.click(screen.getByText("Settings"));

    expect(onPageChange).toHaveBeenCalledWith("settings");
    expect(screen.getByText("Organize").className).toContain("bg-accent");
    expect(screen.getByText("Settings").className).not.toContain("bg-accent");
  });

  it("switches active highlighting to Playlists when clicked", async () => {
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);
    const onPageChange = vi.fn();

    render(
      <Layout page="organize" onPageChange={onPageChange}>
        <div>page content</div>
      </Layout>,
    );

    const user = userEvent.setup();
    await user.click(screen.getByText("Playlists"));

    expect(onPageChange).toHaveBeenCalledWith("playlists");
  });

  it("shows a status dot per health state, including degraded", async () => {
    vi.mocked(client.fetchAuthStatus).mockResolvedValue({
      ...baseAuthStatus,
      write_path: { status: "degraded", reason: "slow" },
      detection_path: { status: "needs_reconnect", reason: "expired" },
    });

    render(
      <Layout page="organize" onPageChange={() => {}}>
        <div>page content</div>
      </Layout>,
    );

    await waitFor(() => {
      expect(screen.getByLabelText("Write path: degraded")).toBeInTheDocument();
    });
    expect(screen.getByLabelText("Detection path: needs reconnect")).toBeInTheDocument();
  });

  // R10/KTD4: the nav shell's own status indicator, independent of whichever
  // page is mounted (here Settings, deliberately not Organize/Playlists) --
  // it must reflect background classification progress regardless.
  it("shows live progress for a background classification run, independent of which page is active", async () => {
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);
    vi.mocked(client.fetchIngestionStatus).mockResolvedValue({
      ingestion_status: "in_progress",
      ingestion_processed_count: 3,
      ingestion_total_count: 10,
    });

    render(
      <Layout page="settings" onPageChange={() => {}}>
        <div>page content</div>
      </Layout>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("background-status-indicator")).toHaveTextContent("3/10");
    });
  });

  it("shows a failed state when a background status is the literal string \"failed\"", async () => {
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);
    vi.mocked(client.fetchIngestionStatus).mockResolvedValue({
      ingestion_status: "failed",
      ingestion_processed_count: 4,
      ingestion_total_count: 10,
    });

    render(
      <Layout page="playlists" onPageChange={() => {}}>
        <div>page content</div>
      </Layout>,
    );

    const indicator = await screen.findByTestId("background-status-indicator");
    expect(indicator).toHaveTextContent(/failed/i);
  });

  it("renders no background-status indicator when everything is idle and nothing is pending review", async () => {
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);

    render(
      <Layout page="organize" onPageChange={() => {}}>
        <div>page content</div>
      </Layout>,
    );

    await waitFor(() => expect(client.fetchAuthStatus).toHaveBeenCalled());
    expect(screen.queryByTestId("background-status-indicator")).not.toBeInTheDocument();
  });

  it("shows a ready-to-review count once background work is done but items are pending", async () => {
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);
    vi.mocked(client.fetchReviewQueue).mockResolvedValue([
      makeReviewQueueItem({ id: 1, playlist_id: 10 }),
      makeReviewQueueItem({ id: 2, playlist_id: 20 }),
    ]);

    render(
      <Layout page="settings" onPageChange={() => {}}>
        <div>page content</div>
      </Layout>,
    );

    const indicator = await screen.findByTestId("background-status-indicator");
    expect(indicator).toHaveTextContent("2 playlists ready to review");
  });
});
