import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as client from "../src/api/client";
import { Onboarding } from "../src/pages/Onboarding";

vi.mock("../src/api/client");

beforeEach(() => {
  vi.resetAllMocks();
});

describe("Onboarding", () => {
  it("renders existing YouTube playlists, already-added playlists, and suggested proposals once loaded", async () => {
    vi.mocked(client.fetchOnboardingAnalysis).mockResolvedValue({
      proposals: [{ name: "Chill Vibes", theme: "lofi", song_count: 12, confidence: 0.8 }],
      existing_playlists: [{ playlist_id: "yt-1", title: "Road Trip" }],
      added_playlists: [{ id: 1, name: "Workout", description: null, rule: null }],
    });

    render(<Onboarding />);

    await screen.findByText("Road Trip");
    expect(screen.getByText("Workout")).toBeInTheDocument();
    expect(screen.getByText("Chill Vibes")).toBeInTheDocument();
  });

  it("shows a loading state before the analysis resolves", () => {
    vi.mocked(client.fetchOnboardingAnalysis).mockReturnValue(new Promise(() => {}));

    render(<Onboarding />);

    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("toggles a proposal's accepted state on checkbox click", async () => {
    vi.mocked(client.fetchOnboardingAnalysis).mockResolvedValue({
      proposals: [{ name: "Chill Vibes", theme: "lofi", song_count: 12, confidence: 0.8 }],
      existing_playlists: [],
      added_playlists: [],
    });

    render(<Onboarding />);
    await screen.findByText("Chill Vibes");

    const checkbox = screen.getByRole("checkbox");
    expect(checkbox).not.toBeChecked();

    const user = userEvent.setup();
    await user.click(checkbox);

    expect(checkbox).toBeChecked();
  });

  it("toggles an existing YouTube playlist's adopted state and submits it on finish", async () => {
    vi.mocked(client.fetchOnboardingAnalysis).mockResolvedValue({
      proposals: [],
      existing_playlists: [{ playlist_id: "yt-1", title: "Road Trip" }],
      added_playlists: [],
    });
    vi.mocked(client.submitOnboardingSelection).mockResolvedValue({ created_playlists: [] });

    render(<Onboarding />);
    await screen.findByText("Road Trip");

    const checkbox = screen.getByRole("checkbox");
    expect(checkbox).not.toBeChecked();

    const user = userEvent.setup();
    await user.click(checkbox);
    expect(checkbox).toBeChecked();

    await user.click(screen.getByText("Finish setup"));

    expect(client.submitOnboardingSelection).toHaveBeenCalledWith(
      [],
      [],
      [{ playlist_id: "yt-1", name: "Road Trip" }],
      [],
      [],
    );
  });

  it("starts an already-added playlist checked, and submits its removal when unchecked", async () => {
    vi.mocked(client.fetchOnboardingAnalysis).mockResolvedValue({
      proposals: [],
      existing_playlists: [],
      added_playlists: [{ id: 7, name: "Workout", description: null, rule: null }],
    });
    vi.mocked(client.submitOnboardingSelection).mockResolvedValue({ created_playlists: [] });

    render(<Onboarding />);
    await screen.findByText("Workout");

    const checkbox = screen.getByRole("checkbox");
    expect(checkbox).toBeChecked();

    const user = userEvent.setup();
    await user.click(checkbox);
    expect(checkbox).not.toBeChecked();

    await user.click(screen.getByText("Finish setup"));

    expect(client.submitOnboardingSelection).toHaveBeenCalledWith([], [], [], [7], []);
  });

  it("adds a custom playlist as a chip and clears the form", async () => {
    vi.mocked(client.fetchOnboardingAnalysis).mockResolvedValue({
      proposals: [],
      existing_playlists: [],
      added_playlists: [],
    });

    render(<Onboarding />);
    await screen.findByText("No new-playlist suggestions found.");

    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Name"), "Road Trip");
    await user.type(screen.getByLabelText("Description"), "for driving");
    await user.click(screen.getByText("Add"));

    expect(screen.getByText("Road Trip")).toBeInTheDocument();
    expect(screen.getByLabelText("Name")).toHaveValue("");
  });

  it("streams reorganize suggestions across multiple polls without re-ordering earlier ones", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      vi.mocked(client.fetchOnboardingAnalysis).mockResolvedValue({
        proposals: [],
        existing_playlists: [],
        added_playlists: [],
      });
      vi.mocked(client.triggerReorganize).mockResolvedValue({
        session_id: 1,
        clustering_status: "in_progress",
      });
      vi.mocked(client.fetchReorganizeStatus)
        .mockResolvedValueOnce({
          session_id: 1,
          clustering_status: "in_progress",
          proposals: [{ name: "90s R&B", theme: "throwback grooves", song_count: 8 }],
        })
        .mockResolvedValueOnce({
          session_id: 1,
          clustering_status: "done",
          proposals: [
            { name: "90s R&B", theme: "throwback grooves", song_count: 8 },
            { name: "Chill Electronic", theme: "downtempo", song_count: 5 },
          ],
        });

      render(<Onboarding />);
      await screen.findByText("No new-playlist suggestions found.");

      const user = userEvent.setup({ delay: null });
      await user.click(screen.getByText("Reorganize My Library"));

      await act(async () => {
        await vi.advanceTimersByTimeAsync(2000);
      });
      expect(screen.getByText("90s R&B", { exact: false })).toBeInTheDocument();
      expect(screen.queryByText("Chill Electronic", { exact: false })).not.toBeInTheDocument();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(2000);
      });
      expect(screen.getByText("90s R&B", { exact: false })).toBeInTheDocument();
      expect(screen.getByText("Chill Electronic", { exact: false })).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows an explicit empty state when a reorganize run completes with no proposals", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      vi.mocked(client.fetchOnboardingAnalysis).mockResolvedValue({
        proposals: [{ name: "Stale Suggestion", theme: "old", song_count: 6, confidence: 0.5 }],
        existing_playlists: [],
        added_playlists: [],
      });
      vi.mocked(client.triggerReorganize).mockResolvedValue({
        session_id: 1,
        clustering_status: "in_progress",
      });
      vi.mocked(client.fetchReorganizeStatus).mockResolvedValue({
        session_id: 1,
        clustering_status: "done",
        proposals: [],
      });

      render(<Onboarding />);
      await screen.findByText("Stale Suggestion");

      const user = userEvent.setup({ delay: null });
      await user.click(screen.getByText("Reorganize My Library"));

      await act(async () => {
        await vi.advanceTimersByTimeAsync(2000);
      });
      expect(screen.getByText("No new playlist suggestions this run.")).toBeInTheDocument();
      expect(screen.queryByText("Stale Suggestion")).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows a confirmation prompt naming the playlist and affected count before removing a playlist with pending work", async () => {
    vi.mocked(client.fetchOnboardingAnalysis).mockResolvedValue({
      proposals: [],
      existing_playlists: [],
      added_playlists: [{ id: 7, name: "Workout", description: null, rule: null }],
    });
    const confirmationError = Object.assign(new Error("conflict"), {
      body: {
        detail: {
          reason: "removal_requires_confirmation",
          playlist_id: 7,
          playlist_name: "Workout",
          pending_count: 3,
        },
      },
    });
    vi.mocked(client.submitOnboardingSelection)
      .mockRejectedValueOnce(confirmationError)
      .mockResolvedValueOnce({ created_playlists: [] });
    vi.mocked(client.asPlaylistRemovalConfirmation).mockImplementation((err) => {
      const body = (err as { body?: { detail?: unknown } }).body?.detail;
      return body && typeof body === "object" && (body as { reason?: string }).reason === "removal_requires_confirmation"
        ? (body as client.PlaylistRemovalConfirmation)
        : null;
    });
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);

    render(<Onboarding />);
    await screen.findByText("Workout");

    const user = userEvent.setup();
    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByText("Finish setup"));

    await vi.waitFor(() => expect(confirmSpy).toHaveBeenCalled());
    expect(confirmSpy.mock.calls[0][0]).toContain("Workout");
    expect(confirmSpy.mock.calls[0][0]).toContain("3");
    expect(client.submitOnboardingSelection).toHaveBeenLastCalledWith([], [], [], [7], [7]);

    confirmSpy.mockRestore();
  });

  it("does not resubmit the removal when the confirmation prompt is declined", async () => {
    vi.mocked(client.fetchOnboardingAnalysis).mockResolvedValue({
      proposals: [],
      existing_playlists: [],
      added_playlists: [{ id: 7, name: "Workout", description: null, rule: null }],
    });
    const confirmationError = Object.assign(new Error("conflict"), {
      body: {
        detail: {
          reason: "removal_requires_confirmation",
          playlist_id: 7,
          playlist_name: "Workout",
          pending_count: 3,
        },
      },
    });
    vi.mocked(client.submitOnboardingSelection).mockRejectedValueOnce(confirmationError);
    vi.mocked(client.asPlaylistRemovalConfirmation).mockImplementation((err) => {
      const body = (err as { body?: { detail?: unknown } }).body?.detail;
      return body && typeof body === "object" && (body as { reason?: string }).reason === "removal_requires_confirmation"
        ? (body as client.PlaylistRemovalConfirmation)
        : null;
    });
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);

    render(<Onboarding />);
    await screen.findByText("Workout");

    const user = userEvent.setup();
    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByText("Finish setup"));

    await vi.waitFor(() => expect(confirmSpy).toHaveBeenCalled());
    expect(client.submitOnboardingSelection).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();

    confirmSpy.mockRestore();
  });

  it("shows a running suggestion count while clustering is in progress", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      vi.mocked(client.fetchOnboardingAnalysis).mockResolvedValue({
        proposals: [],
        existing_playlists: [],
        added_playlists: [],
      });
      vi.mocked(client.triggerReorganize).mockResolvedValue({
        session_id: 1,
        clustering_status: "in_progress",
      });
      vi.mocked(client.fetchReorganizeStatus).mockResolvedValue({
        session_id: 1,
        clustering_status: "in_progress",
        proposals: [{ name: "90s R&B", theme: "throwback grooves", song_count: 8 }],
      });

      render(<Onboarding />);
      await screen.findByText("No new-playlist suggestions found.");

      const user = userEvent.setup({ delay: null });
      await user.click(screen.getByText("Reorganize My Library"));

      await act(async () => {
        await vi.advanceTimersByTimeAsync(2000);
      });
      expect(screen.getByText("1 suggestion(s) found so far", { exact: false })).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps earlier suggestions after retrying the same stalled session", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      vi.mocked(client.fetchOnboardingAnalysis).mockResolvedValue({
        proposals: [],
        existing_playlists: [],
        added_playlists: [],
      });
      vi.mocked(client.triggerReorganize).mockResolvedValue({
        session_id: 1,
        clustering_status: "in_progress",
      });
      vi.mocked(client.fetchReorganizeStatus).mockResolvedValueOnce({
        session_id: 1,
        clustering_status: "stalled",
        proposals: [{ name: "90s R&B", theme: "throwback grooves", song_count: 8 }],
      });

      render(<Onboarding />);
      await screen.findByText("No new-playlist suggestions found.");

      const user = userEvent.setup({ delay: null });
      await user.click(screen.getByText("Reorganize My Library"));

      await act(async () => {
        await vi.advanceTimersByTimeAsync(2000);
      });
      await screen.findByText("Try again");
      expect(screen.getByText("90s R&B", { exact: false })).toBeInTheDocument();

      // Retrying reuses the same (stalled) session -- the backend returns
      // the same session_id -- so the suggestion found above must survive.
      vi.mocked(client.fetchReorganizeStatus).mockResolvedValueOnce({
        session_id: 1,
        clustering_status: "done",
        proposals: [{ name: "90s R&B", theme: "throwback grooves", song_count: 8 }],
      });
      await user.click(screen.getByText("Try again"));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2000);
      });

      expect(screen.getByText("90s R&B", { exact: false })).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows classified-song progress while matching runs", async () => {
    vi.mocked(client.fetchOnboardingAnalysis).mockResolvedValue({
      proposals: [],
      existing_playlists: [],
      added_playlists: [],
    });
    vi.mocked(client.triggerReorganize).mockResolvedValue({
      session_id: 1,
      clustering_status: "done",
    });
    vi.mocked(client.fetchReorganizeStatus).mockResolvedValue({
      session_id: 1,
      clustering_status: "done",
      proposals: [],
    });
    let resolveBatch: (value: client.ReorganizeMatchResult) => void = () => {};
    vi.mocked(client.runReorganizeMatchBatch)
      .mockResolvedValueOnce({ ran: true, processed: 50, queue_items_created: 10, matching_complete: false })
      .mockImplementationOnce(
        () => new Promise((resolve) => { resolveBatch = resolve; }),
      );
    vi.mocked(client.submitOnboardingSelection).mockResolvedValue({ created_playlists: [] });

    render(<Onboarding />);
    await screen.findByText("No new-playlist suggestions found.");

    const user = userEvent.setup();
    await user.click(screen.getByText("Reorganize My Library"));
    await screen.findByText("No new playlist suggestions this run.");

    await user.click(screen.getByText("Finish setup"));

    await screen.findByText("Matching your library… (50 song(s) classified)");
    await act(async () => {
      resolveBatch({ ran: true, processed: 25, queue_items_created: 5, matching_complete: true });
      await Promise.resolve();
    });
    await screen.findByRole("status");
  });

  it("cancels an open reorganize session and reverts to the pre-reorganize state", async () => {
    vi.mocked(client.fetchOnboardingAnalysis).mockResolvedValue({
      proposals: [{ name: "Stale Suggestion", theme: "old", song_count: 6, confidence: 0.5 }],
      existing_playlists: [],
      added_playlists: [],
    });
    vi.mocked(client.triggerReorganize).mockResolvedValue({
      session_id: 1,
      clustering_status: "done",
    });
    vi.mocked(client.fetchReorganizeStatus).mockResolvedValue({
      session_id: 1,
      clustering_status: "done",
      proposals: [],
    });
    vi.mocked(client.cancelReorganize).mockResolvedValue({
      session_id: 1,
      clustering_status: "cancelled",
    });
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);

    render(<Onboarding />);
    await screen.findByText("Stale Suggestion");

    const user = userEvent.setup();
    await user.click(screen.getByText("Reorganize My Library"));
    await screen.findByText("Cancel this session");

    await user.click(screen.getByText("Cancel this session"));

    expect(confirmSpy).toHaveBeenCalled();
    expect(client.cancelReorganize).toHaveBeenCalledWith(1);
    await waitFor(() => {
      expect(screen.queryByText("Cancel this session")).not.toBeInTheDocument();
    });

    confirmSpy.mockRestore();
  });

  it("notes that approvals are deferred while a reorganize session is open", async () => {
    vi.mocked(client.fetchOnboardingAnalysis).mockResolvedValue({
      proposals: [],
      existing_playlists: [],
      added_playlists: [],
    });
    vi.mocked(client.triggerReorganize).mockResolvedValue({
      session_id: 1,
      clustering_status: "done",
    });
    vi.mocked(client.fetchReorganizeStatus).mockResolvedValue({
      session_id: 1,
      clustering_status: "done",
      proposals: [],
    });

    render(<Onboarding />);
    await screen.findByText("No new-playlist suggestions found.");
    expect(screen.queryByText(/won't be written to YouTube until/)).not.toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByText("Reorganize My Library"));

    await screen.findByText(/won't be written to YouTube until/);
  });

  it("shows the done confirmation without switching away in the same tick", async () => {
    vi.mocked(client.fetchOnboardingAnalysis).mockResolvedValue({
      proposals: [],
      existing_playlists: [],
      added_playlists: [],
    });
    vi.mocked(client.submitOnboardingSelection).mockResolvedValue({ created_playlists: [] });
    const onComplete = vi.fn();

    render(<Onboarding onComplete={onComplete} />);
    await screen.findByText("No new-playlist suggestions found.");

    const user = userEvent.setup();
    await user.click(screen.getByText("Finish setup"));

    await screen.findByRole("status");
    expect(screen.getByRole("status")).toHaveTextContent(
      "Playlists created — the backlog will now be organized for review.",
    );
    // onComplete is deferred, not called synchronously with setDone(true) --
    // calling it in the same tick would let the parent switch pages before
    // this "done" message ever gets a chance to paint.
    expect(onComplete).not.toHaveBeenCalled();
  });
});
