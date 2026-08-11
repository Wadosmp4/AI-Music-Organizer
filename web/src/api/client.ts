// Same-origin relative path by default: the browser talks to the Vite dev
// server, which proxies /api to the backend by container DNS name (see
// vite.config.ts) — the browser itself never resolves `backend` directly.
// VITE_API_BASE_URL is an override for other deployment shapes (e.g. a
// built static bundle served separately from the API).
const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "/api/v1";

export interface ReviewQueueExplanation {
  signal: string;
  detail: string;
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
  reorganize_session_id: number | null;
}

export interface StatusEntry {
  status: "ok" | "needs_reconnect" | "degraded";
  reason: string | null;
}

export interface AuthStatus {
  write_path: StatusEntry;
  detection_path: StatusEntry;
  youtube_detection: StatusEntry;
  llm_description_match: StatusEntry;
  llm_clustering: StatusEntry;
  lastfm: StatusEntry;
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
  // "proposal" | "custom" | "adopted" | null (rows created before this field
  // existed) -- which Onboarding UI section this playlist originated from,
  // so it can stay there (checked) instead of collapsing into the generic
  // "Your YouTube Music playlists" list once it's a real playlist.
  source: string | null;
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

// Split into two calls -- /playlists (a DB query + a plain YouTube list,
// fast) and /proposals (AI clustering, can take a long time for a big
// library) -- so the frontend can render playlists immediately instead of
// both being stuck behind the slow one on a single combined endpoint.
export interface OnboardingPlaylists {
  existing_playlists: YouTubePlaylist[];
  added_playlists: AddedPlaylist[];
}

// /proposals is trigger+poll, not a plain GET (mirrors matching/ingestion):
// generation runs as a background task (embed+HDBSCAN clustering + genre
// enrichment, previously up to ~2 minutes of invisible work) so the caller
// polls status/progress/results here instead of waiting on one long request.
export interface OnboardingProposalsTrigger {
  proposals_status: string;
}

export interface OnboardingProposalsStatus {
  proposals_status: string;
  proposals_processed_count: number;
  proposals_total_count: number;
  proposals: PlaylistProposal[];
}

// Carries the parsed JSON error body (when the response had one) alongside
// the HTTP status, so callers that need structured detail -- e.g. U7's
// playlist-removal confirmation (KTD7) -- don't have to re-parse a plain
// Error's message string.
export class ApiError extends Error {
  status: number;
  body: unknown;

