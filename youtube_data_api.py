"""Fetch Liked Music via the official, stable YouTube Data API v3.

ytmusicapi's get_liked_songs() hits YouTube Music's unofficial internal
"LM" browse endpoint, which gets intermittently soft-blocked for cookie
sessions. Liking a song in YouTube Music is the same underlying action as
liking a video on YouTube, so we can read the same list reliably through
the public, documented API instead: channels().list() resolves the
account's "Liked videos" playlist ID, then playlistItems().list() pages
through it.

Requires YT_DATA_API_CLIENT_ID / YT_DATA_API_CLIENT_SECRET in .env, from an
OAuth client of type "Desktop app" (same Google Cloud project as the
YouTube Data API v3 that's already enabled).
"""

import os
import re
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]
TOKEN_FILE = "yt_data_token.json"
UNAVAILABLE_TITLES = {"Private video", "Deleted video"}
MUSIC_CATEGORY_ID = "10"  # YouTube's official video category taxonomy
_CHANNEL_TOPIC_SUFFIX = re.compile(r"\s*-\s*Topic$")


def _get_credentials():
    client_id = os.environ.get("YT_DATA_API_CLIENT_ID")
    client_secret = os.environ.get("YT_DATA_API_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise SystemExit("Set YT_DATA_API_CLIENT_ID / YT_DATA_API_CLIENT_SECRET in .env")

    creds = None
    if Path(TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            client_config = {
                "installed": {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            }
            flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
            creds = flow.run_local_server(port=0, open_browser=False)
        Path(TOKEN_FILE).write_text(creds.to_json())

    return creds


def _music_video_ids(youtube, video_ids):
    """Liked videos include regular (non-music) YouTube likes; keep only official Music category."""
    music_ids = set()
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i : i + 50]
        resp = youtube.videos().list(id=",".join(batch), part="snippet").execute()
        for item in resp.get("items", []):
            if item["snippet"].get("categoryId") == MUSIC_CATEGORY_ID:
                music_ids.add(item["id"])
    return music_ids


def get_liked_songs():
    """Returns [{"videoId", "title", "artists": [{"name": ...}]}] for liked videos in the Music category."""
    creds = _get_credentials()
    youtube = build("youtube", "v3", credentials=creds)

    channel = youtube.channels().list(part="contentDetails", mine=True).execute()
    liked_playlist_id = channel["items"][0]["contentDetails"]["relatedPlaylists"]["likes"]

    tracks = []
    page_token = None
    while True:
        resp = (
            youtube.playlistItems()
            .list(playlistId=liked_playlist_id, part="snippet", maxResults=50, pageToken=page_token)
            .execute()
        )
        for item in resp.get("items", []):
            snippet = item["snippet"]
            title = snippet.get("title", "")
            video_id = snippet.get("resourceId", {}).get("videoId")
            if not video_id or title in UNAVAILABLE_TITLES:
                continue
            artist = _CHANNEL_TOPIC_SUFFIX.sub("", snippet.get("videoOwnerChannelTitle") or "Unknown Artist")
            tracks.append({"videoId": video_id, "title": title, "artists": [{"name": artist}]})
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    music_ids = _music_video_ids(youtube, [t["videoId"] for t in tracks])
    return [t for t in tracks if t["videoId"] in music_ids]
