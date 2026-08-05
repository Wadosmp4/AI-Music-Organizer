import type { AuthStatus } from "../api/client";

// KTD17: write path (cookie auth) and detection path (OAuth) are two
// independently surfaced states, never blended into one signal — each
// needs its own reconnect action, so each gets its own banner.
export function ConnectionHealthBanners({ authStatus }: { authStatus: AuthStatus | null }) {
  if (!authStatus) return null;

  return (
    <div className="connection-health-banners">
      {authStatus.write_path.status === "needs_reconnect" && (
        <div role="alert" data-testid="write-path-banner" className="banner banner-warning">
          Playlist write access needs reconnecting
          {authStatus.write_path.reason ? `: ${authStatus.write_path.reason}` : ""}. Reconnect
          YouTube Music to resume approving/moving songs.
        </div>
      )}
      {authStatus.detection_path.status === "needs_reconnect" && (
        <div role="alert" data-testid="detection-path-banner" className="banner banner-warning">
          New-like detection needs reconnecting
          {authStatus.detection_path.reason ? `: ${authStatus.detection_path.reason}` : ""}.
          Reconnect your Google account to resume detecting new likes.
        </div>
      )}
    </div>
  );
}