  constructor(status: number, body: unknown, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

// Every backend error handler (main.py) sends either a plain-string
// `detail` (bare HTTPException) or a `message` field (e.g. the
// quota_exceeded handler) -- prefer that human-readable text over the raw
// "Request to X failed (503): {...}" fallback, which is what every existing
// `catch (err) { setError(err.message) }` call site across the app renders
// verbatim. A structured non-string `detail` (e.g. removal_requires_confirmation)
// falls through to the fallback unchanged -- callers that need it read
// `ApiError.body` directly, same as before.
function friendlyErrorMessage(body: unknown, fallback: string): string {
  if (typeof body !== "object" || body === null) return fallback;
  const { message, detail } = body as Record<string, unknown>;
  if (typeof message === "string") return message;
  if (typeof detail === "string") return detail;
  return fallback;
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
    const text = await response.text();
    let body: unknown = null;
    try {
      body = JSON.parse(text);
    } catch {
      // Not a JSON body -- ApiError.body stays null, message keeps the raw text.
    }
    const fallback = `Request to ${path} failed (${response.status}): ${text}`;
    throw new ApiError(response.status, body, friendlyErrorMessage(body, fallback));
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

export function fetchOnboardingPlaylists(): Promise<OnboardingPlaylists> {
  return request<OnboardingPlaylists>("/onboarding/playlists");
}

export function triggerOnboardingProposals(): Promise<OnboardingProposalsTrigger> {
  return request<OnboardingProposalsTrigger>("/onboarding/proposals", { method: "POST" });
}

export function fetchOnboardingProposals(): Promise<OnboardingProposalsStatus> {
  return request<OnboardingProposalsStatus>("/onboarding/proposals");
}

// KTD7: a playlist id in `removedPlaylistIds` with non-terminal review work
// still referencing it is rejected (409) unless it's also listed here --
// the caller resubmits with the confirmed id once the user approves removal
// anyway (see the ApiError thrown by `request` and Onboarding.tsx's retry).
export function submitOnboardingSelection(
  acceptedProposals: { name: string; theme: string }[],
  customPlaylists: { name: string; description: string }[],
  adoptedPlaylists: { playlist_id: string; name: string }[] = [],
  removedPlaylistIds: number[] = [],
  confirmedRemovedPlaylistIds: number[] = [],
): Promise<{ created_playlists: CreatedPlaylist[] }> {
  return request("/onboarding/select", {
    method: "POST",
    body: JSON.stringify({
      accepted_proposals: acceptedProposals,
      custom_playlists: customPlaylists,
      adopted_playlists: adoptedPlaylists,
      removed_playlist_ids: removedPlaylistIds,
      confirmed_removed_playlist_ids: confirmedRemovedPlaylistIds,
    }),
  });
}

// The 409 body shape `POST /onboarding/select` returns (KTD7) when a
// removed playlist still has non-terminal review work referencing it.
export interface PlaylistRemovalConfirmation {
  reason: "removal_requires_confirmation";
  playlist_id: number;
  playlist_name: string;
  pending_count: number;
}

// `ApiError.body` is the raw parsed JSON response; FastAPI's HTTPException
// wraps a dict `detail` as `{ detail: {...} }`. Returns null for any other
// error shape so callers can fall through to their generic error handling.
export function asPlaylistRemovalConfirmation(err: unknown): PlaylistRemovalConfirmation | null {
  if (!(err instanceof ApiError) || err.status !== 409) return null;
  const detail = (err.body as { detail?: unknown } | null)?.detail;
  if (
    detail &&
    typeof detail === "object" &&
    (detail as { reason?: string }).reason === "removal_requires_confirmation"
  ) {
    return detail as PlaylistRemovalConfirmation;
  }
  return null;
}

// Triggers a background ingestion-check run (mirrors the reorganize
// matching trigger's shape) -- loops the backend's own bounded batch to
// completion server-side, instead of the caller clicking "Load new songs"
// repeatedly. `mode` is "waiting_for_onboarding" | "triggered";
// `ingestion_status` is "idle" | "in_progress" | "done", polled via
// fetchIngestionStatus.
export interface IngestionCheckTrigger {
  ran: boolean;
  mode: string;
  ingestion_status: string;
}

export function checkForNewSongs(): Promise<IngestionCheckTrigger> {
  return request("/ingestion/check", { method: "POST" });
}

export interface IngestionStatus {
  ingestion_status: string;
  ingestion_processed_count: number;
  ingestion_total_count: number;
}

export function fetchIngestionStatus(): Promise<IngestionStatus> {
  return request("/ingestion/status");
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
  // Live enrichment progress: enriched_count trails total_count (the
  // session's full snapshot size, known from the first poll) as the
  // background job works through the library -- the slow, previously-
  // invisible phase before any proposal ever lands.
  enriched_count: number;
  total_count: number;
  // Live matching progress (U4 follow-up): matched_count trails total_count
  // the same way, once matching has been triggered. matching_status is one
  // of "idle" | "in_progress" | "done".
  matching_status: string;
  matched_count: number;
}

export function triggerReorganize(): Promise<ReorganizeTrigger> {
  return request<ReorganizeTrigger>("/onboarding/reorganize", { method: "POST" });
}

export function fetchReorganizeStatus(sessionId: number): Promise<ReorganizeStatus> {
  return request<ReorganizeStatus>(`/onboarding/reorganize/${sessionId}`);
}

// U4: matches the session's full snapshot against candidate playlists as a
// background task (mirrors triggerReorganize's clustering pattern) -- poll
// fetchReorganizeStatus for matching_status/matched_count instead of
// looping batch calls from the browser (the loop used to die on tab close
// or page navigation, and a slow batch could look "stuck" for minutes).
export interface MatchTrigger {
  session_id: number;
  matching_status: string;
}

export function triggerReorganizeMatching(sessionId: number): Promise<MatchTrigger> {
  return request<MatchTrigger>(`/onboarding/reorganize/${sessionId}/match`, { method: "POST" });
}

// U6: Finish & Apply (R9/R10/R11) -- creates any missing playlists and
// writes every approved_pending_apply item, best-effort, as a background
// task; the trigger response returns immediately, so the caller polls
// `fetchApplyStatus` for completion.
export interface ApplyTrigger {
  session_id: number;
  apply_status: string;
}

export interface ApplyLastResult {
  succeeded: number;
  failed: number;
  failed_item_ids: number[];
  remaining: number;
}

export interface ApplyStatus {
  session_id: number;
  apply_status: string;
  apply_last_result: ApplyLastResult | null;
}

export function triggerFinishAndApply(sessionId: number): Promise<ApplyTrigger> {
  return request<ApplyTrigger>(`/onboarding/reorganize/${sessionId}/apply`, { method: "POST" });
}

export function fetchApplyStatus(sessionId: number): Promise<ApplyStatus> {
  return request<ApplyStatus>(`/onboarding/reorganize/${sessionId}/apply-status`);
}

// Lets the user abandon an open session instead of being forced to finish
// it -- every non-terminal session-tagged item is discarded (never written
// to YouTube). Rejected (409) while this session's own apply is running.
export interface CancelReorganize {
  session_id: number;
  clustering_status: string;
}

export function cancelReorganize(sessionId: number): Promise<CancelReorganize> {
  return request<CancelReorganize>(`/onboarding/reorganize/${sessionId}/cancel`, { method: "POST" });
}
