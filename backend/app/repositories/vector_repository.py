"""Qdrant-backed store for track embeddings (clustering) -- decoupled from
the primary SQLite datastore (see docker-compose.yml's separate `qdrant`
service). Point ids are deterministic (derived from user_id + video_id) so
upserts are naturally idempotent across repeated clustering runs, and a
`content_hash` payload field lets callers skip re-embedding a track whose
title/artist/genre haven't changed since it was last stored.
"""

import hashlib
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

from qdrant_client import QdrantClient, models

from app.core.config import get_settings

TRACKS_COLLECTION = "tracks"
VECTOR_SIZE = 384  # all-MiniLM-L6-v2 (app/services/embeddings.py)


def content_hash(title: str, artist: str, genre: Optional[str]) -> str:
    raw = f"{title}\x1f{artist}\x1f{genre or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _point_id(user_id: int, video_id: str) -> str:
    # Deterministic UUID5 so re-upserting the same (user, video) always
    # targets the same point instead of accumulating duplicates.
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"track:{user_id}:{video_id}"))


@dataclass
class TrackEmbedding:
    video_id: str
    vector: list[float]
    content_hash: str


class QdrantVectorRepository:
    def __init__(self, client: QdrantClient):
        self.client = client

    def ensure_collection(self) -> None:
        if not self.client.collection_exists(TRACKS_COLLECTION):
            self.client.create_collection(
                collection_name=TRACKS_COLLECTION,
                vectors_config=models.VectorParams(size=VECTOR_SIZE, distance=models.Distance.COSINE),
            )

    def get_existing(self, user_id: int, video_ids: list[str]) -> dict[str, TrackEmbedding]:
        """Fetches whatever's already stored for these tracks, keyed by
        video_id -- callers compare `content_hash` to decide which still need
        re-embedding (title/artist/genre changed) versus which can be
        reused as-is, so a repeat clustering run only pays for what's new."""
        if not video_ids:
            return {}
        points = self.client.retrieve(
            collection_name=TRACKS_COLLECTION,
            ids=[_point_id(user_id, video_id) for video_id in video_ids],
            with_vectors=True,
            with_payload=True,
        )
        return {
            point.payload["video_id"]: TrackEmbedding(
                video_id=point.payload["video_id"],
                vector=point.vector,
                content_hash=point.payload["content_hash"],
            )
            for point in points
        }

    def upsert_tracks(self, user_id: int, tracks: list[dict], vectors: list[list[float]]) -> None:
        """`tracks` are dicts with videoId/title/artist/genre (same shape as
        library_analysis.py's enriched track dicts); `vectors` is aligned
        1:1 with `tracks` by index."""
        if not tracks:
            return
        points = [
            models.PointStruct(
                id=_point_id(user_id, track["videoId"]),
                vector=vector,
                payload={
                    "user_id": user_id,
                    "video_id": track["videoId"],
                    "title": track.get("title", ""),
                    "artist": track.get("artist", ""),
                    "content_hash": content_hash(
                        track.get("title", ""), track.get("artist", ""), track.get("genre")
                    ),
                },
            )
            for track, vector in zip(tracks, vectors)
        ]
        self.client.upsert(collection_name=TRACKS_COLLECTION, points=points)


@lru_cache
def get_vector_repository() -> QdrantVectorRepository:
    # One QdrantClient (and its connection pool) for the process lifetime,
    # mirroring app.core.db.get_engine's rationale.
    return QdrantVectorRepository(QdrantClient(url=get_settings().qdrant_url))
