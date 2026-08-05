"""
YouTube Music playlist organizer.

Only touches songs that aren't already in one of your existing playlists —
existing playlist contents are never modified or removed from.

Setup:
    pip install -r requirements.txt

    Auth for playlists/library (ytmusicapi, cookie-based):
      ytmusicapi browser          # paste request headers from music.youtube.com, saves browser.json
    (ytmusicapi's OAuth mode is currently broken by a Google-side change —
    github.com/sigma67/ytmusicapi/issues/676 — so browser auth is used here.)

    Auth for --source liked specifically: ytmusicapi's internal Liked Songs
    endpoint gets intermittently soft-blocked, so this instead uses the
    official YouTube Data API v3 (same "liked video" as YouTube/YouTube Music
    share). Needs an OAuth client of type "Desktop app":
      1. Google Cloud Console -> same project -> enable "YouTube Data API v3"
      2. Credentials -> Create Credentials -> OAuth client ID -> type "Desktop app"
      3. Put YT_DATA_API_CLIENT_ID / YT_DATA_API_CLIENT_SECRET in .env
    First run opens a browser consent screen and caches a refresh token in
    yt_data_token.json.

    Optional, for genre-based grouping (YouTube Music itself has no per-track
    genre data, so this uses Last.fm's artist tags instead):
    1. Get a free key: https://www.last.fm/api/account/create
    2. Either `export LASTFM_API_KEY=...` or save the key to lastfm_key.txt

    Optional, for LLM-based clustering of whatever's still left after genre
    matching (litellm + OpenRouter, same pattern as ai_engineering_II):
    export OPENROUTER_API_KEY="sk-or-..."

Usage:
    python organize_music.py                  # dry run: writes plan.json, prints summary
    python organize_music.py --source liked   # use "Liked Music" instead of "Library Songs"
    python organize_music.py --no-genre       # skip Last.fm lookup
    python organize_music.py --no-llm         # skip LLM clustering, use rule-based artist/genre clusters instead
    python organize_music.py --apply          # actually apply the plan.json from the dry run

Leftover clustering order: Last.fm genre tag is attached as a hint per song,
then (if OPENROUTER_API_KEY is set) an LLM groups the leftovers into
thematically coherent playlists using its own knowledge of the artists/songs.
Without an API key, it falls back to plain genre/artist clustering.
"""

import argparse
import json
import os
import re
import time
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from ytmusicapi import OAuthCredentials, YTMusic

from lastfm import GenreLookup
from llm import match_clusters_to_playlists, suggest_playlists

load_dotenv()

BROWSER_AUTH_FILE = "browser.json"
OAUTH_FILE = "oauth.json"
PLAN_FILE = "plan.json"
MIN_CLUSTER_SIZE = 4  # minimum songs by one artist to justify creating a new playlist
MIN_MATCH_SCORE = 2  # minimum existing songs by an artist in a playlist to route new songs there
RETRY_ATTEMPTS = 6  # YouTube Music intermittently returns a signed-out page for no clear reason
RETRY_DELAY_S = 10.0
TARGET_SAMPLE_SIZE = 15  # tracks shown to the LLM per existing playlist, to infer its actual vibe

# Playlists that are algorithmic/system-generated (all show count=None via
# get_library_playlists) or otherwise not something to route new songs into.
NON_TARGET_PLAYLIST_TITLES = {
    "liked music",
    "2024 recap",
    "june-august recap '24",
    "march-may recap '24",
    "december-february recap '24",
    "my supermix",
    "archive mix",
    "episodes for later",
    "coal drops sessions: full performances | mercury",
    "1 клас",
    "смерть",
}

# j-pop and Japanese Pop are the same theme (confirmed by the user) — rather than
# picking a winner, both feed a single new merged playlist and are excluded from
# ongoing matching so nothing gets added to either old one going forward.
JPOP_MERGE_TITLES = {"j-pop", "japanese pop"}
JPOP_MERGE_NAME = "Japanese Music"


