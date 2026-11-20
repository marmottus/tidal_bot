import argparse
import asyncio
import logging
import sys
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError

from tidal_bot.api import Playlist, Track
from tidal_bot.bot.telegram import TelegramBot, markdown_escape
from tidal_bot.config import CONFIG_FILE, PLAYLISTS_YAML_PATH
from tidal_bot.import_playlist import import_playlist
from tidal_bot.logger import init_logging
from tidal_bot.rss.rss import Rss
from tidal_bot.spotify.spotify import MySpotify
from tidal_bot.tidal.tidal import MyTidal

logger = logging.getLogger("main")


class SyncPlaylist(BaseModel):
    name: str
    playlists: list[str]
    folder: str


class PlaylistYaml(BaseModel):
    sync_interval_seconds: int
    sync_playlists: list[SyncPlaylist]
    enable_bot: bool = False


class Config(BaseModel):
    enable_bot: bool = False
    enable_rss: bool = False
    sync_interval_seconds: int = 3600


def _parse_config() -> Config:
    if not CONFIG_FILE.exists():
        return Config()

    logger.info("Loading playlists to sync from %s", CONFIG_FILE)
    with CONFIG_FILE.open() as f:
        yaml_data = yaml.safe_load(f)
        try:
            config = Config.model_validate(yaml_data)
            return config
        except ValidationError as e:
            logger.error("Failed to parse config file: %s", e)
            return Config()


def _parse_playlist_to_sync() -> PlaylistYaml | None:
    if PLAYLISTS_YAML_PATH.exists():
        logger.info("Loading playlists to sync from %s", PLAYLISTS_YAML_PATH)
        with PLAYLISTS_YAML_PATH.open() as f:
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
    parent_folder_name: str,
    playlists: list[str],
    report_no_update: bool = False,
) -> None:
    logger.info(
        "Syncing Spotify playlists %s into Tidal playlist %s",
        playlists,
        playlist_name,
    )

    max_tries = 3
    spotify_playlists: list[Playlist] = []
    for i in range(max_tries):
        found_playlists = spotify.get_playlists(filter=lambda name: name in playlists)
        spotify_playlists.clear()

        for spotify_playlist_name in playlists:
            try:
                spotify_playlist = next(
                    p for p in found_playlists if p.name == spotify_playlist_name
                )
                spotify_playlists.append(spotify_playlist)
            except StopIteration:
                logger.warning("Spotify playlist %s not found", spotify_playlist_name)
                break

        if len(spotify_playlists) == len(playlists):
            break

        logger.warning(
            "Not all Spotify playlists found for syncing into %s (found %d of %d)",
            playlist_name,
            len(spotify_playlists),
            len(playlists),
        )

        if i == max_tries - 1:
            await bot.send_message(
                message=f"⚠️ Not all Spotify playlists found for syncing into *{markdown_escape(playlist_name)}*"
            )
            return

        await asyncio.sleep(2)

    tidal_playlist = tidal.create_playlist(
        playlist_name=playlist_name, parent_folder_name=parent_folder_name, public=True
    )
    if tidal_playlist is None:
        logger.error("Failed to create or get Tidal playlist %s", playlist_name)
        await bot.send_message(
            message=f"⚠️ Failed to create or get Tidal playlist *{markdown_escape(playlist_name)}*"
        )
        return

    ordered_tracks: list[Track] = []
    playlist_updated = report_no_update
    total_num_tracks = 0

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

        for t in result.tracks:
            if t not in ordered_tracks:
                ordered_tracks.append(t)
            else:
                logger.debug(
                    "Track %s already in ordered tracks, skipping duplicate",
                    t.full_name(),
                )

        num_tracks = len(result.added) + len(result.skipped)
        message = "\n".join(
            [
                f"🎵 Playlist *{markdown_escape(tidal_playlist.name)}* synced",
                f"from *{markdown_escape(spotify_playlist.name)}*",
                "",
                f"\\#️⃣ *Tracks*: {num_tracks}",
                f"✅ *Added*: {len(result.added)}",
                f"⏭️ *Skipped*: {len(result.skipped)}",
                f"❓ *Not Found*: {len(result.not_found)}",
                f"❌ *Error*: {len(result.add_error)}",
            ]
        )

        total_num_tracks += num_tracks

        if result.added:
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

        if result.added or report_no_update:
            playlist_updated = True
            await bot.send_message(message=message)

    has_reorganized = tidal.reorganize_playlist(tidal_playlist, *ordered_tracks)
    if has_reorganized is None:
        logger.error("Failed to reorganize playlist %s", tidal_playlist.name)
        await bot.send_message(
            message=f"⚠️ Failed to reorganize playlist *{markdown_escape(tidal_playlist.name)}*"
        )
        return

    if has_reorganized:
        playlist_updated = True
        await bot.send_message(
            message=f"✅ Playlist *{markdown_escape(tidal_playlist.name)}* has been reorganized"
        )

    if playlist_updated:
        message = "\n".join(
            [
                f"✅ Playlist *{markdown_escape(tidal_playlist.name)}* synced successfully",
                f"🔗 [Open in Tidal]({tidal_playlist.uri})",
                f"\\#️⃣ *Tracks*: {total_num_tracks}",
                f"⌛ *Duration*: {tidal_playlist.duration}",
            ]
        )

        await bot.send_message(message=message)


