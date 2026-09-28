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
  // Furigana leaves the romanization to its own line; a syllable over each
  // Chinese character already is the romanization.
  readonly property bool rubyIsKana: lyricsService && index >= 0 && index < lines.length
    ? lines[index].rubyScript === "kana" : false
  property bool shownRubyIsKana: false
  readonly property string besideRubyText: !intro && lyricsService && index >= 0 && index < lines.length
    ? lyricsService.translationFor(lines[index]) : ""

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
      shownRuby = rubyPairs
      shownRubyIsKana = rubyIsKana
      shownBesideRuby = besideRubyText
      shownNext = nextText
      return
    }
    lineChange.restart()
  }
  onNextTextChanged: if (shownCurrent === "") shownNext = nextText
  // A setting switched mid-line takes effect at once, without a transition.
  onSecondaryTextChanged: if (!lineChange.running) shownSecondary = secondaryText
  onRubyPairsChanged: if (!lineChange.running) {
    shownRuby = rubyPairs
    shownRubyIsKana = rubyIsKana
  }
  onBesideRubyTextChanged: if (!lineChange.running) shownBesideRuby = besideRubyText

  SequentialAnimation {
    id: lineChange

    // Out and upwards, the direction the words are travelling anyway.
    ParallelAnimation {
      NumberAnimation { target: lineColumn; property: "opacity"; to: 0; duration: 130; easing.type: Easing.InCubic }
      NumberAnimation { target: lineColumn; property: "y"; to: -10; duration: 130; easing.type: Easing.InCubic }
    }
    ScriptAction {
      script: {
        root.shownCurrent = root.currentText
        root.shownSecondary = root.secondaryText
        root.shownRuby = root.rubyPairs
        root.shownRubyIsKana = root.rubyIsKana
        root.shownBesideRuby = root.besideRubyText
        root.shownNext = root.nextText
        lineColumn.y = 10
      }
    }
    ParallelAnimation {
      NumberAnimation { target: lineColumn; property: "opacity"; to: 1; duration: 260; easing.type: Easing.OutCubic }
      NumberAnimation { target: lineColumn; property: "y"; to: 0; duration: 260; easing.type: Easing.OutCubic }
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
    width: root.atRight ? Math.min(parent.width * 0.42, root.fontSize * 32) : parent.width * 0.9
    height: lineColumn.implicitHeight

    opacity: root.wanted ? 1 : 0
    Behavior on opacity { NumberAnimation { duration: 220; easing.type: Easing.OutCubic } }

    Column {
      id: lineColumn
      width: parent.width
      spacing: Math.round(root.fontSize * 0.18)

      // A soft shadow rather than an outline or a bevel: it separates the
      // words from a light wallpaper and a dark window alike, where a hard
      // black edge reads as a sticker and clogs the thin strokes of Han
      // characters, and a lift in the theme's own background colour looks
      // embossed on anything darker than the theme.
      layer.enabled: true
      layer.effect: MultiEffect {
        shadowEnabled: true
        shadowColor: Qt.rgba(0, 0, 0, 0.55)
        shadowBlur: 0.45
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
            leftPadding: root.shownRubyIsKana ? 0 : Math.round(root.fontSize * 0.06)
            rightPadding: leftPadding

            // Always takes its height, reading or not, so every character in
            // the row stands on the same baseline. (A positioner skips items
            // of zero width, which an empty Text is.)
            Item {
              anchors.horizontalCenter: parent.horizontalCenter
              width: Math.max(1, reading.implicitWidth)
              height: Math.round(root.fontSize * 0.5)

              Text {
                id: reading
                anchors.horizontalCenter: parent.horizontalCenter
                anchors.bottom: parent.bottom
                text: pair.modelData[1]
                color: Qt.alpha(Color.accent, 0.9)
                font.family: root.readingFamily
                font.pixelSize: Math.round(root.fontSize * 0.4)
              }
            }

            Text {
              anchors.horizontalCenter: parent.horizontalCenter
              text: pair.modelData[0]
              color: Color.accent
              font.family: root.familyFor(root.shownCurrent)
              font.pixelSize: root.fontSize
              font.weight: Font.Bold
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
        text: root.rubyShown && !root.shownRubyIsKana ? "" : root.shownSecondary
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
        color: Qt.alpha(Color.accent, 0.72)
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
        color: Qt.alpha(Color.accent, 0.45)
        font.family: root.familyFor(root.shownNext)
        font.pixelSize: Math.round(root.fontSize * 0.62)
        font.weight: Font.Medium
        wrapMode: Text.WordWrap
        visible: text !== ""
      }
    }
  }
}
