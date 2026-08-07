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
  title: string;
  artist: string;
  playlist_name: string | null;
}

export interface StatusEntry {
  status: "ok" | "needs_reconnect" | "degraded";
  reason: string | null;
}

export interface AuthStatus {
  write_path: StatusEntry;
  detection_path: StatusEntry;
  youtube_detection: StatusEntry;
  llm_bpm_estimate: StatusEntry;
  llm_description_match: StatusEntry;
  llm_clustering: StatusEntry;
  lastfm: StatusEntry;
  getsongbpm: StatusEntry;
}

export interface PlaylistProposal {
  name: string;
  theme: string;
  song_count: number;
  confidence: number;
}

// A playlist this app already created (a prior onboarding run or the
// create-by-description feature) — tracked locally with the id/description/
// rule this app itself assigned.
export interface AddedPlaylist {
  id: number;
  name: string;
  description: string | null;
  rule: Record<string, unknown> | null;
}

// A real YouTube Music playlist the user already had before using this tool
// -- this app has never touched it, so all we know is what YouTube itself
// reports (no local id/description/rule).
export interface YouTubePlaylist {
  playlist_id: string;
  title: string;
}

// Matches the backend's CreatedPlaylistResponse (app/api/v1/onboarding.py) —
// distinct from AddedPlaylist because a freshly created playlist never
// carries a `rule` (only playlists with a structured hard-gate rule do, and
// onboarding selection never sets one).
export interface CreatedPlaylist {
  id: number;
  name: string;
  description: string | null;
}

export interface OnboardingAnalysis {
  proposals: PlaylistProposal[];
  existing_playlists: YouTubePlaylist[];
  added_playlists: AddedPlaylist[];
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    // X-Requested-With: the backend's CSRF guard (app/main.py) requires this
    // on every mutating request — a genuine cross-origin caller can't add a
    // custom header without triggering a CORS preflight, which no CORS
    // policy exists to satisfy.
    headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" },
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

// Never an eager write -- queues the song as a *pending* candidate in
// `playlistId`'s group, awaiting that group's own Approve-all, exactly like
// any algorithmic suggestion. An unassigned item's own row is reassigned in
// place; an already-assigned item gets an independent second pending row
// (a different id in the response) so both placements are approved
// separately. See ReviewQueueService.add_to_playlist for the full contract.
export function addToPlaylist(
  id: number,
  expectedVersion: number,
  playlistId: number,
): Promise<ReviewQueueItem> {
  return request<ReviewQueueItem>(`/review-queue/${id}/add-to-playlist`, {
    method: "POST",
    body: JSON.stringify({ expected_version: expectedVersion, playlist_id: playlistId }),
  });
}

export function fetchAuthStatus(): Promise<AuthStatus> {
  return request<AuthStatus>("/auth-status");
}

export function fetchPlaylists(): Promise<AddedPlaylist[]> {
  return request<AddedPlaylist[]>("/playlists");
}

export function createPlaylist(
  name: string,
  description: string,
): Promise<{ playlist: AddedPlaylist; review_queue_items_created: number }> {
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
  adoptedPlaylists: { playlist_id: string; name: string }[] = [],
  removedPlaylistIds: number[] = [],
): Promise<{ created_playlists: CreatedPlaylist[] }> {
  return request("/onboarding/select", {
    method: "POST",
    body: JSON.stringify({
      accepted_proposals: acceptedProposals,
      custom_playlists: customPlaylists,
      adopted_playlists: adoptedPlaylists,
      removed_playlist_ids: removedPlaylistIds,
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

export function resetBacklog(): Promise<{ library_items_cleared: number }> {
  return request("/ingestion/reset", { method: "POST" });
}

// U2: "Reorganize My Library" -- fetches the complete liked-songs library and
// streams AI-clustered new-playlist suggestions in as background batches
// complete (R1-R3). `clustering_status` is one of "pending" | "in_progress"
// | "done" | "stalled" ("stalled" means no new suggestion has been persisted
// in a while -- offer a re-trigger rather than polling forever).
export interface ReorganizeTrigger {
  session_id: number;
  clustering_status: string;
}

export interface ReorganizeProposal {
  name: string;
  theme: string;
  song_count: number;
}

export interface ReorganizeStatus {
  session_id: number;
  clustering_status: string;
  proposals: ReorganizeProposal[];
}

export function triggerReorganize(): Promise<ReorganizeTrigger> {
  return request<ReorganizeTrigger>("/onboarding/reorganize", { method: "POST" });
}

export function fetchReorganizeStatus(sessionId: number): Promise<ReorganizeStatus> {
  return request<ReorganizeStatus>(`/onboarding/reorganize/${sessionId}`);
}
