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
  it("renders the app name, all three nav links, and the page content", async () => {
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);

    render(
      <Layout page="review" onPageChange={() => {}}>
        <div>page content</div>
      </Layout>,
    );

    expect(screen.getByText("Music Organizer")).toBeInTheDocument();
    expect(screen.getByText("Review Queue")).toBeInTheDocument();
    expect(screen.getByText("Onboarding")).toBeInTheDocument();
    expect(screen.getByText("Settings")).toBeInTheDocument();
    expect(screen.getByText("page content")).toBeInTheDocument();

    await waitFor(() => expect(client.fetchAuthStatus).toHaveBeenCalled());
  });

  it("calls onPageChange with the clicked page", async () => {
    vi.mocked(client.fetchAuthStatus).mockResolvedValue(baseAuthStatus);
    const onPageChange = vi.fn();

    render(
      <Layout page="review" onPageChange={onPageChange}>
        <div>page content</div>
      </Layout>,
    );

    const user = userEvent.setup();
    await user.click(screen.getByText("Settings"));

    expect(onPageChange).toHaveBeenCalledWith("settings");
  });

  it("shows a status dot per health state, including degraded", async () => {
    vi.mocked(client.fetchAuthStatus).mockResolvedValue({
      ...baseAuthStatus,
      write_path: { status: "degraded", reason: "slow" },
      detection_path: { status: "needs_reconnect", reason: "expired" },
    });

    render(
      <Layout page="review" onPageChange={() => {}}>
        <div>page content</div>
      </Layout>,
    );

    await waitFor(() => {
      expect(screen.getByLabelText("Write path: degraded")).toBeInTheDocument();
    });
    expect(screen.getByLabelText("Detection path: needs reconnect")).toBeInTheDocument();
  });
});
