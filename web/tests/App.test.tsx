import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../src/App";
import * as client from "../src/api/client";

// U6: App.tsx now (a) auto-navigates into Organize immediately after
// Onboarding's "Finish setup" (R5/KD4), and (b) redirects a returning user
// with pending Organize work straight to Organize on load instead of
// defaulting to Playlists (R16/KTD7). Both behaviors are exercised here at
// the full-App level since that's where the redirect decision and the
// nav-page wiring actually live -- Onboarding.test.tsx separately covers the
// "no artificial delay" mechanism in isolation.
vi.mock("../src/api/client");

const baseAuthStatus: client.AuthStatus = {
  write_path: { status: "ok", reason: null },
  detection_path: { status: "ok", reason: null },
  youtube_detection: { status: "ok", reason: null },
  llm_description_match: { status: "ok", reason: null },
  llm_clustering: { status: "ok", reason: null },
  lastfm: { status: "ok", reason: null },
};

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

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();

  // Shared defaults for the shell (Layout) and whichever page mounts first
  // (Onboarding briefly mounts on every test here, since the default
  // landing page is "playlists" -- see App.tsx -- even when the mount check
  // is about to redirect away from it).
  vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);
  vi.mocked(client.fetchIngestionStatus).mockResolvedValue({
    ingestion_status: "idle",
    ingestion_processed_count: 0,
    ingestion_total_count: 0,
  });
  vi.mocked(client.fetchOnboardingProposals).mockResolvedValue({
    proposals_status: "done",
    proposals_processed_count: 0,
    proposals_total_count: 0,
    proposals: [],
  });
  vi.mocked(client.triggerOnboardingProposals).mockResolvedValue({
    proposals_status: "in_progress",
  });
  vi.mocked(client.fetchOnboardingPlaylists).mockResolvedValue({
    existing_playlists: [],
    added_playlists: [],
  });
  vi.mocked(client.fetchPlaylists).mockResolvedValue([]);
});

describe("App", () => {
  // R16/KTD7 regression guard: the default landing page stays Playlists
  // when the mount-time review-queue check finds nothing pending.
  it("stays on Playlists by default when there is no pending Organize work on load", async () => {
    vi.mocked(client.fetchReviewQueue).mockResolvedValue([]);

    render(<App />);

    await screen.findByText("Set up your playlists");
    await waitFor(() => expect(client.fetchReviewQueue).toHaveBeenCalled());
    // Still on Playlists after the check resolves -- Organize's page-level
    // heading (an <h1>, distinct from the always-present "Organize" nav
    // button) never appears.
    expect(screen.queryByRole("heading", { name: "Organize" })).not.toBeInTheDocument();
    expect(screen.getByText("Set up your playlists")).toBeInTheDocument();
  });

  // R16/KTD7: a returning user with pending Organize items skips Playlists
  // entirely and opens directly to Organize.
  it("opens directly to Organize when pending review items exist on load", async () => {
    vi.mocked(client.fetchReviewQueue).mockResolvedValue([
      makeReviewQueueItem({ status: "pending" }),
    ]);

    render(<App />);

    await screen.findByTestId("queue-item-1");
    expect(screen.queryByText("Set up your playlists")).not.toBeInTheDocument();
  });

  // R5/KD4: confirming playlist selection auto-navigates straight to
  // Organize, which then shows its own live (matching) progress -- no extra
  // click, no lingering "Playlists" screen.
  it("navigates to Organize immediately after finishing playlist selection, showing live progress there", async () => {
    // Nothing pending yet -- the mount-time redirect (R16) must not be what
    // lands this test on Organize; it's Onboarding's own "Finish setup"
    // completing that must do it (R5).
    vi.mocked(client.fetchReviewQueue).mockResolvedValue([]);
    vi.mocked(client.triggerReorganize).mockResolvedValue({
      session_id: 1,
      clustering_status: "done",
    });
    vi.mocked(client.fetchReorganizeStatus).mockResolvedValue({
      session_id: 1,
      clustering_status: "done",
      proposals: [],
      enriched_count: 0,
      total_count: 100,
      matching_status: "in_progress",
      matched_count: 10,
    });
    vi.mocked(client.submitOnboardingSelection).mockResolvedValue({ created_playlists: [] });
    vi.mocked(client.triggerReorganizeMatching).mockResolvedValue({
      session_id: 1,
      matching_status: "in_progress",
    });

    render(<App />);

    await screen.findByText("Set up your playlists");
    await screen.findByText("No new-playlist suggestions found.");

    const user = userEvent.setup();
    // Opens a reorganize session so Finish setup has something to trigger
    // matching for, and so the stored session id lets Organize pick up its
    // live matching progress right after the switch.
    await user.click(screen.getByText("Reorganize My Library"));
    await screen.findByText("No new playlist suggestions this run.");

    await user.click(screen.getByText("Finish setup"));

    // Landed on Organize (not back on Playlists), and it's already showing
    // live matching progress -- proving the switch happened immediately,
    // not after some extra click or a delay.
    await screen.findByTestId("matching-progress");
    expect(screen.queryByText("Set up your playlists")).not.toBeInTheDocument();
  });
});
