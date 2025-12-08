import logging
import re
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Protocol

logger = logging.getLogger("api")

SPECIAL_CHARS = [
    "\\",
    "_",
    "*",
    "[",
    "]",
    "(",
    ")",
    "~",
    "`",
    ">",
    "<",
    "&",
    "#",
    "+",
    "-",
    "=",
    "|",
    "{",
    "}",
    ".",
    "!",
]


def _markdown_escape(name: str) -> str:
    for char in SPECIAL_CHARS:
        name = name.replace(char, f"\\{char}")
    return name


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

    def full_name(self) -> str:
        return f"{self.name} - {', '.join(self.artists)}"

    def full_name_escaped(self) -> str:
        return _markdown_escape(self.full_name())

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
    tracks: list[Track] = field(default_factory=list)
    uri: str | None = None
    image: bytes | None = None

    def name_escaped(self) -> str:
        return _markdown_escape(self.name)

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
    def get_playlists(self, filter: PlaylistFilter | None = None) -> list[Playlist]:
        """Get playlists from the API."""
        ...

    @abstractmethod
    def search_track(self, track: Track) -> Track | None:
        """Search for a track in the API."""
        ...

    @abstractmethod
    def add_to_playlist(
        self,
        *tracks: Track,
        playlist_name: str,
        playlist_description: str | None = None,
        parent_folder_name: str | None = None,
    ) -> AddedTracksResult | None:
        """Add a track to a playlist in a specific folder.

        Returns an AddedTracksResult containing added, skipped, and not found tracks.

        """
        ...
