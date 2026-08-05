"""LLM-based playlist clustering — same litellm + OpenRouter + strict JSON-schema
pattern used in ai_engineering_II/assignments/2-prompting/starter.py.

Requires: export OPENROUTER_API_KEY="sk-or-..."
"""

import json
from collections import defaultdict
from typing import Optional

import litellm
from litellm import completion
from pydantic import BaseModel, ValidationError

litellm.suppress_debug_info = True

MODEL = "openrouter/google/gemini-2.5-flash"
TEMPERATURE = 0.0
MAX_TOKENS = 8000
BATCH_SIZE = 150  # a single call over thousands of tracks overflows MAX_TOKENS mid-JSON

SYSTEM_PROMPT = """
You are organizing a YouTube Music library into playlists.

You will get a numbered list of songs (artist, title, and a rough genre tag when
known). Group them into a small number of thematically coherent playlists using
your own knowledge of these artists/songs — genre, mood, era, or language. The
genre tag is only a hint; it may be missing or wrong.

Rules:
- Only propose a playlist for a group of at least 4 clearly related songs.
- Give each playlist a short, human-friendly name (e.g. "90s R&B", "Ukrainian Rock", "Chill Electronic").
- Every song index must appear in at most one playlist.
- Leave out songs that don't fit well anywhere — do not force weak groupings.
- Only use indices that were given to you; never invent songs.
"""


class PlaylistSuggestion(BaseModel):
    name: str
    indices: list[int]


class PlaylistSuggestions(BaseModel):
    playlists: list[PlaylistSuggestion]


MATCH_SYSTEM_PROMPT = """
You are deciding whether groups of new songs belong in the user's EXISTING
YouTube Music playlists, instead of getting their own new playlist.

You'll get:
1. A list of candidate clusters — each a themed group of new songs with a
   proposed name and a few example tracks.
2. A list of target playlists — each with its real title and a sample of
   songs actually already inside it, so you can infer its actual vibe (the
   title alone can be misleading, e.g. "Vibe" or "Classic").

For each cluster, decide if it clearly, thematically fits ONE target playlist
better than deserving its own new playlist. Be conservative: only match on a
strong, confident fit — wrong genre, wrong mood, or "sort of similar" is not
enough. When uncertain, leave it unmatched.

Special case: if a target playlist is named for a specific show/artist (e.g.
"Attack on titan") and another target is a general catch-all for the same
domain (e.g. "Anime"), route content specific to that show/artist to the
specific one, and everything else in that domain to the general one.

Only use target playlist titles exactly as given. Never invent a target.
"""


class ClusterMatch(BaseModel):
    cluster_name: str
    matched_playlist: Optional[str] = None


class ClusterMatches(BaseModel):
    matches: list[ClusterMatch]


def _format_clusters(clusters):
    lines = []
    for c in clusters:
        examples = ", ".join(f"{track_artist(t)} - {t['title']}" for t in c["tracks"][:3])
        lines.append(f'- "{c["name"]}" ({len(c["tracks"])} songs), e.g. {examples}')
    return "\n".join(lines)


def _format_targets(targets):
    lines = []
    for t in targets:
        sample = ", ".join(t["sample"])
        lines.append(f'- "{t["title"]}" — sample of what\'s already inside: {sample}')
    return "\n".join(lines)


def track_artist(track):
    artists = track.get("artists") or []
    return artists[0]["name"] if artists else "Unknown Artist"


def match_clusters_to_playlists(clusters, targets, model=MODEL):
    """clusters: [{"name": str, "tracks": [...]}]. targets: [{"title": str, "sample": [str, ...]}].

    Returns {cluster_name: matched_playlist_title}, omitting clusters with no
    confident match. On any API/parsing failure, logs it and returns {} (all
    clusters stay as new-playlist candidates).
    """
    if not clusters or not targets:
        return {}

    prompt = (
        "CANDIDATE CLUSTERS:\n"
        + _format_clusters(clusters)
        + "\n\nTARGET PLAYLISTS:\n"
        + _format_targets(targets)
    )
    schema = ClusterMatches.model_json_schema()
    try:
        resp = completion(
            model=model,
            messages=[
                {"role": "system", "content": MATCH_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "cluster_matches", "schema": schema, "strict": True},
            },
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
        )
        raw = resp.choices[0].message.content or ""
        parsed = ClusterMatches.model_validate_json(raw.strip())
    except (ValidationError, json.JSONDecodeError, litellm.exceptions.APIError) as e:
        print(f"  Cluster-to-playlist matching failed ({e}); keeping all clusters as new playlists.")
        return {}

    valid_clusters = {c["name"] for c in clusters}
    valid_targets = {t["title"] for t in targets}
    return {
        m.cluster_name: m.matched_playlist
        for m in parsed.matches
        if m.matched_playlist and m.cluster_name in valid_clusters and m.matched_playlist in valid_targets
    }


def _format_tracks(tracks):
    lines = []
    for i, t in enumerate(tracks):
        genre = f" [{t['genre']}]" if t.get("genre") else ""
        lines.append(f"{i}: {t['artist']} - {t['title']}{genre}")
    return "\n".join(lines)


def _call(tracks, model):
    schema = PlaylistSuggestions.model_json_schema()
    resp = completion(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _format_tracks(tracks)},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "playlist_suggestions", "schema": schema, "strict": True},
        },
        temperature=TEMPERATURE,
        max_tokens=MAX_TOKENS,
    )
    return resp.choices[0].message.content or ""


def _suggest_batch(tracks, model):
    """A single LLM call over one batch. Returns [{"name": str, "tracks": [...]}]."""
    try:
        raw = _call(tracks, model)
        parsed = PlaylistSuggestions.model_validate_json(raw.strip())
    except (ValidationError, json.JSONDecodeError, litellm.exceptions.APIError) as e:
        print(f"  LLM clustering failed on a batch of {len(tracks)} songs ({e}); leaving them unclustered.")
        return []

    used = set()
    results = []
    for suggestion in parsed.playlists:
        indices = [i for i in suggestion.indices if 0 <= i < len(tracks) and i not in used]
        used.update(indices)
        if indices:
            results.append({"name": suggestion.name, "tracks": [tracks[i] for i in indices]})
    return results


def suggest_playlists(tracks, model=MODEL, batch_size=BATCH_SIZE):
    """tracks: list of dicts with videoId, title, artist, optional genre.

    Processes in batches (a single call over thousands of tracks overflows the
    model's output budget mid-JSON) and merges same-named playlists across
    batches. Returns [{"name": str, "tracks": [...]}], covering only songs the
    LLM grouped confidently.
    """
    if not tracks:
        return []

    merged = defaultdict(list)
    name_casing = {}
    for i in range(0, len(tracks), batch_size):
        batch = tracks[i : i + batch_size]
        for suggestion in _suggest_batch(batch, model):
            key = suggestion["name"].strip().lower()
            name_casing.setdefault(key, suggestion["name"])
            merged[key].extend(suggestion["tracks"])

    return [{"name": name_casing[key], "tracks": ts} for key, ts in merged.items()]
