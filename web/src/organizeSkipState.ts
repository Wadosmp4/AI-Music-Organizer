// Guided one-playlist-at-a-time Organize view (U5): mirrors
// reorganizeSession.ts's shape to persist this-pass's review state across a
// page switch -- App.tsx unmounts whichever page isn't active, so without
// this, switching back to Organize would forget which playlist the user had
// already skipped past (or jumped back to) and could reshuffle which one
// looks "current" purely because the queue happened to reload in a
// different order.
//
// Two independent pieces of state:
//  - the this-pass skip set: playlist ids the user explicitly deferred with
//    "Skip for now" (KD3/R7) -- never blocks progress, so a skipped
//    playlist is just excluded from being picked as "current" until either
//    it's revisited directly or every other playlist runs out.
//  - the pinned "current" playlist id: set only when the user jumps to a
//    playlist directly from the revisit list, so that choice overrides the
//    normal creation-order sequencing (KTD5) until it resolves or the user
//    skips it too.
const SKIPPED_STORAGE_KEY = "yt-music-organizer:organizeSkippedPlaylistIds";
const CURRENT_STORAGE_KEY = "yt-music-organizer:organizeCurrentPlaylistId";

export function getSkippedPlaylistIds(): Set<number> {
  const raw = localStorage.getItem(SKIPPED_STORAGE_KEY);
  if (raw === null) return new Set();
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return new Set();
    return new Set(parsed.filter((id): id is number => typeof id === "number"));
  } catch {
    return new Set();
  }
}

export function addSkippedPlaylistId(playlistId: number): void {
  const next = getSkippedPlaylistIds();
  next.add(playlistId);
  localStorage.setItem(SKIPPED_STORAGE_KEY, JSON.stringify([...next]));
}

export function removeSkippedPlaylistId(playlistId: number): void {
  const next = getSkippedPlaylistIds();
  if (!next.delete(playlistId)) return;
  localStorage.setItem(SKIPPED_STORAGE_KEY, JSON.stringify([...next]));
}

export function clearSkippedPlaylistIds(): void {
  localStorage.removeItem(SKIPPED_STORAGE_KEY);
}

export function getStoredCurrentPlaylistId(): number | null {
  const raw = localStorage.getItem(CURRENT_STORAGE_KEY);
  return raw === null ? null : Number(raw);
}

export function setStoredCurrentPlaylistId(playlistId: number): void {
  localStorage.setItem(CURRENT_STORAGE_KEY, String(playlistId));
}

export function clearStoredCurrentPlaylistId(): void {
  localStorage.removeItem(CURRENT_STORAGE_KEY);
}
