from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from app.models.library import LibraryItem


class TrackArtist(TypedDict):
    name: str


class Track(TypedDict, total=False):
    videoId: str
    title: str
    artists: list[TrackArtist]


def track_from_library_item(item: "LibraryItem") -> Track:
    return {"videoId": item.video_id, "title": item.title, "artists": [{"name": item.artist}]}


class PlaylistSummary(TypedDict):
    playlistId: str
    title: str


class MusicServiceClient(ABC):
    """Data-source-agnostic boundary (R1, KTD25).

    Classification and review-queue logic depend on this interface only,
    never on a specific music service's SDK. `youtube_data_api_client.py`'s
    `YouTubeDataApiClient` is the sole implementation today; a second service
    means a new adapter here, not a change to any business logic that
    consumes it.
    """

    @abstractmethod
    def get_liked_songs(self) -> list[Track]: ...

    @abstractmethod
    def get_library_playlists(self) -> list[PlaylistSummary]: ...

    @abstractmethod
    def get_playlist_tracks(self, playlist_id: str) -> list[Track]: ...

    @abstractmethod
    def add_playlist_items(self, playlist_id: str, video_ids: list[str]) -> None: ...

    @abstractmethod
    def create_playlist(self, name: str, description: str) -> str: ...
