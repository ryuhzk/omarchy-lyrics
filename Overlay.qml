pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Effects
import Quickshell
import Quickshell.Wayland
import qs.Commons

// A desktop lyric line, for singing along to.
//
// This is deliberately not the panel. The panel is a scrolling list you open,
// read and dismiss; this is two lines pinned above the wallpaper that nobody
// opens and nobody closes - it appears while a track with lyrics is playing
// and goes away when it stops.
//
// Two lines rather than one: the line being sung is only half of what a singer
// needs, because by the time you have read it you are already late for the
// next one. The upcoming line is dimmed so it reads as a preview rather than
// as a second thing to sing.
//
// Line-level timing is all there is. The lyrics come from LRC files whose
// timestamps mark whole lines - a check across the user's own library found
// 2448 of them and not one carrying per-word tags - so the whole line
// brightens at once, the way every LRC player does it. Sweeping a highlight
// through the characters would mean inventing timings that are not in the
// data, and it would drift on any held note.
PanelWindow {
  id: root

  required property var lyricsService
  property int fontSize: 30
  property int bottomMargin: 180
  // "right" pins the lines to the bottom-right corner, right-aligned, out of the
  // way of whatever is centred on screen; "center" is the classic karaoke line.
  property string position: "right"
  readonly property bool atRight: position !== "center"

  readonly property var lines: lyricsService ? lyricsService.lines : []

  // CJK text in a CJK face rather than the monospace UI font: Japanese in the
  // Japanese one, so its kanji take their Japanese shapes, and the rest in the
  // Chinese one. Readings and romanization share the Japanese face, whose
  // Latin is proportional.
  readonly property string readingFamily: "Noto Sans CJK JP"

  // Karaoke colours. What is still to be sung is the theme's own text colour,
  // dark on a light theme and light on a dark one, so it reads without
  // leaning on a heavy shadow; what has been sung takes the theme's accent as
  // a glossy gradient, lighter at the top.
  readonly property color unsungColor: Qt.alpha(Color.foreground, 0.9)
  readonly property color sungTop: Qt.lighter(Color.accent, 1.6)
  readonly property color sungBottom: Color.accent
  readonly property color sungReading: Qt.lighter(Color.accent, 1.35)
  readonly property color glowColor: Qt.lighter(Color.accent, 1.25)
  readonly property color quietColor: Qt.alpha(Color.foreground, 0.72)
  function familyFor(text) {
    return /[\u3040-\u30ff]/.test(String(text || "")) ? "Noto Sans CJK JP" : "Noto Sans CJK SC"
  }
  // Resolved through the same call and the same offset the bar widget uses, so
  // the two surfaces cannot disagree about which line is current.
  readonly property int index: lyricsService
    ? lyricsService.lineIndexAt(lyricsService.playbackPositionMs
                                + lyricsService.overlayOffsetMs)
    : -1

  function lineAt(position) {
    if (position < 0 || position >= lines.length) return ""
    return String(lines[position].text || "")
  }

  // Before the first line - the intro, or while the lyrics are still being
  // looked up - the title stands where the lyric will be and the artist under
  // it, so the corner says what is playing instead of staying blank. A track
  // that turned out to have no lyrics does not keep its title up for the
  // whole song.
  readonly property bool intro: lyricsService !== null
    && lyricsService.hasMedia
    && index < 0
    && (lines.length > 0 || lyricsService.lyricsStatus === "loading")

  readonly property string currentText: intro ? lyricsService.title : lineAt(index)
  readonly property string nextText: lineAt(index + 1)
  // Under the line being sung: its romanization (unless it already sits over
  // the characters), then its translation. The intro puts the artist there.
  readonly property string secondaryText: intro ? lyricsService.artist
    : lyricsService && index >= 0 && index < lines.length
      ? lyricsService.romanizationFor(lines[index]) : ""
  // Each syllable of the romanization over the characters it spells, when the
  // backend could pair them; then only the translation is left for underneath.
  readonly property var rubyPairs: lyricsService && index >= 0 && index < lines.length
    ? lyricsService.rubyFor(lines[index]) : []
  // Japanese pairs carry romaji as a third part, set under each character.
  // Read from what is on screen rather than from the line index, which can
  // move on a moment before or after the pairs themselves.
  readonly property bool shownRubyIsKana: shownRuby.some(function(pair) { return String(pair[2] || "") !== "" })
  readonly property string besideRubyText: !intro && lyricsService && index >= 0 && index < lines.length
    ? lyricsService.translationFor(lines[index]) : ""

  // Karaoke: when each character of the line has a time, a brighter colour
  // sweeps across them as they are sung. A line with no readings to set over
  // it still needs a column per character for that, without the readings.
  readonly property var karaoke: !intro && index >= 0 && index < lines.length
    && Array.isArray(lines[index].karaoke) ? lines[index].karaoke : []
  readonly property int lineAtMs: !intro && index >= 0 && index < lines.length ? Number(lines[index].atMs) || 0 : 0
  readonly property var displayPairs: rubyPairs.length > 0 ? rubyPairs
    : karaoke.length > 0 ? String(currentText).split("").map(function(character) { return [character, "", ""] })
    : []
  property var shownKaraoke: []
  property int shownLineAtMs: 0
  // Where each pair starts in the line's characters, and when it is sung:
  // [first character, start ms, end ms], relative to the line.
  readonly property var shownSpans: {
    var spans = []
    var offset = 0
    for (var i = 0; i < shownRuby.length; i++) {
      var length = String(shownRuby[i][0]).length
      var start = -1
      var end = -1
      for (var c = offset; c < offset + length && c < shownKaraoke.length; c++) {
        var times = shownKaraoke[c]
        if (times[1] <= 0) continue
        if (start < 0) start = times[0]
        end = Math.max(end, times[0] + times[1])
      }
      spans.push([offset, start, end])
      offset += length
    }
    return spans
  }
  readonly property bool shownHasReadings: shownRuby.some(function(pair) { return pair[1] !== "" || pair[2] !== "" })
  readonly property bool sweeping: shownKaraoke.length > 0 && rubyShown

  // The position inside the line, advanced every frame between the player's
  // quarter-second updates so the sweep glides instead of stepping.
  property real clockMs: 0
  property real sampledPositionMs: 0
  property real sampledAtMs: 0
  Connections {
    target: root.lyricsService
    function onPlaybackPositionMsChanged() {
      root.sampledPositionMs = root.lyricsService.playbackPositionMs + root.lyricsService.overlayOffsetMs
      root.sampledAtMs = Date.now()
    }
  }
  FrameAnimation {
    running: root.sweeping && root.visible
    onTriggered: {
      var playing = root.lyricsService && root.lyricsService.isPlaying
      var now = root.sampledPositionMs + (playing ? Math.min(1000, Date.now() - root.sampledAtMs) : 0)
      root.clockMs = now - root.shownLineAtMs
    }
  }

  // How far the sweep has got through pair `number`: 0 before, 1 after.
  // Keyed on the times alone, not on whether the row is laid out yet: for the
  // moment before it is, the whole line would otherwise flash as sung.
  function sungFraction(number) {
    if (shownKaraoke.length === 0 || number >= shownSpans.length) return 1
    var span = shownSpans[number]
    if (span[1] < 0) return clockMs >= 0 ? 1 : 0
    if (span[2] <= span[1]) return clockMs >= span[1] ? 1 : 0
    return Math.max(0, Math.min(1, (clockMs - span[1]) / (span[2] - span[1])))
  }

  // Shown only while something is actually playing and actually has lyrics.
  // A paused track keeps its lyrics loaded, so `isPlaying` is what separates
  // "singing along" from "left the music open".
  readonly property bool wanted: lyricsService
    && lyricsService.isPlaying
    && (lines.length > 0 || intro)
    && currentText !== ""

  // The window itself stays mapped while fading, or the fade would have
  // nothing to draw; `visible` follows the opacity down to zero.
  visible: wanted || content.opacity > 0

  anchors { bottom: true; left: true; right: true }
  implicitHeight: lineColumn.implicitHeight + bottomMargin

  // What is on screen, which lags the source by one transition. Rendering
  // `currentText` directly would swap the words instantly in the middle of the
  // fade, so the line appears to change twice.
  property string shownCurrent: ""
  property string shownSecondary: ""
  property var shownRuby: []
  property string shownBesideRuby: ""
  // A line whose readings would not fit on one row is drawn plainly, with the
  // romanization underneath as before.
  readonly property bool rubyShown: shownRuby.length > 0 && rubyRow.implicitWidth <= lineColumn.width
  property string shownNext: ""

  onCurrentTextChanged: {
    // The first line of a track has nothing to cross-fade from, and neither
    // does a track whose lyrics just finished loading; those appear with the
    // window's own fade instead of a transition that starts from blank.
    if (shownCurrent === "" || currentText === "") {
      shownCurrent = currentText
      shownSecondary = secondaryText
      shownRuby = displayPairs
      shownKaraoke = karaoke
      shownLineAtMs = lineAtMs
      shownBesideRuby = besideRubyText
      shownNext = nextText
      return
    }
    lineChange.restart()
  }
  onNextTextChanged: if (shownCurrent === "") shownNext = nextText
  // A setting switched mid-line takes effect at once, without a transition.
  onSecondaryTextChanged: if (!lineChange.running) shownSecondary = secondaryText
  onDisplayPairsChanged: if (!lineChange.running) {
    shownRuby = displayPairs
    shownKaraoke = karaoke
    shownLineAtMs = lineAtMs
  }
  onBesideRubyTextChanged: if (!lineChange.running) shownBesideRuby = besideRubyText

  SequentialAnimation {
    id: lineChange

    // Out and upwards, the direction the words are travelling anyway,
    // shrinking a little as it goes.
    ParallelAnimation {
      NumberAnimation { target: lineColumn; property: "opacity"; to: 0; duration: 160; easing.type: Easing.InCubic }
      NumberAnimation { target: lineColumn; property: "y"; to: -root.fontSize * 0.6; duration: 160; easing.type: Easing.InCubic }
      NumberAnimation { target: lineColumn; property: "scale"; to: 0.96; duration: 160; easing.type: Easing.InCubic }
    }
    ScriptAction {
      script: {
        root.shownCurrent = root.currentText
        root.shownSecondary = root.secondaryText
        root.shownRuby = root.displayPairs
        root.shownKaraoke = root.karaoke
        root.shownLineAtMs = root.lineAtMs
        root.clockMs = 0
        root.shownBesideRuby = root.besideRubyText
        root.shownNext = root.nextText
        lineColumn.y = root.fontSize * 0.5
        lineColumn.scale = 1
      }
    }
    // In from below with a little overshoot; the characters themselves
    // drop in one after another (see the pairs below).
    ParallelAnimation {
      NumberAnimation { target: lineColumn; property: "opacity"; to: 1; duration: 220; easing.type: Easing.OutCubic }
      NumberAnimation { target: lineColumn; property: "y"; to: 0; duration: 420; easing.type: Easing.OutBack; easing.overshoot: 1.6 }
    }
  }

  color: "transparent"
  WlrLayershell.namespace: "omarchy-lyrics-overlay"
  WlrLayershell.layer: WlrLayer.Overlay
  WlrLayershell.keyboardFocus: WlrKeyboardFocus.None

  // Nothing here is clickable, so nothing here may take a click. Without both
  // of these the overlay would sit over the whole width of the desktop and
  // swallow every press aimed at the window underneath it.
  exclusionMode: ExclusionMode.Ignore
  mask: Region {}

  // Two layers of fade, deliberately separate. The outer one is the overlay
  // arriving and leaving with playback; the inner one is a line handing over to
  // the next. Binding both to the same opacity would make a line change during
  // the appear animation cancel it half-drawn.
  Item {
    id: content

    anchors.horizontalCenter: root.atRight ? undefined : parent.horizontalCenter
    anchors.right: root.atRight ? parent.right : undefined
    anchors.rightMargin: root.atRight ? Math.round(root.fontSize * 1.6) : 0
    anchors.bottom: parent.bottom
    anchors.bottomMargin: root.bottomMargin
    width: root.atRight ? Math.min(parent.width * 0.6, root.fontSize * 48) : parent.width * 0.9
    height: lineColumn.implicitHeight

    opacity: root.wanted ? 1 : 0
    Behavior on opacity { NumberAnimation { duration: 220; easing.type: Easing.OutCubic } }

    Column {
      id: lineColumn
      width: parent.width
      transformOrigin: root.atRight ? Item.BottomRight : Item.Bottom
      spacing: Math.round(root.fontSize * 0.18)

      // A soft shadow rather than an outline or a bevel: it separates the
      // words from a light wallpaper and a dark window alike, where a hard
      // black edge reads as a sticker and clogs the thin strokes of Han
      // characters, and a lift in the theme's own background colour looks
      // embossed on anything darker than the theme.
      layer.enabled: true
      layer.effect: MultiEffect {
        shadowEnabled: true
        shadowColor: Qt.rgba(0, 0, 0, 0.18)
        shadowBlur: 0.2
        shadowVerticalOffset: 1
        shadowHorizontalOffset: 0
        blurMax: 16
      }

      Row {
        id: rubyRow

        x: root.atRight ? parent.width - width : Math.round((parent.width - width) / 2)
        bottomPadding: Math.round(root.fontSize * 0.08)
        visible: root.rubyShown

        Repeater {
          model: root.shownRuby

          Column {
            id: pair

            required property var modelData
            required property int index
            // 0 -> 1 as this part of the line is sung; 1 throughout when the
            // line has no character times, so it simply lights up whole.
            readonly property real sung: root.sungFraction(index)
            readonly property bool singing: root.sweeping && sung > 0 && sung < 1
            leftPadding: root.shownHasReadings ? Math.round(root.fontSize * 0.06) : 0
            rightPadding: leftPadding

            // Drops in a beat after the pair before it when the line arrives.
            property real entered: 0
            opacity: entered
            transform: Translate { y: (1 - pair.entered) * root.fontSize * 0.45 }
            SequentialAnimation on entered {
              running: true
              PauseAnimation { duration: Math.min(pair.index, 24) * 18 }
              NumberAnimation { from: 0; to: 1; duration: 360; easing.type: Easing.OutBack; easing.overshoot: 2 }
            }

            // Always takes its height, reading or not, so every character in
            // the row stands on the same baseline. (A positioner skips items
            // of zero width, which an empty Text is.)
            Item {
              anchors.horizontalCenter: parent.horizontalCenter
              visible: root.shownHasReadings
              width: Math.max(1, reading.implicitWidth)
              height: Math.round(root.fontSize * 0.5)

              Text {
                id: reading
                anchors.horizontalCenter: parent.horizontalCenter
                anchors.bottom: parent.bottom
                text: pair.modelData[1]
                color: pair.sung > 0 ? root.sungReading : Qt.alpha(root.unsungColor, 0.8)
                Behavior on color { ColorAnimation { duration: 220 } }
                font.family: root.readingFamily
                font.pixelSize: Math.round(root.fontSize * 0.4)
              }
            }

            // The character twice: dim underneath, bright on top, the bright
            // one revealed left to right as it is sung. The character being
            // sung swells a little and springs back when it is done.
            Item {
              id: glyph
              anchors.horizontalCenter: parent.horizontalCenter
              width: base.implicitWidth
              height: base.implicitHeight
              scale: pair.singing ? 1.16 : 1
              Behavior on scale { SpringAnimation { spring: 4; damping: 0.26; epsilon: 0.002 } }
              transformOrigin: Item.Bottom

              // A glow on the character being sung only; a trail of glowing
              // characters behind it blurs the whole line.
              layer.enabled: pair.singing
              layer.effect: MultiEffect {
                shadowEnabled: true
                shadowColor: root.glowColor
                shadowBlur: 0.7
                shadowOpacity: 0.6
                shadowHorizontalOffset: 0
                shadowVerticalOffset: 0
                blurMax: 28
                Behavior on shadowOpacity { NumberAnimation { duration: 260 } }
              }

              // Still to be sung.
              Text {
                id: base
                text: pair.modelData[0]
                color: root.sweeping ? root.unsungColor : "transparent"
                font.family: root.familyFor(root.shownCurrent)
                font.pixelSize: root.fontSize
                font.weight: Font.Bold
              }

              // Sung: the accent gradient in the character's shape, revealed
              // left to right. A line without character times is sung whole.
              Item {
                width: base.width * pair.sung
                height: base.height
                clip: true

                Rectangle {
                  id: gloss
                  width: base.width
                  height: base.height
                  visible: false
                  layer.enabled: true
                  gradient: Gradient {
                    GradientStop { position: 0.15; color: root.sungTop }
                    GradientStop { position: 0.85; color: root.sungBottom }
                  }
                }

                Text {
                  id: shape
                  text: pair.modelData[0]
                  font: base.font
                  visible: false
                  layer.enabled: true
                }

                MultiEffect {
                  anchors.fill: gloss
                  source: gloss
                  maskEnabled: true
                  maskSource: shape
                }
              }
            }

            // Japanese: the romaji of this part right under it.
            Item {
              anchors.horizontalCenter: parent.horizontalCenter
              visible: root.shownRubyIsKana
              width: Math.max(1, romaji.implicitWidth)
              height: romaji.implicitHeight

              Text {
                id: romaji
                anchors.horizontalCenter: parent.horizontalCenter
                text: pair.modelData[2] || ""
                color: pair.sung > 0 ? root.sungReading : Qt.alpha(root.unsungColor, 0.8)
                Behavior on color { ColorAnimation { duration: 220 } }
                font.family: root.readingFamily
                font.pixelSize: Math.round(root.fontSize * 0.42)
                font.weight: Font.Medium
              }
            }
          }
        }
      }

      Text {
        width: parent.width
        horizontalAlignment: root.atRight ? Text.AlignRight : Text.AlignHCenter
        text: root.shownCurrent
        visible: !root.rubyShown
        // The line being sung wears the theme's accent, the same colour the
        // panel fills its current row with, so switching Omarchy themes moves
        // this with everything else rather than leaving one white line behind.
        color: Color.accent
        Behavior on color { ColorAnimation { duration: 160 } }
        font.family: root.familyFor(root.shownCurrent)
        font.pixelSize: root.fontSize
        font.weight: Font.Bold
        font.letterSpacing: root.fontSize * 0.02
        wrapMode: Text.WordWrap
      }

      // The reading to sing: the romanization, unless it already sits over
      // the characters.
      Text {
        width: parent.width
        horizontalAlignment: root.atRight ? Text.AlignRight : Text.AlignHCenter
        text: root.rubyShown && root.shownHasReadings ? "" : root.shownSecondary
        color: Qt.alpha(Color.accent, 0.85)
        font.family: root.readingFamily
        font.pixelSize: Math.round(root.fontSize * 0.5)
        font.weight: Font.Medium
        font.wordSpacing: root.fontSize * 0.08
        wrapMode: Text.WordWrap
        visible: text !== ""
      }

      // The meaning, a step quieter than the reading above it.
      Text {
        width: parent.width
        horizontalAlignment: root.atRight ? Text.AlignRight : Text.AlignHCenter
        text: root.shownBesideRuby
        color: root.quietColor
        font.family: root.familyFor(root.shownBesideRuby)
        font.pixelSize: Math.round(root.fontSize * 0.52)
        wrapMode: Text.WordWrap
        visible: text !== ""
      }

      // What comes next, dimmed so it reads as a preview.
      Text {
        width: parent.width
        horizontalAlignment: root.atRight ? Text.AlignRight : Text.AlignHCenter
        text: root.shownNext
        topPadding: Math.round(root.fontSize * 0.22)
        color: Qt.alpha(root.unsungColor, 0.5)
        font.family: root.familyFor(root.shownNext)
        font.pixelSize: Math.round(root.fontSize * 0.62)
        font.weight: Font.Medium
        wrapMode: Text.WordWrap
        visible: text !== ""
      }
    }
  }
}
