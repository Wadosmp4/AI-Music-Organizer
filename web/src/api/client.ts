// Same-origin relative path by default: the browser talks to the Vite dev
// server, which proxies /api to the backend by container DNS name (see
// vite.config.ts) — the browser itself never resolves `backend` directly.
// VITE_API_BASE_URL is an override for other deployment shapes (e.g. a
// built static bundle served separately from the API).
const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "/api/v1";

export interface ReviewQueueExplanation {
  signal: string;
  detail: string;
  bpm?: number | null;
  bpm_source?: "measured" | "estimated" | null;
  genre?: string | null;
}

export interface ReviewQueueItem {
  id: number;
  library_item_id: number;
  playlist_id: number | null;
  status: string;
  version: number;
  confidence: number | null;
  explanation: ReviewQueueExplanation | null;
}

export interface StatusEntry {
  status: "ok" | "needs_reconnect" | "degraded";
  reason: string | null;
}

export interface AuthStatus {
  write_path: StatusEntry;
  detection_path: StatusEntry;
  youtube_detection: StatusEntry;
  llm: StatusEntry;
  lastfm: StatusEntry;
  getsongbpm: StatusEntry;
}

export interface PlaylistProposal {
  name: string;
  theme: string;
  song_count: number;
  confidence: number;
}

export interface ExistingPlaylist {
  id: number;
  name: string;
  description: string | null;
  rule: Record<string, unknown> | null;
}

export interface OnboardingAnalysis {
  proposals: PlaylistProposal[];
  existing_playlists: ExistingPlaylist[];
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`Request to ${path} failed (${response.status}): ${body}`);
  }
  return response.json() as Promise<T>;
}

export function fetchReviewQueue(): Promise<ReviewQueueItem[]> {
  return request<ReviewQueueItem[]>("/review-queue");
}

export function approveItem(id: number, expectedVersion: number): Promise<ReviewQueueItem> {
  return request<ReviewQueueItem>(`/review-queue/${id}/approve`, {
    method: "POST",
    body: JSON.stringify({ expected_version: expectedVersion }),
  });
}

export function rejectItem(id: number, expectedVersion: number): Promise<ReviewQueueItem> {
  return request<ReviewQueueItem>(`/review-queue/${id}/reject`, {
    method: "POST",
    body: JSON.stringify({ expected_version: expectedVersion }),
  });
}

export function moveItem(
  id: number,
  expectedVersion: number,
  newPlaylistId: number,
): Promise<ReviewQueueItem> {
  return request<ReviewQueueItem>(`/review-queue/${id}/move`, {
    method: "POST",
    body: JSON.stringify({ expected_version: expectedVersion, new_playlist_id: newPlaylistId }),
  });
}

export function fetchAuthStatus(): Promise<AuthStatus> {
  return request<AuthStatus>("/auth-status");
}

export function createPlaylist(
  name: string,
  description: string,
): Promise<{ playlist: ExistingPlaylist; review_queue_items_created: number }> {
  return request("/playlists", {
    method: "POST",
    body: JSON.stringify({ name, description }),
  });
}

export function fetchOnboardingAnalysis(): Promise<OnboardingAnalysis> {
  return request<OnboardingAnalysis>("/onboarding/analysis");
}

export function submitOnboardingSelection(
  acceptedProposals: { name: string; theme: string }[],
  customPlaylists: { name: string; description: string }[],
): Promise<{ created_playlists: ExistingPlaylist[] }> {
  return request("/onboarding/select", {
    method: "POST",
    body: JSON.stringify({
      accepted_proposals: acceptedProposals,
      custom_playlists: customPlaylists,
    }),
  });
}

export function checkForNewSongs(): Promise<{
  ran: boolean;
  mode: string;
  new_songs_found: number;
  queue_items_created: number;
  backfill_complete: boolean;
}> {
  return request("/ingestion/check", { method: "POST" });
}
