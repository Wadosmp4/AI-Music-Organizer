// Shared between Onboarding and Review Queue: persists which reorganize
// session is open across a page switch -- App.tsx unmounts whichever page
// isn't active, so without this an in-progress session (still running fine
// server-side, whether clustering or matching) would look like it never
// started when the user switches back to check on it.
const STORAGE_KEY = "yt-music-organizer:reorganizeSessionId";

export function getStoredReorganizeSessionId(): number | null {
  const raw = localStorage.getItem(STORAGE_KEY);
  return raw === null ? null : Number(raw);
}

export function setStoredReorganizeSessionId(sessionId: number): void {
  localStorage.setItem(STORAGE_KEY, String(sessionId));
}

export function clearStoredReorganizeSessionId(): void {
  localStorage.removeItem(STORAGE_KEY);
}
