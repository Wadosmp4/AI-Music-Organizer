import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as client from "../src/api/client";
import { Onboarding } from "../src/pages/Onboarding";
import * as reorganizeSession from "../src/reorganizeSession";

vi.mock("../src/api/client");

beforeEach(() => {
  vi.resetAllMocks();
  // Most tests inject a "done" status for fetchOnboardingProposals directly
  // and don't care about the trigger step -- default it to a no-op success
  // so a test that doesn't explicitly mock it doesn't crash on an
  // unconfigured call returning undefined.
  vi.mocked(client.triggerOnboardingProposals).mockResolvedValue({ proposals_status: "in_progress" });
  // Onboarding persists the open reorganize session id across page
  // switches -- without clearing it, a session id written by one test
  // leaks into the next and gets restored on mount, unexpectedly.
  localStorage.clear();
});

describe("Onboarding", () => {
  it("renders existing YouTube playlists, already-added playlists, and suggested proposals once loaded", async () => {
    vi.mocked(client.fetchOnboardingPlaylists).mockResolvedValue({
      existing_playlists: [{ playlist_id: "yt-1", title: "Road Trip" }],
      added_playlists: [{ id: 1, name: "Workout", description: null, rule: null, source: null }],
    });
    vi.mocked(client.fetchOnboardingProposals).mockResolvedValue({

      proposals_status: "done",

      proposals_processed_count: 0,

      proposals_total_count: 0,
      proposals: [{ name: "Chill Vibes", theme: "lofi", song_count: 12, confidence: 0.8 }],
    })

    render(<Onboarding />);

    await screen.findByText("Road Trip");
    expect(screen.getByText("Workout")).toBeInTheDocument();
    expect(screen.getByText("Chill Vibes")).toBeInTheDocument();
  });

  it("keeps an already-accepted proposal checked in the Suggested section instead of merging into Your playlists", async () => {
    vi.mocked(client.fetchOnboardingPlaylists).mockResolvedValue({
      existing_playlists: [],
      added_playlists: [
        { id: 5, name: "Chill Vibes", description: "lofi", rule: null, source: "proposal" },
      ],
    });
    vi.mocked(client.fetchOnboardingProposals).mockResolvedValue({

      proposals_status: "done",

      proposals_processed_count: 0,

      proposals_total_count: 0,
      proposals: [],
    })

    render(<Onboarding />);
    await screen.findByText("Chill Vibes");

    const checkbox = screen.getByRole("checkbox");
    expect(checkbox).toBeChecked();
    const section = checkbox.closest("section");
    expect(section).toHaveTextContent("Suggested new playlists");
    expect(section).not.toHaveTextContent("Your YouTube Music playlists");
  });

  it("keeps an already-added custom playlist checked in the Add your own section instead of merging into Your playlists", async () => {
    vi.mocked(client.fetchOnboardingPlaylists).mockResolvedValue({
      existing_playlists: [],
      added_playlists: [
        { id: 6, name: "Road Trip Mix", description: "upbeat", rule: null, source: "custom" },
      ],
    });
    vi.mocked(client.fetchOnboardingProposals).mockResolvedValue({

      proposals_status: "done",

      proposals_processed_count: 0,

      proposals_total_count: 0,
      proposals: [],
    })

    render(<Onboarding />);
    await screen.findByText("Road Trip Mix");

    const checkbox = screen.getByRole("checkbox");
    expect(checkbox).toBeChecked();
    const section = checkbox.closest("section");
    expect(section).toHaveTextContent("Add your own playlist");
    expect(section).not.toHaveTextContent("Your YouTube Music playlists");
  });

  it("does not show a fresh AI proposal a second time, unchecked, once it's already an added playlist", async () => {
    vi.mocked(client.fetchOnboardingPlaylists).mockResolvedValue({
      existing_playlists: [],
      added_playlists: [
        { id: 5, name: "Chill Vibes", description: "lofi", rule: null, source: "proposal" },
      ],
    });
    vi.mocked(client.fetchOnboardingProposals).mockResolvedValue({

      proposals_status: "done",

      proposals_processed_count: 0,

      proposals_total_count: 0,
      proposals: [{ name: "Chill Vibes", theme: "lofi", song_count: 12, confidence: 0.8 }],
    })

    render(<Onboarding />);
    await screen.findByText("Chill Vibes");

    // Exactly one entry (the already-added, checked one) -- not a second,
    // unchecked, freshly-clustered duplicate.
    expect(screen.getAllByText("Chill Vibes", { exact: false })).toHaveLength(1);
    expect(screen.getByRole("checkbox")).toBeChecked();
  });

  it("keeps a playlist with unknown/legacy origin (no source) in Your YouTube Music playlists", async () => {
    vi.mocked(client.fetchOnboardingPlaylists).mockResolvedValue({
      existing_playlists: [],
      added_playlists: [{ id: 8, name: "Legacy Mix", description: null, rule: null, source: null }],
    });
    vi.mocked(client.fetchOnboardingProposals).mockResolvedValue({

      proposals_status: "done",

      proposals_processed_count: 0,

      proposals_total_count: 0,
      proposals: [],
    })

    render(<Onboarding />);
    await screen.findByText("Legacy Mix");

    const checkbox = screen.getByRole("checkbox");
    expect(checkbox).toBeChecked();
    const section = checkbox.closest("section");
    expect(section).toHaveTextContent("Your YouTube Music playlists");
  });

  it("sorts suggested playlists by song count, most first", async () => {
    vi.mocked(client.fetchOnboardingPlaylists).mockResolvedValue({
      existing_playlists: [],
      added_playlists: [],
    });
    vi.mocked(client.fetchOnboardingProposals).mockResolvedValue({

      proposals_status: "done",

      proposals_processed_count: 0,

      proposals_total_count: 0,
      proposals: [
        { name: "Small", theme: "a", song_count: 5, confidence: 0.5 },
        { name: "Biggest", theme: "b", song_count: 40, confidence: 0.5 },
        { name: "Medium", theme: "c", song_count: 15, confidence: 0.5 },
      ],
    })

    render(<Onboarding />);
    await screen.findByText("Biggest", { exact: false });

    const names = screen.getAllByRole("checkbox").map((checkbox) => checkbox.closest("label")?.textContent);
    expect(names).toEqual([
      expect.stringContaining("Biggest"),
      expect.stringContaining("Medium"),
      expect.stringContaining("Small"),
    ]);
  });

  it("triggers proposal generation on a first-ever visit (idle) and stops once done", async () => {
    vi.mocked(client.fetchOnboardingPlaylists).mockResolvedValue({
      existing_playlists: [],
      added_playlists: [],
    });
    vi.mocked(client.fetchOnboardingProposals)
      .mockResolvedValueOnce({
        proposals_status: "idle",
        proposals_processed_count: 0,
        proposals_total_count: 0,
        proposals: [],
      })
      .mockResolvedValue({
        proposals_status: "done",
        proposals_processed_count: 4,
        proposals_total_count: 4,
        proposals: [{ name: "Chill Vibes", theme: "lofi", song_count: 4, confidence: 1 }],
      });

    render(<Onboarding />);

    expect(await screen.findByText("Chill Vibes")).toBeInTheDocument();
    expect(client.triggerOnboardingProposals).toHaveBeenCalledTimes(1);
  });

  it("resumes an already in-progress run without re-triggering it", async () => {
    vi.mocked(client.fetchOnboardingPlaylists).mockResolvedValue({
      existing_playlists: [],
      added_playlists: [],
    });
    vi.mocked(client.fetchOnboardingProposals).mockResolvedValue({
      proposals_status: "in_progress",
      proposals_processed_count: 25,
      proposals_total_count: 100,
      proposals: [],
    });

    render(<Onboarding />);

    const progressBar = await screen.findByRole("progressbar", {
      name: "Suggested playlist generation progress",
    });
    expect(progressBar).toHaveAttribute("value", "25");
    expect(progressBar).toHaveAttribute("max", "100");
    expect(screen.getByText("25 / 100 songs (25%)")).toBeInTheDocument();
    expect(client.triggerOnboardingProposals).not.toHaveBeenCalled();
  });

  it("shows a loading state before playlists resolve", () => {
    vi.mocked(client.fetchOnboardingPlaylists).mockReturnValue(new Promise(() => {}));
    vi.mocked(client.fetchOnboardingProposals).mockReturnValue(new Promise(() => {}));

    render(<Onboarding />);

    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("shows playlists immediately while suggestions are still loading", async () => {
    vi.mocked(client.fetchOnboardingPlaylists).mockResolvedValue({
      existing_playlists: [{ playlist_id: "yt-1", title: "Road Trip" }],
      added_playlists: [],
    });
    vi.mocked(client.fetchOnboardingProposals).mockReturnValue(new Promise(() => {}));

    render(<Onboarding />);

    await screen.findByText("Road Trip");
    expect(screen.getByText("Loading suggestions…")).toBeInTheDocument();
  });

  it("toggles a proposal's accepted state on checkbox click", async () => {
    vi.mocked(client.fetchOnboardingPlaylists).mockResolvedValue({
      existing_playlists: [],
      added_playlists: [],
    });
    vi.mocked(client.fetchOnboardingProposals).mockResolvedValue({

      proposals_status: "done",

      proposals_processed_count: 0,

      proposals_total_count: 0,
      proposals: [{ name: "Chill Vibes", theme: "lofi", song_count: 12, confidence: 0.8 }],
    })

    render(<Onboarding />);
    await screen.findByText("Chill Vibes");

    const checkbox = screen.getByRole("checkbox");
    expect(checkbox).not.toBeChecked();

    const user = userEvent.setup();
    await user.click(checkbox);

    expect(checkbox).toBeChecked();
  });

  it("toggles an existing YouTube playlist's adopted state and submits it on finish", async () => {
    vi.mocked(client.fetchOnboardingPlaylists).mockResolvedValue({
      existing_playlists: [{ playlist_id: "yt-1", title: "Road Trip" }],
      added_playlists: [],
    });
    vi.mocked(client.fetchOnboardingProposals).mockResolvedValue({

      proposals_status: "done",

      proposals_processed_count: 0,

      proposals_total_count: 0,
      proposals: [],
    })
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
    vi.mocked(client.fetchOnboardingPlaylists).mockResolvedValue({
      existing_playlists: [],
      added_playlists: [{ id: 7, name: "Workout", description: null, rule: null, source: null }],
    });
    vi.mocked(client.fetchOnboardingProposals).mockResolvedValue({

      proposals_status: "done",

      proposals_processed_count: 0,

      proposals_total_count: 0,
      proposals: [],
    })
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

  it("adds a custom playlist, checked by default, and clears the form", async () => {
    vi.mocked(client.fetchOnboardingPlaylists).mockResolvedValue({
      existing_playlists: [],
      added_playlists: [],
    });
    vi.mocked(client.fetchOnboardingProposals).mockResolvedValue({

      proposals_status: "done",

      proposals_processed_count: 0,

      proposals_total_count: 0,
      proposals: [],
    })

    render(<Onboarding />);
    await screen.findByText("No new-playlist suggestions found.");

    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Name"), "Road Trip");
    await user.type(screen.getByLabelText("Description"), "for driving");
    await user.click(screen.getByText("Add"));

    expect(screen.getByText("Road Trip")).toBeInTheDocument();
    expect(screen.getByLabelText("Name")).toHaveValue("");
    expect(screen.getByRole("checkbox", { name: /Road Trip/ })).toBeChecked();
  });

  it("excludes an unchecked custom playlist from submission", async () => {
    vi.mocked(client.fetchOnboardingPlaylists).mockResolvedValue({
      existing_playlists: [],
      added_playlists: [],
    });
    vi.mocked(client.fetchOnboardingProposals).mockResolvedValue({

      proposals_status: "done",

      proposals_processed_count: 0,

      proposals_total_count: 0,
      proposals: [],
    })
    vi.mocked(client.submitOnboardingSelection).mockResolvedValue({ created_playlists: [] });

    render(<Onboarding />);
    await screen.findByText("No new-playlist suggestions found.");

    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Name"), "Road Trip");
    await user.type(screen.getByLabelText("Description"), "for driving");
    await user.click(screen.getByText("Add"));

    const checkbox = screen.getByRole("checkbox", { name: /Road Trip/ });
    expect(checkbox).toBeChecked();
    await user.click(checkbox);
    expect(checkbox).not.toBeChecked();

    await user.click(screen.getByText("Finish setup"));

    await vi.waitFor(() => expect(client.submitOnboardingSelection).toHaveBeenCalled());
    expect(client.submitOnboardingSelection).toHaveBeenCalledWith([], [], [], [], []);
  });

  it("streams reorganize suggestions across multiple polls without re-ordering earlier ones", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      vi.mocked(client.fetchOnboardingPlaylists).mockResolvedValue({
        existing_playlists: [],
        added_playlists: [],
      });
      vi.mocked(client.fetchOnboardingProposals).mockResolvedValue({

        proposals_status: "done",

        proposals_processed_count: 0,

        proposals_total_count: 0,
        proposals: [],
      })
      vi.mocked(client.triggerReorganize).mockResolvedValue({
        session_id: 1,
        clustering_status: "in_progress",
      });
      vi.mocked(client.fetchReorganizeStatus)
        .mockResolvedValueOnce({
          session_id: 1,
          clustering_status: "in_progress",
          proposals: [{ name: "90s R&B", theme: "throwback grooves", song_count: 8 }],
          enriched_count: 50,
          total_count: 100,
          matching_status: "idle",
          matched_count: 0,
        })
        .mockResolvedValueOnce({
          session_id: 1,
          clustering_status: "done",
          proposals: [
            { name: "90s R&B", theme: "throwback grooves", song_count: 8 },
            { name: "Chill Electronic", theme: "downtempo", song_count: 5 },
          ],
          enriched_count: 100,
          total_count: 100,
          matching_status: "idle",
          matched_count: 0,
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
      vi.mocked(client.fetchOnboardingPlaylists).mockResolvedValue({
        existing_playlists: [],
        added_playlists: [],
      });
      vi.mocked(client.fetchOnboardingProposals).mockResolvedValue({

        proposals_status: "done",

        proposals_processed_count: 0,

        proposals_total_count: 0,
        proposals: [{ name: "Stale Suggestion", theme: "old", song_count: 6, confidence: 0.5 }],
      })
      vi.mocked(client.triggerReorganize).mockResolvedValue({
        session_id: 1,
        clustering_status: "in_progress",
      });
      vi.mocked(client.fetchReorganizeStatus).mockResolvedValue({
        session_id: 1,
        clustering_status: "done",
        proposals: [],
        enriched_count: 0,
        total_count: 0,
        matching_status: "idle",
        matched_count: 0,
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
    vi.mocked(client.fetchOnboardingPlaylists).mockResolvedValue({
      existing_playlists: [],
      added_playlists: [{ id: 7, name: "Workout", description: null, rule: null, source: null }],
    });
    vi.mocked(client.fetchOnboardingProposals).mockResolvedValue({

      proposals_status: "done",

      proposals_processed_count: 0,

      proposals_total_count: 0,
      proposals: [],
    })
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
    vi.mocked(client.fetchOnboardingPlaylists).mockResolvedValue({
      existing_playlists: [],
      added_playlists: [{ id: 7, name: "Workout", description: null, rule: null, source: null }],
    });
    vi.mocked(client.fetchOnboardingProposals).mockResolvedValue({

      proposals_status: "done",

      proposals_processed_count: 0,

      proposals_total_count: 0,
      proposals: [],
    })
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
      vi.mocked(client.fetchOnboardingPlaylists).mockResolvedValue({
        existing_playlists: [],
        added_playlists: [],
      });
      vi.mocked(client.fetchOnboardingProposals).mockResolvedValue({

        proposals_status: "done",

        proposals_processed_count: 0,

        proposals_total_count: 0,
        proposals: [],
      })
      vi.mocked(client.triggerReorganize).mockResolvedValue({
        session_id: 1,
        clustering_status: "in_progress",
      });
      vi.mocked(client.fetchReorganizeStatus).mockResolvedValue({
        session_id: 1,
        clustering_status: "in_progress",
        proposals: [{ name: "90s R&B", theme: "throwback grooves", song_count: 8 }],
        enriched_count: 50,
        total_count: 100,
        matching_status: "idle",
        matched_count: 0,
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
      vi.mocked(client.fetchOnboardingPlaylists).mockResolvedValue({
        existing_playlists: [],
        added_playlists: [],
      });
      vi.mocked(client.fetchOnboardingProposals).mockResolvedValue({

        proposals_status: "done",

        proposals_processed_count: 0,

        proposals_total_count: 0,
        proposals: [],
      })
      vi.mocked(client.triggerReorganize).mockResolvedValue({
        session_id: 1,
        clustering_status: "in_progress",
      });
      vi.mocked(client.fetchReorganizeStatus).mockResolvedValueOnce({
        session_id: 1,
        clustering_status: "stalled",
        proposals: [{ name: "90s R&B", theme: "throwback grooves", song_count: 8 }],
        enriched_count: 50,
        total_count: 100,
        matching_status: "idle",
        matched_count: 0,
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
        enriched_count: 100,
        total_count: 100,
        matching_status: "idle",
        matched_count: 0,
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

  it("triggers matching in the background and finishes without waiting for it to complete", async () => {
    // Matching now runs entirely server-side (U4 follow-up) -- "Finish
    // setup" only has to kick it off, not babysit it to completion the way
    // the old frontend-driven batch loop did. The Review Queue page is
    // responsible for showing its live progress.
    vi.mocked(client.fetchOnboardingPlaylists).mockResolvedValue({
      existing_playlists: [],
      added_playlists: [],
    });
    vi.mocked(client.fetchOnboardingProposals).mockResolvedValue({

      proposals_status: "done",

      proposals_processed_count: 0,

      proposals_total_count: 0,
      proposals: [],
    })
    vi.mocked(client.triggerReorganize).mockResolvedValue({
      session_id: 1,
      clustering_status: "done",
    });
    vi.mocked(client.fetchReorganizeStatus).mockResolvedValue({
      session_id: 1,
      clustering_status: "done",
      proposals: [],
      enriched_count: 0,
      total_count: 0,
      matching_status: "idle",
      matched_count: 0,
    });
    let resolveMatchTrigger: (value: client.MatchTrigger) => void = () => {};
    vi.mocked(client.triggerReorganizeMatching).mockReturnValue(
      new Promise((resolve) => {
        resolveMatchTrigger = resolve;
      }),
    );
    vi.mocked(client.submitOnboardingSelection).mockResolvedValue({ created_playlists: [] });

    render(<Onboarding />);
    await screen.findByText("No new-playlist suggestions found.");

    const user = userEvent.setup();
    await user.click(screen.getByText("Reorganize My Library"));
    await screen.findByText("No new playlist suggestions this run.");

    await user.click(screen.getByText("Finish setup"));

    await vi.waitFor(() => expect(client.triggerReorganizeMatching).toHaveBeenCalledWith(1));
    // Still waiting on the trigger call itself (not yet resolved) -- the
    // button should reflect that, not have already flipped to "done".
    expect(screen.getByText("Finishing setup…")).toBeInTheDocument();

    await act(async () => {
      resolveMatchTrigger({ session_id: 1, matching_status: "in_progress" });
      await Promise.resolve();
    });

    await screen.findByRole("status");
  });

  it("cancels an open reorganize session and reverts to the pre-reorganize state", async () => {
    vi.mocked(client.fetchOnboardingPlaylists).mockResolvedValue({
      existing_playlists: [],
      added_playlists: [],
    });
    vi.mocked(client.fetchOnboardingProposals).mockResolvedValue({

      proposals_status: "done",

      proposals_processed_count: 0,

      proposals_total_count: 0,
      proposals: [{ name: "Stale Suggestion", theme: "old", song_count: 6, confidence: 0.5 }],
    })
    vi.mocked(client.triggerReorganize).mockResolvedValue({
      session_id: 1,
      clustering_status: "done",
    });
    vi.mocked(client.fetchReorganizeStatus).mockResolvedValue({
      session_id: 1,
      clustering_status: "done",
      proposals: [],
      enriched_count: 0,
      total_count: 0,
      matching_status: "idle",
      matched_count: 0,
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
    vi.mocked(client.fetchOnboardingPlaylists).mockResolvedValue({
      existing_playlists: [],
      added_playlists: [],
    });
    vi.mocked(client.fetchOnboardingProposals).mockResolvedValue({

      proposals_status: "done",

      proposals_processed_count: 0,

      proposals_total_count: 0,
      proposals: [],
    })
    vi.mocked(client.triggerReorganize).mockResolvedValue({
      session_id: 1,
      clustering_status: "done",
    });
    vi.mocked(client.fetchReorganizeStatus).mockResolvedValue({
      session_id: 1,
      clustering_status: "done",
      proposals: [],
      enriched_count: 0,
      total_count: 0,
      matching_status: "idle",
      matched_count: 0,
    });

    render(<Onboarding />);
    await screen.findByText("No new-playlist suggestions found.");
    expect(screen.queryByText(/won't be written to YouTube until/)).not.toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByText("Reorganize My Library"));

    await screen.findByText(/won't be written to YouTube until/);
  });

  // R5/KD4 (session-settled, user-directed): auto-navigate to Organize
  // immediately after confirming selection, with no extra click and no
  // intermediate "done" pause -- Organize's own extended progress block
  // (U5) already carries the "Classifying…" state. Supersedes the prior
  // behavior (deferring onComplete by 1500ms so a local "done" message
  // could paint first).
  it("calls onComplete immediately after finishing setup, without an artificial delay (R5/KD4)", async () => {
    vi.mocked(client.fetchOnboardingPlaylists).mockResolvedValue({
      existing_playlists: [],
      added_playlists: [],
    });
    vi.mocked(client.fetchOnboardingProposals).mockResolvedValue({

      proposals_status: "done",

      proposals_processed_count: 0,

      proposals_total_count: 0,
      proposals: [],
    })
    vi.mocked(client.submitOnboardingSelection).mockResolvedValue({ created_playlists: [] });
    const onComplete = vi.fn();

    render(<Onboarding onComplete={onComplete} />);
    await screen.findByText("No new-playlist suggestions found.");

    const user = userEvent.setup();
    const start = Date.now();
    await user.click(screen.getByText("Finish setup"));

    // The old implementation only called onComplete after a 1500ms
    // setTimeout -- a generous 500ms ceiling (well under that) is enough to
    // tell the two apart without being flaky on a slow CI box.
    await vi.waitFor(() => expect(onComplete).toHaveBeenCalledTimes(1));
    expect(Date.now() - start).toBeLessThan(500);
  });

  it("ignores a second click on Finish setup while the first is still submitting", async () => {
    // Reproduces the window that caused a real production bug: the button
    // only disabled once matchingInProgress became true, well after
    // submitOnboardingSelection's own round-trip -- a fast double-click
    // could fire two overlapping handleFinish calls, which downstream fired
    // two overlapping matching-batch loops that raced to create the same
    // LibraryItem and crashed with a UNIQUE constraint violation. Delaying
    // submitOnboardingSelection here creates a real window to click twice
    // into.
    let resolveSelection: (value: { created_playlists: never[] }) => void = () => {};
    vi.mocked(client.fetchOnboardingPlaylists).mockResolvedValue({
      existing_playlists: [],
      added_playlists: [],
    });
    vi.mocked(client.fetchOnboardingProposals).mockResolvedValue({

      proposals_status: "done",

      proposals_processed_count: 0,

      proposals_total_count: 0,
      proposals: [],
    })
    vi.mocked(client.submitOnboardingSelection).mockReturnValue(
      new Promise((resolve) => {
        resolveSelection = resolve;
      }),
    );

    render(<Onboarding />);
    await screen.findByText("No new-playlist suggestions found.");

    const user = userEvent.setup();
    const button = screen.getByText("Finish setup");
    await user.click(button);
    // The button is disabled synchronously on click, before
    // submitOnboardingSelection even resolves -- a second click while it's
    // still pending must be a no-op, not a second overlapping call.
    await user.click(screen.getByText("Finishing setup…"));

    await act(async () => {
      resolveSelection({ created_playlists: [] });
      await Promise.resolve();
    });

    await screen.findByRole("status");
    expect(client.submitOnboardingSelection).toHaveBeenCalledTimes(1);
  });

  it("shows a percentage progress bar while enrichment is in progress", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      vi.mocked(client.fetchOnboardingPlaylists).mockResolvedValue({
        existing_playlists: [],
        added_playlists: [],
      });
      vi.mocked(client.fetchOnboardingProposals).mockResolvedValue({

        proposals_status: "done",

        proposals_processed_count: 0,

        proposals_total_count: 0,
        proposals: [],
      })
      vi.mocked(client.triggerReorganize).mockResolvedValue({
        session_id: 1,
        clustering_status: "in_progress",
      });
      vi.mocked(client.fetchReorganizeStatus).mockResolvedValue({
        session_id: 1,
        clustering_status: "in_progress",
        proposals: [],
        enriched_count: 340,
        total_count: 2794,
        matching_status: "idle",
        matched_count: 0,
      });

      render(<Onboarding />);
      await screen.findByText("No new-playlist suggestions found.");

      const user = userEvent.setup({ delay: null });
      await user.click(screen.getByText("Reorganize My Library"));

      await act(async () => {
        await vi.advanceTimersByTimeAsync(2000);
      });

      const progressBar = screen.getByRole("progressbar", { name: "Library processing progress" });
      expect(progressBar).toHaveAttribute("value", "340");
      expect(progressBar).toHaveAttribute("max", "2794");
      expect(screen.getByText("340 / 2794 songs (12%)")).toBeInTheDocument();
      expect(screen.getByText("Processing your liked songs…")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("resumes an open reorganize session after unmounting and remounting the page", async () => {
    // Simulates switching to another tab (App.tsx unmounts this whole page)
    // and back -- the server-side session kept running the whole time; only
    // this component's local state was lost, and localStorage is how it
    // finds its way back to polling the same session instead of looking
    // like nothing was ever started.
    vi.mocked(client.fetchOnboardingPlaylists).mockResolvedValue({
      existing_playlists: [],
      added_playlists: [],
    });
    vi.mocked(client.fetchOnboardingProposals).mockResolvedValue({

      proposals_status: "done",

      proposals_processed_count: 0,

      proposals_total_count: 0,
      proposals: [],
    })
    vi.mocked(client.triggerReorganize).mockResolvedValue({
      session_id: 7,
      clustering_status: "in_progress",
    });
    vi.mocked(client.fetchReorganizeStatus).mockResolvedValue({
      session_id: 7,
      clustering_status: "in_progress",
      proposals: [{ name: "90s R&B", theme: "throwback grooves", song_count: 8 }],
      enriched_count: 500,
      total_count: 1000,
      matching_status: "idle",
      matched_count: 0,
    });

    const first = render(<Onboarding />);
    await screen.findByText("No new-playlist suggestions found.");
    const user = userEvent.setup();
    await user.click(screen.getByText("Reorganize My Library"));
    await screen.findByText("Session open.", { exact: false });
    first.unmount();

    render(<Onboarding />);

    await screen.findByText("90s R&B", { exact: false });
    expect(screen.getByText("500 / 1000 songs (50%)")).toBeInTheDocument();
    expect(screen.getByText("Session open.", { exact: false })).toBeInTheDocument();
  });

  it("skips onboarding's own proposal generation entirely when a reorganize session is being restored", async () => {
    // A restored session's own proposals are authoritative; onboarding's
    // own initial suggestions would just be thrown away in that case (they
    // used to actually run and then get discarded, wasting a real,
    // LLM-cost background job -- and once briefly created a race where a
    // slow-to-resolve static snapshot could stomp on the session's already-
    // landed suggestions). Now the fetch/trigger is skipped altogether.
    vi.mocked(client.fetchOnboardingPlaylists).mockResolvedValue({
      existing_playlists: [],
      added_playlists: [],
    });
    vi.mocked(client.fetchReorganizeStatus).mockResolvedValue({
      session_id: 7,
      clustering_status: "in_progress",
      proposals: [{ name: "90s R&B", theme: "throwback grooves", song_count: 8 }],
      enriched_count: 500,
      total_count: 1000,
      matching_status: "idle",
      matched_count: 0,
    });
    localStorage.setItem("yt-music-organizer:reorganizeSessionId", "7");

    render(<Onboarding />);

    await screen.findByText("90s R&B", { exact: false });

    expect(client.fetchOnboardingProposals).not.toHaveBeenCalled();
    expect(client.triggerOnboardingProposals).not.toHaveBeenCalled();
  });

  it("clears a stale stored reorganize session reference and recovers onboarding's own proposals immediately (R15/KTD6)", async () => {
    // Reproduces the observed bug: a browser-local reference to a Reorganize
    // session that no longer resolves server-side (e.g. deleted, hence a
    // 404) used to suppress onboarding's own suggestions forever -- nothing
    // ever cleared the stale reference, and the "hasStoredSession" branch
    // above had already opted out of generating onboarding's own proposals.
    // The fix must both clear the dead reference AND fall back to
    // triggering onboarding's own proposals for *this* mount, not just
    // future ones.
    localStorage.setItem("yt-music-organizer:reorganizeSessionId", "99");
    vi.mocked(client.fetchOnboardingPlaylists).mockResolvedValue({
      existing_playlists: [],
      added_playlists: [],
    });
    vi.mocked(client.fetchReorganizeStatus).mockRejectedValue(new Error("404 Not Found"));
    vi.mocked(client.fetchOnboardingProposals)
      .mockResolvedValueOnce({
        proposals_status: "idle",
        proposals_processed_count: 0,
        proposals_total_count: 0,
        proposals: [],
      })
      .mockResolvedValue({
        proposals_status: "done",
        proposals_processed_count: 4,
        proposals_total_count: 4,
        proposals: [{ name: "Chill Vibes", theme: "lofi", song_count: 4, confidence: 1 }],
      });
    const clearSpy = vi.spyOn(reorganizeSession, "clearStoredReorganizeSessionId");

    render(<Onboarding />);

    // The current mount recovers on its own -- not just a future one.
    expect(await screen.findByText("Chill Vibes")).toBeInTheDocument();
    expect(clearSpy).toHaveBeenCalled();
    expect(client.triggerOnboardingProposals).toHaveBeenCalledTimes(1);
    expect(localStorage.getItem("yt-music-organizer:reorganizeSessionId")).toBeNull();

    clearSpy.mockRestore();
  });

  it("does not clear or fall back when a stored reorganize session's status poll succeeds", async () => {
    // Regression guard for the fix above: a session that still resolves
    // fine server-side must keep suppressing onboarding's own proposal
    // generation exactly as before -- only a dead reference triggers
    // cleanup and recovery.
    localStorage.setItem("yt-music-organizer:reorganizeSessionId", "7");
    vi.mocked(client.fetchOnboardingPlaylists).mockResolvedValue({
      existing_playlists: [],
      added_playlists: [],
    });
    vi.mocked(client.fetchReorganizeStatus).mockResolvedValue({
      session_id: 7,
      clustering_status: "in_progress",
      proposals: [{ name: "90s R&B", theme: "throwback grooves", song_count: 8 }],
      enriched_count: 500,
      total_count: 1000,
      matching_status: "idle",
      matched_count: 0,
    });
    const clearSpy = vi.spyOn(reorganizeSession, "clearStoredReorganizeSessionId");

    render(<Onboarding />);

    await screen.findByText("90s R&B", { exact: false });

    expect(clearSpy).not.toHaveBeenCalled();
    expect(client.fetchOnboardingProposals).not.toHaveBeenCalled();
    expect(client.triggerOnboardingProposals).not.toHaveBeenCalled();
    expect(localStorage.getItem("yt-music-organizer:reorganizeSessionId")).toBe("7");

    clearSpy.mockRestore();
  });
});
