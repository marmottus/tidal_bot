import asyncio
import logging
import sys
from asyncio import Queue, Task, get_event_loop
from datetime import timedelta
from pathlib import Path

import requests
import yaml
from pydantic import ValidationError
from spotipy.cache_handler import CacheFileHandler
from spotipy.client import Spotify
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyOAuth

from tidal_bot.api import AddedTracksResult, Album, Api, Playlist, PlaylistFilter, Track
from tidal_bot.spotify.model import (
    PagingPlaylistObject,
    PagingPlaylistTrackObject,
    SimplifiedAlbumObject,
    SimplifiedPlaylistObject,
    TrackObject,
)

logger = logging.getLogger("spotify")


async def _timed_input(prompt: str, timeout: float = 3.0) -> str:
    sys.stdout.write(prompt)
    sys.stdout.flush()

    loop = get_event_loop()
    queue: Queue[str] = Queue()
    tasks: set[Task[None]] = set()

    def _done_callback(task: Task[None]) -> None:
        tasks.discard(task)

    def _reader() -> None:
        task = loop.create_task(queue.put(sys.stdin.readline()))
        tasks.add(task)
        task.add_done_callback(_done_callback)

    loop.add_reader(sys.stdin.fileno(), _reader)

    try:
        return await asyncio.wait_for(queue.get(), timeout=timeout)
    except TimeoutError:
        logger.error("User input timed out after %.1f seconds", timeout)
        sys.stdout.write("\n")
        sys.stdout.flush()
        raise
    finally:
        logger.debug("Cleaning up input reader")

        loop.remove_reader(sys.stdin.fileno())

        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        tasks.clear()


def _parse_album(spotify_album: SimplifiedAlbumObject) -> Album | None:
    if spotify_album.name is None:
        logger.debug("Spotify album name is None")
        return None

    if spotify_album.total_tracks is None:
        logger.debug("Spotify album total tracks is None")
        return None

    if spotify_album.artists is None:
        logger.debug("Spotify album artists is None")
        artists = set()
    else:
        artists = {
            artist.name for artist in spotify_album.artists if artist.name is not None
        }

    return Album(
        name=spotify_album.name,
        total_tracks=spotify_album.total_tracks,
        artists=artists,
    )


def _parse_track(spotify_track: TrackObject) -> Track | None:
    if spotify_track.name is None:
        logger.debug("Spotify track name is None")
        return None

    # if spotify_track.id is None:
    #     logger.debug("Spotify track ID is None")
    #     return None

    if spotify_track.external_ids is None:
        logger.debug("Spotify track external IDs is None")
        return None

    if spotify_track.external_ids.isrc is None:
        logger.debug("Spotify track ISRC is None")
        return None

    if spotify_track.duration_ms is None:
        logger.debug("Spotify track duration is None")
        return None

    if spotify_track.artists is None:
        logger.debug("Spotify track artists is None")
        artists = set()
    else:
        artists = {
            artist.name for artist in spotify_track.artists if artist.name is not None
        }

    album = _parse_album(spotify_track.album) if spotify_track.album else None

    return Track(
        name=spotify_track.name,
        artists=artists,
        isrc=spotify_track.external_ids.isrc.upper(),
        id=spotify_track.id if spotify_track.id is not None else -1,
        duration=timedelta(milliseconds=spotify_track.duration_ms),
        album=album,
    )


