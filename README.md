# Omarchy Lyrics

Synchronized lyrics for the current MPRIS track, directly in the Omarchy bar.

The plugin selects the active media player, looks up lyrics from LRCLIB when the
track changes, and follows timed LRC lines as playback advances. Click the bar
widget to open the complete lyrics panel.

## Features

- Current synchronized lyric line in the bar
- Full lyrics panel with active-line highlighting and automatic scrolling
- MPRIS playback controls: middle-click toggles playback; the wheel changes tracks
- Exact LRCLIB lookup with ranked search fallback
- Plain-lyrics and instrumental fallbacks
- Local result cache with shorter negative-result expiry
- Estimated playback clock when a player does not expose a working position

## Requirements

- Omarchy 4.0 or newer
- Python 3
- An MPRIS-compatible media player
- Network access to `https://lrclib.net` for uncached tracks

No Python packages, API keys, `playerctl`, or background containers are required.

## Local installation

For development, link the checkout into the user plugin directory:

```bash
ln -s "$PWD" ~/.config/omarchy/plugins/io.github.ryuhzk.lyrics
omarchy-shell shell rescanPlugins
omarchy plugin enable io.github.ryuhzk.lyrics --after omarchy.clock
```

The plugin source and `shell.json` both hot-reload. Remove the link and disable
the plugin when it is no longer wanted.

## Usage

- Left-click: open or close the full lyrics panel
- Right-click: bypass the cache and fetch the current lyrics again
- Middle-click: play or pause
- Scroll up/down: previous or next track
- Escape: close the panel
- Tab / Shift+Tab: switch between neighboring bar panels

## Settings

| Setting                | Default | Meaning                                              |
| ---------------------- | ------: | ---------------------------------------------------- |
| `maxWidth`             |   `360` | Maximum Bar lyric width                              |
| `panelWidth`           |   `520` | Full lyrics panel width                              |
| `offsetMs`             |     `0` | Positive advances lyrics; negative delays them       |
| `showTrackWhenMissing` |  `true` | Show title and artist before or without a lyric line |

For example:

```bash
omarchy bar set io.github.ryuhzk.lyrics offsetMs 500
```

## Synchronization behavior

Players with a working MPRIS position provide exact seeking and startup sync.
Some browser integrations advertise position support but always return zero.
For those players, the plugin estimates time from track changes and pause/resume
state. That estimate is accurate after the next track begins, but cannot recover
the initial offset when the plugin starts halfway through a song or after a seek.

## Privacy and caching

Track title, artist, album, and duration are sent to LRCLIB for lookup. Lyrics
responses are cached under `${XDG_CACHE_HOME:-~/.cache}/omarchy-lyrics` for up
to 30 days; misses expire after six hours. The plugin does not use credentials
or transmit local file paths.

## Development

Run the complete local check:

```bash
./check
```

It compiles and tests the Python backend, validates the plugin manifest, checks
the stable metadata contract, and runs `qmllint` when available.
