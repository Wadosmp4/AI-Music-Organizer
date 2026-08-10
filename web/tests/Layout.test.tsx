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
});

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
});
