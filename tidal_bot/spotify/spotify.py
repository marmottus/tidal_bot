import logging
from datetime import timedelta
from pathlib import Path

import requests
import yaml
from pydantic import ValidationError
from spotipy.cache_handler import CacheFileHandler
from spotipy.client import Spotify
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
        isrc=spotify_track.external_ids.isrc,
        id=spotify_track.id if spotify_track.id is not None else -1,
        duration=timedelta(milliseconds=spotify_track.duration_ms),
        album=album,
    )


class MySpotify(Api):
    def __init__(self) -> None:
        logger.info("Authenticating to Spotify API")

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
        session_path = (
            Path(__file__).parent.parent.parent / ".session/spotify/session.json"
        )
        session_path.parent.mkdir(parents=True, exist_ok=True)
        cache_handler = CacheFileHandler(cache_path=session_path)

        if session_path.exists():
            logger.debug("Load cached Spotify session %s", session_path)

        try:
            sp_auth = SpotifyOAuth(
                scope=scopes,
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=redirect_uri,
                requests_timeout=2,
                open_browser=False,
                cache_handler=cache_handler,
            )

            access_token = sp_auth.get_access_token(as_dict=False)

            self._spotify = Spotify(auth=access_token)

            logger.info("Successfully authenticated to Spotify")
        except Exception as e:
            logger.error("Failed to authenticate to Spotify: %s", e)
            session_path.unlink(missing_ok=True)
            raise

    def _get_tracks_from_playlist(
        self,
        playlist: SimplifiedPlaylistObject,
    ) -> list[Track]:
        if playlist.id is None:
            logger.warning("playlist ID is None, cannot fetch tracks")
            return []

        logger.info("Fetching tracks from playlist: %s", playlist.name)

        tracks: list[Track] = []

        offset = 0
        limit = 100

        while True:
            logger.debug("fetching %u tracks at offset %d", limit, offset)
            response = self._spotify.playlist_items(
                playlist_id=playlist.id, offset=offset, limit=limit
            )
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

        return tracks

    def get_playlists(self, filter: PlaylistFilter | None = None) -> list[Playlist]:
        playlists: list[Playlist] = []

        logger.info("Fetching Spotify playlists")

        response = self._spotify.current_user_playlists()

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
            if spotify_playlist.external_urls is not None:
                uri = spotify_playlist.external_urls.spotify
            else:
                uri = None

            playlists.append(
                Playlist(
                    name=spotify_playlist.name, tracks=tracks, uri=uri, image=image
                )
            )
        return playlists

    def search_track(self, track: Track) -> Track | None:
        return None

    def merge_playlist(
        self,
        playlist: Playlist,
        playlist_description: str | None = None,
        parent_folder_name: str | None = None,
    ) -> AddedTracksResult | None:
        return None