def load_client():
    if Path(OAUTH_FILE).exists():
        client_id = os.environ.get("YTM_OAUTH_CLIENT_ID")
        client_secret = os.environ.get("YTM_OAUTH_CLIENT_SECRET")
        if not client_id or not client_secret:
            raise SystemExit(
                "oauth.json found but YTM_OAUTH_CLIENT_ID / YTM_OAUTH_CLIENT_SECRET are not set (put them in .env)."
            )
        return YTMusic(OAUTH_FILE, oauth_credentials=OAuthCredentials(client_id=client_id, client_secret=client_secret))
    if Path(BROWSER_AUTH_FILE).exists():
        return YTMusic(BROWSER_AUTH_FILE)
    raise SystemExit("No auth found. Run 'ytmusicapi oauth' (recommended) or 'ytmusicapi browser'.")


def with_retries(fn, attempts=RETRY_ATTEMPTS, delay=RETRY_DELAY_S):
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if attempt < attempts:
                print(f"  Retry {attempt}/{attempts - 1} after {e.__class__.__name__}...")
                time.sleep(delay)
    raise last_exc


def track_artist(track):
    artists = track.get("artists") or []
    return artists[0]["name"] if artists else "Unknown Artist"


_ARTIST_NOISE = re.compile(r"(?:[\s\-(]*(?:vevo|official))+\)?\s*$", re.IGNORECASE)


def artist_bucket_key(track):
    """Normalized key for merging channel-name variants of the same artist
    (e.g. "Twenty One Pilots" vs "twenty one pilots" vs "TwentyOnePilotsVEVO")."""
    return _ARTIST_NOISE.sub("", track_artist(track)).strip().lower()


def fetch_source_songs(yt, source):
    if source == "liked":
        from youtube_data_api import get_liked_songs  # avoid pulling in google-api deps for --source library

        return get_liked_songs()
    return with_retries(lambda: yt.get_library_songs(limit=None))


def _bucket_by_artist(tracks):
    """Groups by normalized artist key but keeps the first-seen raw name for display."""
    buckets = defaultdict(list)
    display_names = {}
    for track in tracks:
        key = artist_bucket_key(track)
        buckets[key].append(track)
        display_names.setdefault(key, track_artist(track))
    return buckets, display_names


def cluster_rule_based(leftovers, genre_lookup):
    """Group leftover tracks by Last.fm genre tag; artist-cluster anything Last.fm can't tag."""
    genre_clusters = defaultdict(list)
    artist_buckets, artist_names = _bucket_by_artist(leftovers)

    unclustered_by_genre = []
    for key, tracks in artist_buckets.items():
        genre = genre_lookup.genre_for(artist_names[key])
        if genre:
            genre_clusters[genre].extend(tracks)
        else:
            unclustered_by_genre.append((artist_names[key], tracks))

    new_playlists = {}
    unclustered = []
    for name, tracks in {**genre_clusters, **dict(unclustered_by_genre)}.items():
        if len(tracks) >= MIN_CLUSTER_SIZE:
            new_playlists[name] = tracks
        else:
            unclustered.extend(tracks)
    return new_playlists, unclustered


def cluster_with_llm(leftovers, genre_lookup):
    """Bucket by normalized artist first (plain code — can't fragment across LLM batches);
    only the remainder (artists too small to stand alone) goes to the LLM for
    genre/mood grouping."""
    artist_buckets, artist_names = _bucket_by_artist(leftovers)

    new_playlists = {}
    remainder = []
    for key, tracks in artist_buckets.items():
        if len(tracks) >= MIN_CLUSTER_SIZE:
            new_playlists[artist_names[key]] = tracks
        else:
            remainder.extend(tracks)

    enriched = [
        {**t, "artist": track_artist(t), "genre": genre_lookup.genre_for(track_artist(t))}
        for t in remainder
    ]
    by_id = {t["videoId"]: t for t in remainder}

    suggestions = suggest_playlists(enriched)
    used_ids = set()
    for s in suggestions:
        tracks = [by_id[t["videoId"]] for t in s["tracks"]]
        if len(tracks) >= MIN_CLUSTER_SIZE:  # the LLM doesn't always honor this itself
            new_playlists[s["name"]] = tracks
            used_ids.update(t["videoId"] for t in tracks)

    unclustered = [t for t in remainder if t["videoId"] not in used_ids]
    return new_playlists, unclustered


