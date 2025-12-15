import logging
import re
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from tidalapi.playlist import UserPlaylist

from tidal_bot.spotify.model import SimplifiedPlaylistObject

logger = logging.getLogger("api")


@dataclass
class Album:
    name: str
    total_tracks: int
    artists: set[str] = field(default_factory=set)

    def __str__(self) -> str:
        output = (
            f"Album: {self.name} by "
            + ", ".join(self.artists)
            + f" ({self.total_tracks} track(s))"
        )

        return output


def _normalize_artist_name(name: str) -> set[str]:
    """Normalize artist name for comparison."""
    lower = (
        unicodedata.normalize("NFD", name.strip().lower())
        .encode("ascii", "ignore")
        .decode("ascii")
    )

    normalized: set[str] = set()

    for separator in ("&", ","):
        if separator in lower:
            normalized.update(lower.split(separator))
        else:
            normalized.add(lower)

    return normalized


@dataclass
class Track:
    id: str | int
    isrc: str
    name: str
    duration: timedelta
    artists: set[str] = field(default_factory=set)
    album: Album | None = None
    added_at: datetime | None = None

    def full_name(self) -> str:
        return f"{self.name} - {', '.join(self.artists)}"

    def __str__(self) -> str:
        mm, ss = divmod(self.duration.seconds, 60)

        output = "Track: " + self.full_name() + "\n"
        output += f"  ISRC: {self.isrc}, ID: {self.id}, Duration: {mm:02d}:{ss:02d}"

        if self.album:
            output += f"\n  Album: {self.album}"

        return output

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Track):
            return NotImplemented

        if self.isrc == other.isrc:
            return True

        duration_diff = abs(self.duration - other.duration)
        if duration_diff > timedelta(seconds=2):
            return False

        self_artists_lower = set()
        for a in self.artists:
            self_artists_lower.update(_normalize_artist_name(a))
        other_artists_lower = set()
        for a in other.artists:
            other_artists_lower.update(_normalize_artist_name(a))

        if self_artists_lower.intersection(other_artists_lower) == set():
            return False

        if self.album is None or other.album is None:
            return True

        return (
            re.match(re.escape(self.album.name), other.album.name, re.IGNORECASE)
            is not None
        ) or re.match(
            re.escape(other.album.name), self.album.name, re.IGNORECASE
        ) is not None


@dataclass
class Playlist:
    name: str
    api_playlist: UserPlaylist | SimplifiedPlaylistObject
    tracks: list[Track] = field(default_factory=list)
    uri: str | None = None
    image: bytes | None = None

    def __str__(self) -> str:
        output = f"Playlist: {self.name} - {len(self.tracks)} track(s)"

        if self.uri:
            output += f"\n  URI: {self.uri}"

        if not self.tracks:
            return output

        output += "\n"
        output += "\n".join(f"{track}" for track in self.tracks)

        return output


@dataclass
class AddedTracksResult:
    added: list[Track] = field(default_factory=list)
    skipped: list[Track] = field(default_factory=list)
    not_found: list[Track] = field(default_factory=list)
    add_error: list[Track] = field(default_factory=list)


class PlaylistFilter(Protocol):
    def __call__(self, name: str) -> bool:
        """A callable type for filtering playlists by name."""
        ...


class Api(ABC):
    @abstractmethod
    async def connect(self) -> None:
        """Connect to the API."""
        ...

    @abstractmethod
    def get_playlists(self, filter: PlaylistFilter | None = None) -> list[Playlist]:
        """Get playlists from the API."""
        ...

    @abstractmethod
    def search_track(self, track: Track) -> Track | None:
        """Search for a track in the API."""
        ...

    @abstractmethod
    def merge_playlists(
        self,
        from_playlist: Playlist,
        dest_playlist: Playlist,
    ) -> AddedTracksResult | None:
        """Merge a playlist into the API, creating it if it does not exist.

        Parameters:
            from_playlist (Playlist): The source playlist to merge from.
            dest_playlist (Playlist): The destination playlist to merge into.

        Returns an AddedTracksResult containing added, skipped, and not found tracks.

        """
        ...

    @abstractmethod
    def create_playlist(
        self,
        playlist_name: str,
        playlist_description: str | None = None,
        parent_folder_name: str | None = None,
        public: bool = False,
    ) -> Playlist | None:
        """Create a new playlist in the API.

        Parameters:
            playlist_name (str): The name of the playlist to create.
            playlist_description (str | None): The description of the playlist.
            parent_folder_name (str | None): The parent folder name for the playlist.
            public (bool): Whether the playlist is public or private.

        Returns the created Playlist, or None if creation failed.

        """
        ...