async def _sync_command(bot: TelegramBot, report_no_update: bool = True) -> None:
    await bot.send_message(
        message=markdown_escape("This command is currently disabled. Fuck Spotify.")
    )
    return

    config = _parse_playlist_to_sync()

    if config is None:
        logger.info("No playlists to sync found in configuration")
        await bot.send_message(message="⚠️ No playlists to sync found in configuration")
        return

    spotify = MySpotify()
    tidal = MyTidal()

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
            parent_folder_name=playlist.folder,
            playlists=playlist.playlists,
            report_no_update=report_no_update,
        )


async def _list_command(bot: TelegramBot) -> None:
    await bot.send_message(
        message=markdown_escape("This command is currently disabled. Fuck Spotify.")
    )
    return

    config = _parse_playlist_to_sync()

    if config is None:
        logger.info("No playlists to sync found in configuration")
        await bot.send_message(message="⚠️ No playlists to sync found in configuration")
        return

    for dest_playlist in config.sync_playlists:
        message = f"📋 *{markdown_escape(dest_playlist.name)}*\n"
        for source_playlist in dest_playlist.playlists:
            message += f"• {markdown_escape(source_playlist)}\n"

        await bot.send_message(message=message)


async def _sync_rss(bot: TelegramBot | None) -> None:
    rss = Rss()
    rss.load_entries()
    rss.parse_rss_entries()

    for entry in reversed(rss.entries):
        if entry.notified:
            continue

        logger.info("Send RSS Entry: %s", entry)

        message = "📰 Good news everyone\\!\n\n"
        if entry.pub_date:
            message += f"Published: {markdown_escape(entry.pub_date)}\n\n"
        message += f"*{markdown_escape(entry.title)}*\n\n"
        message += f"{markdown_escape(entry.description)}\n\n"

        if entry.links:
            message += f"[Link]({entry.links[0]})\n"

        if bot is not None:
            await bot.send_message(message=message)

        entry.notified = True

    rss.save_entries()


async def main() -> None:
    config = _parse_config()

    bot: TelegramBot | None = None
    if config.enable_bot:
        logger.info("Starting Telegram bot")
        bot = TelegramBot(sync_callback=_sync_command, list_callback=_list_command)

    try:
        if bot is not None:
            await bot.start()

        while True:
            # await _sync_command(bot, report_no_update=False)
            if config.enable_rss:
                await _sync_rss(bot)
            logger.info("Next sync in %u seconds", config.sync_interval_seconds)
            await asyncio.sleep(config.sync_interval_seconds)
    except asyncio.CancelledError:
        if bot is not None:
            await bot.stop()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="tidal_bot")
    parser.add_argument(
        "--import-playlist",
        type=Path,
        metavar="YAML_FILE",
        help="parse a YAML playlist file, create the Tidal playlist (in the "
        "optional folder) if it does not exist and interactively confirm "
        "every song not yet in the playlist",
    )
    return parser.parse_args()


if __name__ == "__main__":
    init_logging()

    args = _parse_args()
    if args.import_playlist is not None:
        sys.exit(asyncio.run(import_playlist(args.import_playlist)))

    asyncio.run(main(), debug=False)