def _sample_tracks(tracks, n=TARGET_SAMPLE_SIZE):
    """Evenly-spaced sample so the LLM sees the playlist's actual range, not just its oldest additions."""
    if len(tracks) <= n:
        return tracks
    step = len(tracks) / n
    return [tracks[int(i * step)] for i in range(n)]


def _format_sample(tracks):
    return [f"{track_artist(t)} - {t['title']}" for t in _sample_tracks(tracks)]


def route_clusters_to_existing(new_playlists, target_pids, playlist_titles, playlist_tracks):
    """Ask the LLM whether each candidate new playlist actually belongs in one of the
    real existing playlists (using a sample of their real contents, not just their
    name) or in the special j-pop/Japanese Pop merge target.

    Returns (remaining_new_playlists, existing_playlist_additions)."""
    targets = [
        {"title": playlist_titles[pid], "sample": _format_sample(playlist_tracks[pid])}
        for pid in target_pids
        if playlist_tracks[pid]
    ]

    jpop_pids = [pid for pid, title in playlist_titles.items() if title.strip().lower() in JPOP_MERGE_TITLES]
    jpop_seed, seen_ids = [], set()
    for pid in jpop_pids:
        for t in playlist_tracks[pid]:
            if t["videoId"] not in seen_ids:
                seen_ids.add(t["videoId"])
                jpop_seed.append(t)
    if jpop_seed:
        targets.append({"title": JPOP_MERGE_NAME, "sample": _format_sample(jpop_seed)})

    clusters = [{"name": name, "tracks": tracks} for name, tracks in new_playlists.items()]
    matches = match_clusters_to_playlists(clusters, targets)

    title_to_pid = {title: pid for pid, title in playlist_titles.items()}
    existing_additions = defaultdict(list)
    remaining = dict(new_playlists)
    jpop_merge_tracks = list(jpop_seed)

    for name, target_title in matches.items():
        tracks = remaining.pop(name, None)
        if tracks is None:
            continue
        if target_title == JPOP_MERGE_NAME:
            jpop_merge_tracks.extend(tracks)
        elif target_title in title_to_pid:
            existing_additions[title_to_pid[target_title]].extend(tracks)

    if jpop_seed:
        deduped, seen = [], set()
        for t in jpop_merge_tracks:
            if t["videoId"] not in seen:
                seen.add(t["videoId"])
                deduped.append(t)
        remaining[JPOP_MERGE_NAME] = deduped

    return remaining, existing_additions


def build_plan(yt, source, genre_lookup, use_llm):
    playlists = yt.get_library_playlists(limit=None)
    playlist_artist_counts = defaultdict(lambda: defaultdict(int))
    playlist_titles = {}
    playlist_track_ids = defaultdict(set)
    playlist_tracks = defaultdict(list)

    for pl in playlists:
        if source == "liked" and pl["title"].strip().lower() == "liked music":
            # This *is* the source (get_liked_songs) — including it here would make
            # every liked song count as "already placed" against itself.
            continue
        playlist_titles[pl["playlistId"]] = pl["title"]
        try:
            details = with_retries(lambda pid=pl["playlistId"]: yt.get_playlist(pid, limit=None))
        except Exception as e:
            print(f"  Skipping '{pl['title']}' — could not fetch its tracks after retries ({e.__class__.__name__}).")
            continue
        for t in details.get("tracks", []):
            if not t.get("videoId"):
                continue
            playlist_track_ids[pl["playlistId"]].add(t["videoId"])
            playlist_artist_counts[pl["playlistId"]][artist_bucket_key(t)] += 1
            playlist_tracks[pl["playlistId"]].append(t)

    already_placed = set().union(*playlist_track_ids.values()) if playlist_track_ids else set()

    # Eligible to receive new songs: excludes algorithmic/system playlists and the
    # j-pop/Japanese Pop pair (which get merged into a new playlist instead).
    target_pids = {
        pid
        for pid, title in playlist_titles.items()
        if title.strip().lower() not in NON_TARGET_PLAYLIST_TITLES and title.strip().lower() not in JPOP_MERGE_TITLES
    }

    songs = fetch_source_songs(yt, source)
    unplaced = [s for s in songs if s.get("videoId") and s["videoId"] not in already_placed]

    assignments = defaultdict(list)
    leftovers = []

    for track in unplaced:
        key = artist_bucket_key(track)
        best_playlist, best_score = None, 0
        for pid in target_pids:
            score = playlist_artist_counts[pid].get(key, 0)
            if score > best_score:
                best_playlist, best_score = pid, score
        if best_playlist and best_score >= MIN_MATCH_SCORE:
            assignments[best_playlist].append(track)
        else:
            leftovers.append(track)

    if use_llm:
        new_playlists, unclustered = cluster_with_llm(leftovers, genre_lookup)
    else:
        new_playlists, unclustered = cluster_rule_based(leftovers, genre_lookup)

    if use_llm and new_playlists:
        new_playlists, existing_additions = route_clusters_to_existing(
            new_playlists, target_pids, playlist_titles, playlist_tracks
        )
        for pid, tracks in existing_additions.items():
            assignments[pid].extend(tracks)

    return {
        "source": source,
        "existing_playlist_assignments": {
            playlist_titles[pid]: {
                "playlistId": pid,
                "add": [
                    {"videoId": t["videoId"], "title": t["title"], "artist": track_artist(t)}
                    for t in tracks
                ],
            }
            for pid, tracks in assignments.items()
        },
        "new_playlist_suggestions": {
            artist: [{"videoId": t["videoId"], "title": t["title"]} for t in tracks]
            for artist, tracks in new_playlists.items()
        },
        "unclustered": [
            {"videoId": t["videoId"], "title": t["title"], "artist": track_artist(t)}
            for t in unclustered
        ],
    }


