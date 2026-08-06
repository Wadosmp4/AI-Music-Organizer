import { render, screen } from "@testing-library/react";
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

    expect(client.submitOnboardingSelection).toHaveBeenCalledWith([], [], [], [7]);
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
