import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, fetchReviewQueue } from "../src/api/client";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("request() error message handling", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("prefers a structured `message` field over the raw response text", async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse(503, {
        reason: "quota_exceeded",
        message: "YouTube API daily quota exceeded. Try again after it resets.",
      }),
    );

    await expect(fetchReviewQueue()).rejects.toMatchObject({
      message: "YouTube API daily quota exceeded. Try again after it resets.",
    });
  });

  it("falls back to a plain-string `detail` field when no `message` is present", async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse(409, { detail: "onboarding proposals already in progress" }),
    );

    await expect(fetchReviewQueue()).rejects.toMatchObject({
      message: "onboarding proposals already in progress",
    });
  });

  it("falls back to the raw response text when the body has no message/detail string", async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse(409, {
        reason: "removal_requires_confirmation",
        playlist_id: 1,
        playlist_name: "Old Mix",
        pending_count: 3,
      }),
    );

    const error = (await fetchReviewQueue().catch((e) => e)) as ApiError;
    expect(error).toBeInstanceOf(ApiError);
    expect(error.message).toContain("Request to /review-queue failed (409)");
    // The structured body is still available for callers that need it.
    expect(error.body).toMatchObject({ reason: "removal_requires_confirmation" });
  });

  it("keeps the raw response text when the body isn't JSON at all", async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response("Internal Server Error", { status: 500 }),
    );

    const error = (await fetchReviewQueue().catch((e) => e)) as ApiError;
    expect(error.body).toBeNull();
    expect(error.message).toContain("Internal Server Error");
  });
});
