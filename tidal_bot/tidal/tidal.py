import asyncio
import logging
import time
from collections.abc import Iterator, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol, TypeVar

import yaml
from requests.exceptions import RequestException
from tidalapi.album import Album as TidalAlbum
from tidalapi.exceptions import TidalAPIError
from tidalapi.media import Track as TidalTrack
from tidalapi.playlist import Folder as TidalFolder
from tidalapi.playlist import UserPlaylist
from tidalapi.session import Session
from tidalapi.user import LoggedInUser

from tidal_bot.api import AddedTracksResult, Album, Api, Playlist, PlaylistFilter, Track

logger = logging.getLogger("tidal")

T = TypeVar("T")


class FetchWithLimitFunc[T](Protocol):
    def __call__(self, *args: Any, **kwargs: Any) -> Sequence[T]: ...


class RetryFunc[T](Protocol):
    def __call__(self, *args: Any, **kwargs: Any) -> T: ...


def _retry_on_error[T](func: RetryFunc[T], *args: Any, **kwargs: Any) -> T | None:
    backoff_intervals = [1, 2, 5, 10, 30, 60]

    for i, backoff in enumerate(backoff_intervals):
        try:
            return func(*args, **kwargs)
        except (TidalAPIError, RequestException) as e:
            logger.warning("Error during API call: %s", e)
            if i == len(backoff_intervals) - 1:
                logger.error("Max retries reached, giving up")
                return None

            logger.info("Retrying in %d seconds...", backoff)
            time.sleep(backoff)

    raise RuntimeError("Unreachable code in _retry_on_error")  # pragma: no cover


def _fetch_with_limit[T](
    func: FetchWithLimitFunc[T], *args: Any, message: str | None = None, **kwargs: Any
) -> Iterator[T]:
    offset = 0
    limit = 50

    while True:
        if message is not None:
            logger.debug(
                "%s at offset %d",
                message,
                offset,
            )

        items = _retry_on_error(func, *args, offset=offset, limit=limit, **kwargs)
        if items is None:
            break

        yield from items

        if len(items) < limit:
            break

        offset += limit

    return


def _parse_album(tidal_album: TidalAlbum) -> Album | None:
    if tidal_album.name is None:
        logger.debug("Tidal album name is None")
        return None

    artists = (
        {tidal_album.artist.name}
        if tidal_album.artist is not None and tidal_album.artist.name is not None
        else set()
    )

    if tidal_album.artists is None:
        logger.debug("Tidal album artists is None")
    else:
        for artist in tidal_album.artists:
            if artist.name is not None and artist.name not in artists:
                artists.add(artist.name)

    return Album(
        name=tidal_album.name,
        total_tracks=tidal_album.num_tracks
        if tidal_album.num_tracks is not None
        else 0,
        artists=artists,
    )


def _parse_track(tidal_track: TidalTrack) -> Track | None:
    if tidal_track.name is None:
        logger.debug("Tidal track name is None")
        return None

    if tidal_track.isrc is None:
        logger.debug("Tidal track ISRC is None")
        return None

    if tidal_track.id is None:
        logger.debug("Tidal track ID is None")
        return None

    if tidal_track.duration is None:
        logger.debug("Tidal track duration is None")
        return None

    if tidal_track.artists is None:
        logger.debug("Tidal track artists is None")
        artists = set()
    else:
        artists = {
            artist.name for artist in tidal_track.artists if artist.name is not None
        }

    album = _parse_album(tidal_track.album) if tidal_track.album else None

    return Track(
        name=tidal_track.name,
        artists=artists,
        isrc=tidal_track.isrc,
        id=tidal_track.id,
        duration=timedelta(seconds=tidal_track.duration),
        album=album,
    )


