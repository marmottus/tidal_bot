import asyncio
import logging
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError

from tidal_bot.api import Playlist, Track
from tidal_bot.bot.telegram import TelegramBot, markdown_escape
from tidal_bot.logger import init_logging
from tidal_bot.spotify.spotify import MySpotify
from tidal_bot.tidal.tidal import MyTidal

logger = logging.getLogger("main")


class SyncPlaylist(BaseModel):
    name: str
    playlists: list[str]


class PlaylistYaml(BaseModel):
    sync_playlists: list[SyncPlaylist]


def _parse_playlist_to_sync() -> PlaylistYaml | None:
    config_folder = Path(__file__).parent.parent / "config"
    playlists_yaml = config_folder / "playlists.yaml"

    if playlists_yaml.exists():
        logger.info("Loading playlists to sync from %s", playlists_yaml)
        with playlists_yaml.open() as f:
            yaml_data = yaml.safe_load(f)
            try:
                config = PlaylistYaml.model_validate(yaml_data)
                return config
            except ValidationError as e:
                logger.error("Failed to parse playlists.yaml: %s", e)
                return None

    return None


async def _merge_spotify_playlists(
    spotify: MySpotify,
    tidal: MyTidal,
    bot: TelegramBot,
    playlist_name: str,
    playlists: list[str],
) -> None:
    logger.info(
        "Syncing Spotify playlists %s into Tidal playlist %s",
        playlists,
        playlist_name,
    )

    found_playlists = spotify.get_playlists(filter=lambda name: name in playlists)

    spotify_playlists: list[Playlist] = []
    for playlist in playlists:
        try:
            spotify_playlist = next(p for p in found_playlists if p.name == playlist)
            spotify_playlists.append(spotify_playlist)
        except StopIteration:
            logger.warning("Spotify playlist %s not found", playlist)
            await bot.send_message(
                message=f"⚠️ Spotify playlist *{markdown_escape(playlist)}* not found"
            )
            return

    tidal_playlist = tidal.create_playlist(
        playlist_name=playlist_name, parent_folder_name="Eurovision", public=True
    )
    if tidal_playlist is None:
        logger.error("Failed to create or get Tidal playlist %s", playlist_name)
        await bot.send_message(
            message=f"⚠️ Failed to create or get Tidal playlist *{markdown_escape(playlist_name)}*"
        )
        return

    ordered_tracks: list[Track] = []
    for spotify_playlist in spotify_playlists:
        result = tidal.merge_playlists(
            from_playlist=spotify_playlist,
            dest_playlist=tidal_playlist,
        )
        if result is None:
            logger.error("Failed to add tracks to playlist %s", tidal_playlist.name)
            await bot.send_message(
                message=f"⚠️ Failed to add tracks to playlist *{markdown_escape(tidal_playlist.name)}*"
            )
            continue

        logger.info(
            "Playlist %s updated from %s: Added %d, Skipped %d, Not Found %d",
            spotify_playlist.name,
            tidal_playlist.name,
            len(result.added),
            len(result.skipped),
            len(result.not_found),
        )

        ordered_tracks += result.tracks

        if result.added:
            message = "\n".join(
                [
                    f"🎵 Playlist *{markdown_escape(tidal_playlist.name)}* synced",
                    f"from *{markdown_escape(spotify_playlist.name)}*",
                    "",
                    f"✅ *Added*: {len(result.added)}",
                    f"⏭️ *Skipped*: {len(result.skipped)}",
                    f"❓ *Not Found*: {len(result.not_found)}",
                    f"❌ *Error*: {len(result.add_error)}",
                ]
            )

            message += "\n\n*Added tracks:*\n"
            message += "\n".join(
                f" 🎤 {markdown_escape(track.full_name())}" for track in result.added
            )

            if result.not_found:
                message += "\n\n*Tracks not found:*\n"
                message += "\n".join(
                    f" ❓ {markdown_escape(track.full_name())}"
                    for track in result.not_found
                )

            if result.add_error:
                message += "\n\n*Tracks with errors:*\n"
                message += "\n".join(
                    f" ❌ {markdown_escape(track.full_name())}"
                    for track in result.add_error
                )

            await bot.send_message(message=message)

    has_reoganized = tidal.reorganize_playlist(tidal_playlist, *ordered_tracks)
    if has_reoganized is None:
        logger.error("Failed to reorganize playlist %s", tidal_playlist.name)
        await bot.send_message(
            message=f"⚠️ Failed to reorganize playlist *{markdown_escape(tidal_playlist.name)}*"
        )
        return

    if has_reoganized:
        await bot.send_message(
            message=f"✅ Playlist *{markdown_escape(tidal_playlist.name)}* has been reorganized"
        )


async def main() -> None:
    config = _parse_playlist_to_sync()
    bot = TelegramBot()
    spotify = MySpotify()
    tidal = MyTidal()

    if config is None:
        logger.info("No playlists to sync found in configuration")
        return

    try:
        await spotify.connect()
    except TimeoutError as e:
        logger.error("Spotify connection timed out: %s", e)
        await bot.send_message(
            message=markdown_escape("⚠️ Spotify connection lost, please refresh token")
        )
        return
    except Exception as e:
        logger.error("Spotify connection error: %s", e)
        await bot.send_message(
            message=markdown_escape(f"⚠️ Spotify connection error occurred: {e}")
        )
        return

    try:
        await tidal.connect()
    except TimeoutError as e:
        logger.error("Tidal connection timed out: %s", e)
        await bot.send_message(
            message=markdown_escape("⚠️ Tidal connection lost, please refresh token")
        )
        return
    except Exception as e:
        logger.error("Tidal connection error: %s", e)
        await bot.send_message(
            message=markdown_escape(f"⚠️ Tidal connection error occurred: {e}")
        )
        return

    for playlist in config.sync_playlists:
        if not playlist.playlists:
            logger.warning(
                "No source playlists defined for sync playlist %s", playlist.name
            )
            continue

        await _merge_spotify_playlists(
            spotify=spotify,
            tidal=tidal,
            bot=bot,
            playlist_name=playlist.name,
            playlists=playlist.playlists,
        )


if __name__ == "__main__":
    init_logging()

    asyncio.run(main(), debug=False)
