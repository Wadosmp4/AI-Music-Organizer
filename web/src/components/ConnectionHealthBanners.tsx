import type { ReactNode } from "react";

import type { AuthStatus } from "../api/client";
import { ALERT_BANNER } from "../styles";

function Banner({ testId, children }: { testId: string; children: ReactNode }) {
  return (
    <div role="alert" data-testid={testId} className={ALERT_BANNER}>
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
    authStatus.detection_path.status === "needs_reconnect" ||
    authStatus.youtube_detection.status === "degraded";
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
      {/* Distinct from the two reconnect banners above: a degraded
          youtube_detection reading (e.g. a quota-exceeded 403) isn't a
          credential problem, so it gets its own banner with no reconnect
          call to action -- the reason text (backend-supplied) already says
          what's actually wrong and when it self-resolves. */}
      {authStatus.youtube_detection.status === "degraded" && (
        <Banner testId="youtube-detection-degraded-banner">
          YouTube library reads are temporarily degraded
          {authStatus.youtube_detection.reason ? `: ${authStatus.youtube_detection.reason}` : ""}.
        </Banner>
      )}
    </div>
  );
}
