pragma ComponentBehavior: Bound

import QtQuick
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
  property int bottomMargin: 96

  readonly property var lines: lyricsService ? lyricsService.lines : []
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

  readonly property string currentText: lineAt(index)
  readonly property string nextText: lineAt(index + 1)

  // Shown only while something is actually playing and actually has lyrics.
  // A paused track keeps its lyrics loaded, so `isPlaying` is what separates
  // "singing along" from "left the music open".
  readonly property bool wanted: lyricsService
    && lyricsService.isPlaying
    && lines.length > 0
    && currentText !== ""

  // The window itself stays mapped while fading, or the fade would have
  // nothing to draw; `visible` follows the opacity down to zero.
  visible: wanted || content.opacity > 0

  anchors { bottom: true; left: true; right: true }
  implicitHeight: content.implicitHeight + bottomMargin

  color: "transparent"
  WlrLayershell.namespace: "omarchy-lyrics-overlay"
  WlrLayershell.layer: WlrLayer.Overlay
  WlrLayershell.keyboardFocus: WlrKeyboardFocus.None

  // Nothing here is clickable, so nothing here may take a click. Without both
  // of these the overlay would sit over the whole width of the desktop and
  // swallow every press aimed at the window underneath it.
  exclusionMode: ExclusionMode.Ignore
  mask: Region {}

  Column {
    id: content

    anchors.horizontalCenter: parent.horizontalCenter
    anchors.bottom: parent.bottom
    anchors.bottomMargin: root.bottomMargin
    width: parent.width * 0.9
    spacing: Math.round(root.fontSize * 0.35)

    opacity: root.wanted ? 1 : 0
    Behavior on opacity { NumberAnimation { duration: 220; easing.type: Easing.OutCubic } }

    Text {
      width: parent.width
      horizontalAlignment: Text.AlignHCenter
      text: root.currentText
      color: Color.foreground
      font.family: Style.font.family
      font.pixelSize: root.fontSize
      font.bold: true
      wrapMode: Text.WordWrap
      // Drawn over whatever the wallpaper happens to be, so it carries its own
      // contrast rather than trusting the background to be dark.
      style: Text.Outline
      styleColor: Qt.rgba(0, 0, 0, 0.75)
    }

    Text {
      width: parent.width
      horizontalAlignment: Text.AlignHCenter
      text: root.nextText
      color: Qt.darker(Color.foreground, 1.7)
      font.family: Style.font.family
      font.pixelSize: Math.round(root.fontSize * 0.72)
      wrapMode: Text.WordWrap
      visible: text !== ""
      style: Text.Outline
      styleColor: Qt.rgba(0, 0, 0, 0.6)
    }
  }
}