def print_summary(plan):
    print(f"Source: {plan['source']}\n")
    print("== Assign to existing playlists ==")
    for name, data in plan["existing_playlist_assignments"].items():
        print(f"  {name}: +{len(data['add'])} songs")

    print("\n== Suggested NEW playlists ==")
    for name, tracks in plan["new_playlist_suggestions"].items():
        print(f"  '{name}' ({len(tracks)} songs)")

    print(f"\n== Left unclustered: {len(plan['unclustered'])} songs (no confident group found) ==")


def apply_plan(yt, plan):
    for name, data in plan["existing_playlist_assignments"].items():
        video_ids = [t["videoId"] for t in data["add"]]
        if video_ids:
            yt.add_playlist_items(data["playlistId"], video_ids, duplicates=False)
            print(f"Added {len(video_ids)} songs to '{name}'")

    for name, tracks in plan["new_playlist_suggestions"].items():
        video_ids = [t["videoId"] for t in tracks]
        playlist_id = yt.create_playlist(name, f"Auto-created for {name}")
        yt.add_playlist_items(playlist_id, video_ids, duplicates=False)
        print(f"Created playlist '{name}' with {len(video_ids)} songs")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=["library", "liked"], default="library")
    parser.add_argument("--apply", action="store_true", help="Apply a previously generated plan.json")
    parser.add_argument("--no-genre", action="store_true", help="Skip Last.fm lookup, cluster leftovers by artist only")
    parser.add_argument("--no-llm", action="store_true", help="Skip LLM clustering, use rule-based genre/artist clusters instead")
    args = parser.parse_args()

    yt = load_client()

    if args.apply:
        plan = json.loads(Path(PLAN_FILE).read_text())
        apply_plan(yt, plan)
        return

    genre_lookup = GenreLookup()
    if args.no_genre:
        genre_lookup.api_key = None
    elif not genre_lookup.enabled:
        print("No Last.fm API key found (LASTFM_API_KEY or lastfm_key.txt) — genre hints disabled.")

    use_llm = not args.no_llm and bool(os.environ.get("OPENROUTER_API_KEY"))
    if not use_llm and not args.no_llm:
        print("No OPENROUTER_API_KEY found — falling back to rule-based genre/artist clustering.")
    print()

    plan = build_plan(yt, args.source, genre_lookup, use_llm)
    Path(PLAN_FILE).write_text(json.dumps(plan, indent=2))
    print_summary(plan)
    print(f"\nFull plan written to {PLAN_FILE}. Review it, edit if needed, then rerun with --apply.")


if __name__ == "__main__":
    main()
