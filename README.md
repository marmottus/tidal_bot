# Tidal Bot

## Import a playlist from a YAML file

```sh
python -m tidal_bot --import-playlist playlist.yaml
```

Parses the YAML file, searches each song on Tidal and adds it to every target
playlist it is not already in. Playlists are created if they do not exist,
optionally inside their given folder. Every song missing from at least one
playlist is interactively confirmed once (pick one of the candidates or skip)
and then added to all playlists it is missing from. When playlists were
updated, a summary is sent to Telegram (if `config/telegram.yaml` is
configured).

YAML file format:

```yaml
playlists:
  - name: My Playlist
    folder: My Folder  # optional
    public: true
  - name: Another Playlist
    public: false
songs:
  - artist: Some Artist
    song: Some Song
```

