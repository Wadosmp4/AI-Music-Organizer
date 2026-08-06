import type { ReactNode } from "react";

import type { AuthStatus } from "../api/client";

function Banner({ testId, children }: { testId: string; children: ReactNode }) {
  return (
    <div
      role="alert"
      data-testid={testId}
      className="rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800"
    >
      {children}
    </div>
  );
}

// KTD17: write path (cookie auth) and detection path (OAuth) are two
// independently surfaced states, never blended into one signal — each
// needs its own reconnect action, so each gets its own banner.
export function ConnectionHealthBanners({ authStatus }: { authStatus: AuthStatus | null }) {
  if (!authStatus) return null;

  const hasBanner =
    authStatus.write_path.status === "needs_reconnect" ||
    authStatus.detection_path.status === "needs_reconnect";
  if (!hasBanner) return null;

  return (
    <div className="mb-4 flex flex-col gap-2">
      {authStatus.write_path.status === "needs_reconnect" && (
        <Banner testId="write-path-banner">
          Playlist write access needs reconnecting
          {authStatus.write_path.reason ? `: ${authStatus.write_path.reason}` : ""}. Reconnect
          YouTube Music to resume approving/moving songs.
        </Banner>
      )}
      {authStatus.detection_path.status === "needs_reconnect" && (
        <Banner testId="detection-path-banner">
          New-like detection needs reconnecting
          {authStatus.detection_path.reason ? `: ${authStatus.detection_path.reason}` : ""}.
          Reconnect your Google account to resume detecting new likes.
        </Banner>
      )}
    </div>
  );
}
