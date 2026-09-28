# Omarchy Lyrics

Synchronized lyrics for whatever is playing, over the desktop and in a panel
from the Omarchy bar, with a translation or romanization under each line so you
can sing along to songs in languages you are still learning.

## Features

- The current and next line over the desktop while a track is playing. The
  lines are click-through and fade away when playback stops.
- The romanization (Japanese romaji, Cantonese jyutping) set over the
  characters it spells, one syllable per character in Chinese and a whole
  reading per kanji in Japanese, with the translation on a line underneath.
  Lines whose romanization cannot be paired with the words show it underneath
  instead.
- A full lyrics panel from the bar, with the current line highlighted and
  followed as the song plays.
- Lyrics from NetEase Cloud Music by default, with LRCLIB as the fallback.
  Timed lyrics always win over plain ones.
- Lyrics that come without a translation or romanization get them from
  NetEase, found by their words, so a track tagged with an English title still
  gets them.
- Optional Jellyfin support: the lyrics stored with your library are used
  first, and NetEase only adds the translation or romanization, matched on the
  words so that a different recording of the same song still lines up.
- Colours follow the Omarchy theme.
- Playback controls on the bar icon.

## Requirements

- Omarchy 4.0 or newer
- Python 3 (standard library only)
- An MPRIS media player
- Network access to `music.163.com` and `lrclib.net` for tracks that are not
  cached yet

## Install

```bash
omarchy plugin add https://github.com/ryuhzk/omarchy-lyrics --enable
```

`omarchy plugin add` shows what it is about to clone and asks before doing it.
`--enable` puts the icon in the middle of the bar. To move it:

```bash
omarchy bar move io.github.ryuhzk.lyrics --section right --index 0
```

or use **Setup → Plugins → Lyrics**.

The plugin keeps running when its panel is closed, so the desktop lyrics can
follow the music. Restart the shell once after installing:

```bash
omarchy restart shell
```

## Jellyfin (optional)

If you play music from a Jellyfin server, for example through Feishin, the
plugin can read the lyrics stored in your library. Create an API key in the
Jellyfin dashboard (**Dashboard → API Keys**), then:

```bash
mkdir -p ~/.config/omarchy-lyrics
cat > ~/.config/omarchy-lyrics/jellyfin.env <<'EOF'
JELLYFIN_URL=https://jellyfin.example.com
JELLYFIN_API_KEY=your-api-key
EOF
chmod 600 ~/.config/omarchy-lyrics/jellyfin.env
```

The plugin finds the playing track through the server's active sessions, or by
searching the library for its title, artist and length. Without this file,
Jellyfin is skipped.

## Usage

- Left-click the icon: open or close the lyrics panel
- Right-click: skip the cache and fetch the lyrics again
- Middle-click: play or pause
- Scroll up or down: previous or next track
- Escape: close the panel

## Settings

Change them in **Setup → Plugins → Lyrics**, or from the command line:

```bash
omarchy bar set io.github.ryuhzk.lyrics offsetMs 500
```

| Setting               | Default        | Meaning                                                    |
| --------------------- | -------------- | ---------------------------------------------------------- |
| `lyricsSource`        | `NetEase`      | Source asked first: `NetEase` or `LRCLIB`                  |
| `secondaryLyrics`     | `Auto`         | `Auto`, `Translation`, `Romanization` or `Off`             |
| `overlayEnabled`      | `true`         | Show the lyrics over the desktop                           |
| `overlayPosition`     | `Bottom right` | `Bottom right` or `Bottom center`                          |
| `overlayFontSize`     | `30`           | Size of the current line; the others are drawn smaller     |
| `overlayBottomMargin` | `180`          | Distance of the lines from the bottom of the screen        |
| `panelWidth`          | `520`          | Width of the lyrics panel                                  |
| `offsetMs`            | `0`            | Positive values advance the lyrics, negative values delay  |

## Update

```bash
omarchy plugin update io.github.ryuhzk.lyrics
```

The update shows the diff before it applies anything.

## Remove

```bash
omarchy plugin remove io.github.ryuhzk.lyrics
```

It asks first, then takes the icon off the bar, removes its entry from
`~/.config/omarchy/shell.json` and deletes the plugin's folder.

The plugin also keeps a lyrics cache and, if you set it up, the Jellyfin file.
Remove them too if you want nothing left behind:

```bash
rm -rf ~/.cache/omarchy-lyrics ~/.config/omarchy-lyrics
```

## Privacy

To find lyrics, the track's title, artist, album and length are sent to NetEase
Cloud Music and LRCLIB. A line or two of the lyrics may also be sent to NetEase
to find the matching translation. With Jellyfin set up, the same details go to
your own server, together with your API key. Nothing else leaves the machine,
and no file paths are sent.

Results are cached under `${XDG_CACHE_HOME:-~/.cache}/omarchy-lyrics` for 30
days. A track with no lyrics is looked up again after six hours.

## Synchronization

Players that report a working MPRIS position stay exactly in sync, seeking
included. Some browser players claim to report their position but always report
zero. For those, the plugin estimates the time from track changes and
pause/resume. The estimate is right from the next track on, but it cannot know
where you were when the plugin started halfway through a song or after a seek.

## Development

```bash
./check
```

It compiles and tests the Python backend, validates the manifest and runs
`qmllint` when it is available.
