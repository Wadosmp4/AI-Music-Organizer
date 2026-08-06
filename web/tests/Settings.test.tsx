import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as client from "../src/api/client";
import { Settings } from "../src/pages/Settings";

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

beforeEach(() => {
  vi.resetAllMocks();
  window.history.replaceState(null, "", "/");
});

describe("Settings", () => {
  it("renders a status row per connection-health field", async () => {
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);

    render(<Settings />);

    await screen.findByText("Write path (playlist edits)");
    expect(screen.getByText("Detection path (new likes)")).toBeInTheDocument();
    expect(screen.getByText("Last ingestion check")).toBeInTheDocument();
  });

  it("labels a non-ok status row correctly", async () => {
    vi.mocked(client.fetchAuthStatus).mockResolvedValue({
      ...baseAuthStatus,
      write_path: { status: "needs_reconnect", reason: "expired" },
    });

    render(<Settings />);

    await screen.findByText("Write path (playlist edits)");
    expect(screen.getByText("— needs reconnect")).toBeInTheDocument();
  });

  it("submits the create-playlist form and shows the resulting message", async () => {
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);
    vi.mocked(client.createPlaylist).mockResolvedValue({
      playlist: { id: 1, name: "Road Trip", description: null, rule: null },
      review_queue_items_created: 3,
    });

    render(<Settings />);
    await screen.findByText("Write path (playlist edits)");

    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Name"), "Road Trip");
    await user.type(screen.getByLabelText("Description"), "songs for driving");
    await user.click(screen.getByText("Create"));

    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent(
        'Created "Road Trip" — 3 songs proposed for review.',
      );
    });
    expect(client.createPlaylist).toHaveBeenCalledWith("Road Trip", "songs for driving");
  });

  it("runs a check-for-new-songs pass and shows the result message", async () => {
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);
    vi.mocked(client.checkForNewSongs).mockResolvedValue({
      ran: true,
      mode: "steady_state",
      new_songs_found: 2,
      queue_items_created: 1,
      backfill_complete: true,
    });

    render(<Settings />);
    await screen.findByText("Write path (playlist edits)");

    const user = userEvent.setup();
    await user.click(screen.getByText("Check now"));

    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent(
        "Check complete (steady_state): 2 new song(s), 1 added to review queue.",
      );
    });
  });

  it("shows the youtube_connect success message once and strips the query param", async () => {
    window.history.replaceState(null, "", "/?youtube_connect=success");
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);

    render(<Settings />);

    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent("YouTube account connected.");
    });
    expect(window.location.search).toBe("");
    // A single fetch, not one-per-branch -- the redundant post-redirect
    // refetch was removed because it could race the mount-time fetch.
    expect(client.fetchAuthStatus).toHaveBeenCalledTimes(1);
  });

  it("does not update state after unmount", async () => {
    let resolveFetch: (status: client.AuthStatus) => void = () => {};
    vi.mocked(client.fetchAuthStatus).mockReturnValue(
      new Promise((resolve) => {
        resolveFetch = resolve;
      }),
    );

    const { unmount } = render(<Settings />);
    unmount();

    expect(() => resolveFetch(baseAuthStatus)).not.toThrow();
  });
});
