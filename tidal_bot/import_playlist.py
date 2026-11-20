import logging
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError

from tidal_bot.api import Playlist, Track, normalize_artist_name
from tidal_bot.bot.telegram import TelegramSender, markdown_escape
from tidal_bot.tidal.tidal import MyTidal

logger = logging.getLogger("import")


class SongEntry(BaseModel):
    artist: str
    song: str

    def full_name(self) -> str:
        return f"{self.song} - {self.artist}"


class PlaylistTarget(BaseModel):
    name: str
    public: bool = False
    folder: str | None = None


class PlaylistFile(BaseModel):
    playlists: list[PlaylistTarget]
    songs: list[SongEntry]


def _parse_playlist_file(yaml_path: Path) -> PlaylistFile | None:
    if not yaml_path.exists():
        logger.error("Playlist file %s does not exist", yaml_path)
        return None

    logger.info("Loading playlist from %s", yaml_path)
    with yaml_path.open() as f:
        yaml_data = yaml.safe_load(f)

    try:
        return PlaylistFile.model_validate(yaml_data)
    except ValidationError as e:
        logger.error("Failed to parse playlist file %s: %s", yaml_path, e)
        return None


def _song_matches_track(song: SongEntry, track: Track) -> bool:
    if song.song.strip().casefold() != track.name.strip().casefold():
        return False

    song_artists = normalize_artist_name(song.artist)
    track_artists: set[str] = set()
    for artist in track.artists:
        track_artists.update(normalize_artist_name(artist))

    return bool(song_artists & track_artists)


def _rank_candidates(song: SongEntry, candidates: list[Track]) -> list[Track]:
    """Order candidates with exact title/artist matches first."""
    return sorted(candidates, key=lambda t: not _song_matches_track(song, t))


def _ask_track_choice(playlist_names: str, num_candidates: int) -> int | None:
    prompt = (
        f"➕ Add to {playlist_names}? "
        f"[Enter = add 1, 1-{num_candidates} = pick, s = skip] "
    )

    while True:
        choice = input(prompt).strip().lower()

        if choice in ("s", "n", "skip", "no"):
            return None

        if not choice:
            return 0

        if choice.isdigit() and 1 <= int(choice) <= num_candidates:
            return int(choice) - 1

        logger.warning("Invalid choice: %s", choice)


async def _send_summary(
    yaml_path: Path,
    playlists: list[Playlist],
    added: dict[str, list[Track]],
    not_found: list[SongEntry],
    errors: list[tuple[SongEntry, str]],
) -> None:
    try:
        bot = TelegramSender()
    except Exception as e:
        logger.warning("Telegram notification disabled: %s", e)
        return

    lines = [f"🎵 Playlist import from *{markdown_escape(yaml_path.name)}*"]

    for playlist in playlists:
        tracks = added[playlist.name]
        if not tracks:
            continue

        lines.append("")
        lines.append(f"📋 *{markdown_escape(playlist.name)}*")
        if playlist.uri:
            lines.append(f"🔗 [Open in Tidal]({playlist.uri})")
        lines.append(f"✅ *Added*: {len(tracks)}")
        lines.extend(f" 🎤 {markdown_escape(t.full_name())}" for t in tracks)

    if not_found:
        lines.append("")
        lines.append("*Tracks not found:*")
        lines.extend(f" ❓ {markdown_escape(s.full_name())}" for s in not_found)

    if errors:
        lines.append("")
        lines.append("*Tracks with errors:*")
        lines.extend(
            f" ❌ {markdown_escape(f'{s.full_name()} ({name})')}" for s, name in errors
        )

    await bot.send_message(message="\n".join(lines))


async def import_playlist(yaml_path: Path) -> int:
    config = _parse_playlist_file(yaml_path)
    if config is None:
        return 1

    tidal = MyTidal()
    try:
        await tidal.connect()
    except Exception as e:
        logger.error("Tidal connection error: %s", e)
        return 1

    playlists: list[Playlist] = []
    for target in config.playlists:
        playlist = tidal.create_playlist(
            playlist_name=target.name,
            parent_folder_name=target.folder,
            public=target.public,
        )
        if playlist is None:
            logger.error("Failed to create or get Tidal playlist %s", target.name)
            return 1

        playlists.append(playlist)

    added: dict[str, list[Track]] = {p.name: [] for p in playlists}
    already_present: list[SongEntry] = []
    declined: list[SongEntry] = []
    not_found: list[SongEntry] = []
    errors: list[tuple[SongEntry, str]] = []

    for song in config.songs:
        missing_from = [
            playlist
            for playlist in playlists
            if not any(_song_matches_track(song, track) for track in playlist.tracks)
        ]

        if not missing_from:
            logger.info("⏭️  %s already in all playlists, skipping", song.full_name())
            already_present.append(song)
            continue

        candidates = _rank_candidates(
            song, tidal.search_tracks(f"{song.song} {song.artist}", limit=5)
        )

        if not candidates:
            logger.warning("❓ No Tidal track found for %s", song.full_name())
            not_found.append(song)
            continue

        logger.info("🎵 Candidates for %s:", song.full_name())
        for i, candidate in enumerate(candidates, start=1):
            album = f" [{candidate.album.name}]" if candidate.album else ""
            logger.info("  %d. %s%s", i, candidate.full_name(), album)

        playlist_names = ", ".join(f"'{p.name}'" for p in missing_from)
        choice = _ask_track_choice(playlist_names, len(candidates))
        if choice is None:
            logger.info("🚫 Skipping %s", song.full_name())
            declined.append(song)
            continue

        track = candidates[choice]
        for playlist in missing_from:
            if tidal.add_track(playlist, track):
                logger.info(
                    "✅ Added %s to playlist %s", track.full_name(), playlist.name
                )
                added[playlist.name].append(track)
            else:
                errors.append((song, playlist.name))

    logger.info("--------------------------------")
    logger.info("Import summary:")
    for playlist in playlists:
        logger.info("  📋 %s:", playlist.name)
        logger.info("    ✅ Added: %d", len(added[playlist.name]))
        if playlist.uri:
            logger.info("    🔗 %s", playlist.uri)
    logger.info("  ⏭️  Already present: %d", len(already_present))
    logger.info("  🚫 Declined: %d", len(declined))
    logger.info("  ❓ Not found: %d", len(not_found))
    logger.info("  ❌ Errors: %d", len(errors))

    for song in not_found:
        logger.info("  ❓ %s", song.full_name())
    for song, playlist_name in errors:
        logger.info("  ❌ %s (%s)", song.full_name(), playlist_name)

    if any(added.values()) or not_found or declined or errors:
        await _send_summary(yaml_path, playlists, added, not_found + declined, errors)

    return 0 if not errors else 1