class MyTidal(Api):
    def __init__(self) -> None:
        logger.info("Initial Tidal API")

        self._session = Session()

        self._session_path = (
            Path(__file__).parent.parent.parent / ".session/tidal/session.yaml"
        )
        self._session_path.parent.mkdir(parents=True, exist_ok=True)
        self._authenticated = False

        if self._session_path.exists():
            logger.debug("Load cached Tidal session %s", self._session_path)
            with self._session_path.open() as f:
                session_yaml = yaml.safe_load(f)

            try:
                token_type = session_yaml["token_type"]
                access_token = session_yaml["access_token"]
                refresh_token = session_yaml["refresh_token"]

                if self._session.load_oauth_session(
                    token_type=token_type,
                    access_token=access_token,
                    refresh_token=refresh_token,
                ):
                    logger.info(
                        "Successfully authenticated to Tidal using cached session"
                    )
                    self._authenticated = True
                    return

                logger.warning(
                    "Failed to authenticate to Tidal using cached session, "
                    "creating a new one"
                )
                self._session_path.unlink()
            except (TypeError, KeyError) as e:
                logger.warning(
                    "Error loading cached Tidal session: %s, creating a new one", e
                )
                self._session_path.unlink()

    async def connect(self) -> None:
        if self._authenticated:
            return

        logger.info("Authenticating to Tidal")

        login, future = self._session.login_oauth()

        url = login.verification_uri_complete
        if not url.startswith("https://"):
            url = "https://" + url

        logger.info("URL %s", url)
        try:
            result = await asyncio.wait_for(asyncio.wrap_future(future), timeout=30.0)
            if not result:
                logger.error("Failed to authenticate to Tidal")
                raise OSError("Tidal authentication failed")

            logger.info("Successfully authenticated to Tidal")
        except Exception as e:
            # ensure the future terminates
            login.interval = login.expires_in
            _ = await asyncio.gather(
                asyncio.wrap_future(future), return_exceptions=True
            )
            logger.error("Failed to authenticate to Tidal: %s", e)
            raise

        logger.debug("Saving Tidal session to %s", self._session_path)
        with self._session_path.open("w") as f:
            yaml.dump(
                {
                    "session_id": self._session.session_id,
                    "token_type": self._session.token_type,
                    "access_token": self._session.access_token,
                    "refresh_token": self._session.refresh_token,
                },
                f,
            )

    def merge_playlist(
        self,
        playlist: Playlist,
        playlist_description: str | None = None,
        parent_folder_name: str | None = None,
    ) -> AddedTracksResult | None:
        logger.info("--------------------------------")
        logger.info("Adding tracks to Tidal playlist: %s", playlist.name)
        logger.info("--------------------------------")

        user = self._session.user

        if not isinstance(user, LoggedInUser):
            logger.error("User is not logged in")
            return None

        result = AddedTracksResult()

        if parent_folder_name is not None:
            folders = _retry_on_error(user.favorites.playlist_folders)
            if folders is None:
                logger.error(
                    "Error fetching playlist folders for folder %s",
                    parent_folder_name,
                )
                return None

            tidal_folder: TidalFolder | None = None
            try:
                tidal_folder = next(
                    f
                    for f in folders
                    if f.name is not None and f.name == parent_folder_name
                )
            except StopIteration:
                logger.info("Folder %s not found, creating it", parent_folder_name)

            if tidal_folder is None:
                tidal_folder = _retry_on_error(user.create_folder, parent_folder_name)
                if tidal_folder is None:
                    logger.error(
                        "Error creating playlist folder %s",
                        parent_folder_name,
                    )
                    return None

            parent_folder_id = (
                tidal_folder.id if tidal_folder.id is not None else "root"
            )
        else:
            tidal_folder = _retry_on_error(self._session.folder)
            if tidal_folder is None:
                logger.error("Error fetching root playlist folder")
                return None

            parent_folder_id = "root"

        logger.info("Fetching playlists from folder: %s", tidal_folder.name)
        tidal_playlist: UserPlaylist | None = None
        try:
            tidal_playlist = next(
                p
                for p in _fetch_with_limit(
                    tidal_folder.items,
                    message=f"Fetching playlists from folder {tidal_folder.name}",
                )
                if isinstance(p, UserPlaylist) and p.name == playlist.name
            )
        except StopIteration:
            logger.info("Playlist %s not found, creating it", playlist.name)

        existing_tracks: list[Track] = []

        if tidal_playlist is None:
            tidal_playlist = _retry_on_error(
                user.create_playlist,
                playlist.name,
                f'Playlist "{playlist.name}"',
                parent_id=parent_folder_id,
            )
            if tidal_playlist is None:
                logger.error("Error creating playlist %s", playlist.name)
                return None
        else:
            logger.info("Fetching tracks from playlist: %s", playlist.name)
            for tidal_track in _fetch_with_limit(
                tidal_playlist.tracks,
                message=f"Fetching tracks from playlist {playlist.name}",
            ):
                track = _parse_track(tidal_track)
                if track is not None:
                    existing_tracks.append(track)

        if (
            playlist_description is not None
            and playlist_description != tidal_playlist.description
        ):
            logger.info("Updating playlist description for %s", playlist.name)
            res = _retry_on_error(tidal_playlist.edit, description=playlist_description)
            if res is None or not res:
                logger.error(
                    "Error updating playlist description for %s", playlist.name
                )
                return None

        if not tidal_playlist.public:
            logger.info("Setting playlist %s to public", playlist.name)
            res = _retry_on_error(tidal_playlist.set_playlist_public)
            if res is None or not res:
                logger.error("Error setting playlist %s to public", playlist.name)
                return None

        for i, track in enumerate(playlist.tracks):
            logger.debug(
                "⚙️  Processing track %d/%d: %s",
                i + 1,
                len(playlist.tracks),
                track.full_name(),
            )

            try:
                existing_track = next(t for t in existing_tracks if t == track)
                logger.info(
                    "⏭️  Track %s already exists in playlist %s, skipping",
                    existing_track.full_name(),
                    playlist.name,
                )
                result.skipped.append(existing_track)
                continue
            except StopIteration:
                pass

            found_tidal_track = self.search_track(track)
            if found_tidal_track is None:
                logger.warning(
                    "❓ Cannot add track %s to playlist %s: track not found",
                    track.full_name(),
                    playlist.name,
                )
                result.not_found.append(track)
                continue

            logger.info(
                "✅ Adding track %s to playlist %s",
                found_tidal_track.full_name(),
                playlist.name,
            )

            added = _retry_on_error(tidal_playlist.add_by_isrc, found_tidal_track.isrc)
            if added is None or not added:
                logger.error(
                    "❌ Error adding track %s to playlist %s",
                    found_tidal_track.full_name(),
                    playlist.name,
                )
                result.add_error.append(found_tidal_track)
            else:
                result.added.append(found_tidal_track)

        return result

    def search_track(self, track: Track) -> Track | None:
        logger.info("Searching for track: %s", track.full_name())

        query = f"{track.name} " + " ".join(track.artists)
        logger.debug("Search query: %s", query)

        result = _retry_on_error(self._session.search, query, models=[TidalTrack])
        if result is None:
            logger.error("Error searching for track %s", track.full_name())
            return None

        for t in result["tracks"]:
            found_track = _parse_track(t)
            if found_track is None:
                continue

            if found_track == track:
                logger.info("Found track: %s", found_track)
                return found_track

        logger.warning(
            "No matching track found for: %s - %s", track.name, ", ".join(track.artists)
        )

        return None

    def get_playlists(self, filter: PlaylistFilter | None = None) -> list[Playlist]:
        logger.info("Fetching Tidal playlists")

        user = self._session.user

        if not isinstance(user, LoggedInUser):
            logger.error("User is not logged in")
            return []

        playlists: list[Playlist] = []

        tidal_playlists = _retry_on_error(user.playlists)
        if tidal_playlists is None:
            logger.error("Error fetching user playlists")
            return []

        for tidal_playlist in tidal_playlists:
            if not isinstance(tidal_playlist, UserPlaylist):
                continue

            if tidal_playlist.name is None:
                continue

            if filter is not None and not filter(tidal_playlist.name):
                continue

            logger.info("Fetching tracks from playlist: %s", tidal_playlist.name)

            playlist = Playlist(name=tidal_playlist.name)
            for tidal_track in _fetch_with_limit(
                tidal_playlist.tracks,
                message=f"Fetching tracks from playlist {tidal_playlist.name}",
            ):
                track = _parse_track(tidal_track)
                if track is not None:
                    playlist.tracks.append(track)

            playlists.append(playlist)

        return playlists
