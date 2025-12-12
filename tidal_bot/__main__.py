import asyncio
import logging

from tidal_bot.bot.telegram import TelegramBot
from tidal_bot.logger import init_logging
from tidal_bot.spotify.spotify import MySpotify
from tidal_bot.tidal.tidal import MyTidal

logger = logging.getLogger("main")


def _filter_playlist(name: str) -> bool:
    return name.startswith("EUROVISION")


async def main() -> None:
    bot = TelegramBot()

    spotify = MySpotify()
    try:
        await spotify.connect()
    except TimeoutError as e:
        logger.error("Spotify connection timed out: %s", e)
        await bot.send_message(
            message="⚠️ Spotify connection lost, please refresh token"
        )
        return

    tidal = MyTidal()
    try:
        await tidal.connect()
    except TimeoutError as e:
        logger.error("Tidal connection timed out: %s", e)
        await bot.send_message(message="⚠️ Tidal connection lost, please refresh token")
        return

    spotify_playlists = spotify.get_playlists(filter=_filter_playlist)

    for p in spotify_playlists:
        description = (
            f"Playlist synced from Spotify {p.uri}" if p.uri is not None else None
        )

        result = tidal.merge_playlist(
            playlist=p,
            playlist_description=description,
            parent_folder_name="Eurovision",
        )
        if result is None:
            logger.error("Failed to add tracks to playlist '%s'", p.name)
            continue

        logger.info(
            "Playlist '%s': Added %d, Skipped %d, Not Found %d",
            p.name,
            len(result.added),
            len(result.skipped),
            len(result.not_found),
        )

        if result.added:
            message = "\n".join(
                [
                    f"🎵 Playlist *{p.name_escaped()}*",
                    "",
                    f"✅ *Added*: {len(result.added)}",
                    f"⏭️ *Skipped*: {len(result.skipped)}",
                    f"❓ *Not Found*: {len(result.not_found)}",
                    f"❌ *Error*: {len(result.add_error)}",
                ]
            )

            message += "\n\n*Added tracks:*\n"
            message += "\n".join(
                f" 🎤 {track.full_name_escaped()}" for track in result.added
            )

            if result.not_found:
                message += "\n\n*Tracks not found:*\n"
                message += "\n".join(
                    f" ❓ {track.full_name_escaped()}" for track in result.not_found
                )

            if result.add_error:
                message += "\n\n*Tracks with errors:*\n"
                message += "\n".join(
                    f" ❌ {track.full_name_escaped()}" for track in result.add_error
                )

            await bot.send_message(message=message)


if __name__ == "__main__":
    init_logging()

    asyncio.run(main(), debug=False)