class MySpotify(Api):
    def __init__(self) -> None:
        logger.info("Initialize Spotify API")

        config_file = Path(__file__).parent.parent.parent / "config" / "spotify.yaml"
        if not config_file.exists():
            logger.error("Spotify config file %s does not exist", config_file)
            raise FileNotFoundError(f"Spotify config file {config_file} not found")

        with config_file.open() as f:
            config = yaml.safe_load(f)
            try:
                spotify_config = config["spotify"]
                client_id = spotify_config["client_id"]
                client_secret = spotify_config["client_secret"]
                redirect_uri = spotify_config["redirect_uri"]
            except KeyError as e:
                logger.error(
                    "Spotify config file %s is missing required fields: %s",
                    config_file,
                    e,
                )
                raise

        scopes = "playlist-read-private, user-library-read"
        self._session_path = (
            Path(__file__).parent.parent.parent / ".session/spotify/session.json"
        )
        self._session_path.parent.mkdir(parents=True, exist_ok=True)
        if self._session_path.exists():
            logger.debug("Load cached Spotify session %s", self._session_path)

        self._sp_auth = SpotifyOAuth(
            scope=scopes,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            requests_timeout=2,
            open_browser=False,
            cache_handler=CacheFileHandler(cache_path=str(self._session_path)),
        )
        self._spotify: Spotify | None = None

    async def connect(self) -> None:
        logger.info("Authenticating to Spotify")

        def _get_user_input(prompt: str) -> str:
            return asyncio.run(_timed_input(prompt, timeout=30.0))

        def _get_access_token() -> str:
            logger.info("Get Spotify access token")
            self._sp_auth._get_user_input = _get_user_input  # type: ignore[method-assign] # noqa: SLF001
            return self._sp_auth.get_access_token(as_dict=False)

        try:
            access_token = await asyncio.to_thread(_get_access_token)
            self._spotify = Spotify(auth=access_token)
            logger.info("Successfully authenticated to Spotify")
        except Exception as e:
            logger.error("Failed to authenticate to Spotify: %s", e)
            Path(self._sp_auth.cache_handler.cache_path).unlink(missing_ok=True)
            raise

    def _get_tracks_from_playlist(
        self,
        playlist: SimplifiedPlaylistObject,
    ) -> list[Track]:
        if playlist.id is None:
            logger.warning("playlist ID is None, cannot fetch tracks")
            return []

        if self._spotify is None:
            logger.error("Spotify client is not initialized")
            return []

        logger.info("Fetching tracks from playlist: %s", playlist.name)

        tracks: list[Track] = []

        offset = 0
        limit = 100

        while True:
            logger.debug("fetching %u tracks at offset %d", limit, offset)
            try:
                response = self._spotify.playlist_items(
                    playlist_id=playlist.id, offset=offset, limit=limit
                )
            except SpotifyException as e:
                logger.error(
                    "Failed to fetch tracks from playlist %s: %s",
                    playlist.name,
                    e,
                )
                return []

            try:
                spotify_tracks = PagingPlaylistTrackObject.model_validate(response)
            except ValidationError as e:
                logger.error(
                    "Failed to parse tracks from playlist %s: %s",
                    playlist.name,
                    e,
                )
                break

            if spotify_tracks.items is not None:
                for spotify_track in spotify_tracks.items:
                    if isinstance(spotify_track.track, TrackObject):
                        track = _parse_track(spotify_track.track)
                        if track is not None:
                            tracks.append(track)
            else:
                logger.debug("No items found in playlist %s", playlist.name)

            if spotify_tracks.next is None:
                break

            offset += limit

        logger.debug("fetched total %d tracks", len(tracks))

        for track in list(tracks):
            found_tracks = [t for t in tracks if t == track]
            if len(found_tracks) > 1:
                logger.debug(
                    "Found %d duplicates for track %s, keeping first occurrence",
                    len(found_tracks),
                    track,
                )
                for t in found_tracks[1:]:
                    tracks.remove(t)

        logger.info(
            "Total %d unique tracks in playlist %s",
            len(tracks),
            playlist.name,
        )

        return tracks

    def get_playlists(self, filter: PlaylistFilter | None = None) -> list[Playlist]:
        playlists: list[Playlist] = []

        logger.info("Fetching Spotify playlists")

        if self._spotify is None:
            logger.error("Spotify client is not initialized")
            return []

        try:
            response = self._spotify.current_user_playlists()
        except SpotifyException as e:
            logger.error("Failed to fetch Spotify playlists: %s", e)
            return []

        try:
            spotify_playlists = PagingPlaylistObject.model_validate(response)
        except ValidationError as e:
            logger.error("Failed to parse Spotify playlists: %s", e)
            return []

        if not spotify_playlists.items:
            logger.info("No Spotify playlists found")
            return []

        for spotify_playlist in spotify_playlists.items:
            if spotify_playlist.name is None:
                logger.debug("Skipping playlist with no name")
                continue

            if filter is not None and not filter(spotify_playlist.name):
                continue

            image: bytes | None = None
            if spotify_playlist.images:
                first_image = spotify_playlist.images[0]
                if first_image.url is not None:
                    try:
                        logger.debug("Fetching album image from %s", first_image.url)
                        response = requests.get(first_image.url, timeout=5)
                        response.raise_for_status()
                        image = response.content
                    except requests.HTTPError as e:
                        logger.warning(
                            "Failed to fetch album image from %s: %s",
                            first_image.url,
                            e,
                        )

            tracks = self._get_tracks_from_playlist(spotify_playlist)
            if not tracks:
                logger.info(
                    "No tracks found in Spotify playlist %s",
                    spotify_playlist.name,
                )

            if spotify_playlist.external_urls is not None:
                uri = spotify_playlist.external_urls.spotify
            else:
                uri = None

            playlists.append(
                Playlist(
                    name=spotify_playlist.name,
                    tracks=tracks,
                    uri=uri,
                    image=image,
                    api_playlist=spotify_playlist,
                )
            )
        return playlists

    def search_track(self, track: Track) -> Track | None:
        return None

    def merge_playlists(
        self,
        from_playlist: Playlist,
        dest_playlist: Playlist,
    ) -> AddedTracksResult | None:
        return None

    def create_playlist(
        self,
        playlist_name: str,
        playlist_description: str | None = None,
        parent_folder_name: str | None = None,
        public: bool = False,
    ) -> Playlist | None:
        return None
