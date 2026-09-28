import QtQuick
import Quickshell
import qs.Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "io.github.ryuhzk.lyrics"

  readonly property var lyricsService: bar && bar.shell
    ? bar.shell.serviceFor(moduleName) : null
  readonly property int panelWidth: boundedInt(setting("panelWidth", 520), 360, 900)
  readonly property int offsetMs: boundedInt(setting("offsetMs", 0), -10000, 10000)
  readonly property bool overlayEnabled: setting("overlayEnabled", true) === true
  readonly property int overlayFontSize: boundedInt(setting("overlayFontSize", 30), 14, 72)
  readonly property int overlayBottomMargin: boundedInt(setting("overlayBottomMargin", 180), 0, 600)
  readonly property string overlayPosition: setting("overlayPosition", "Bottom right") === "Bottom center" ? "center" : "right"
  readonly property string lyricsSource: setting("lyricsSource", "NetEase") === "LRCLIB" ? "lrclib" : "netease"
  readonly property string secondaryMode: {
    var chosen = String(setting("secondaryLyrics", "Auto"))
    if (chosen === "Translation") return "translation"
    if (chosen === "Romanization") return "romanization"
    if (chosen === "Off") return "off"
    return "auto"
  }
  // The lyric line itself is not drawn here any more. It lived in the bar as
  // scrolling text whose width followed the words, so every line change - four
  // times a second while a track plays - resized this widget and relaid out
  // every other item in the bar, and a marquee animation ran on top of that.
  // The desktop overlay is where the words belong; the bar keeps a fixed-width
  // glyph that says whether something is playing and opens the panel.
  readonly property string trackLabel: lyricsService
    ? [lyricsService.title, lyricsService.artist].filter(function(value) {
        return String(value || "") !== ""
      }).join(" · ") : ""
  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false
  readonly property bool popoutSwitchClosing: panelLoader.item
    ? panelLoader.item.popoutSwitchClosing === true : false

  // The overlay is owned by the service, which stays loaded whether or not
  // this panel is open, so its settings are pushed rather than read there.
  Binding { target: root.lyricsService; property: "overlayEnabled"; value: root.overlayEnabled; when: root.lyricsService !== null }
  Binding { target: root.lyricsService; property: "overlayFontSize"; value: root.overlayFontSize; when: root.lyricsService !== null }
  Binding { target: root.lyricsService; property: "overlayBottomMargin"; value: root.overlayBottomMargin; when: root.lyricsService !== null }
  Binding { target: root.lyricsService; property: "overlayPosition"; value: root.overlayPosition; when: root.lyricsService !== null }
  Binding { target: root.lyricsService; property: "overlayOffsetMs"; value: root.offsetMs; when: root.lyricsService !== null }
  Binding { target: root.lyricsService; property: "lyricsSource"; value: root.lyricsSource; when: root.lyricsService !== null }
  Binding { target: root.lyricsService; property: "secondaryMode"; value: root.secondaryMode; when: root.lyricsService !== null }

  function boundedInt(value, minimum, maximum) {
    var parsed = parseInt(String(value), 10)
    if (!isFinite(parsed)) parsed = minimum
    return Math.max(minimum, Math.min(maximum, parsed))
  }

  function open() {
    if (panelLoader.item) panelLoader.item.open()
  }

  function close() {
    if (panelLoader.item) panelLoader.item.close()
  }

  function toggle() {
    if (panelLoader.item) panelLoader.item.toggle()
  }

  function closeForPopoutSwitch() {
    if (panelLoader.item) panelLoader.item.closeForPopoutSwitch()
  }

  function injectPanel() {
    if (!panelLoader.item) return
    panelLoader.item.bar = root.bar
    panelLoader.item.anchorItem = root
    panelLoader.item.hostWidget = root
    panelLoader.item.lyricsService = root.lyricsService
    panelLoader.item.panelWidth = root.panelWidth
    panelLoader.item.offsetMs = root.offsetMs
  }

  visible: lyricsService ? lyricsService.hasMedia : false
  implicitWidth: visible ? row.implicitWidth + Style.space(14) : 0
  implicitHeight: barSize

  onBarChanged: injectPanel()
  onLyricsServiceChanged: injectPanel()
  onPanelWidthChanged: injectPanel()
  onOffsetMsChanged: injectPanel()

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  Row {
    id: row
    anchors.centerIn: parent
    spacing: Style.space(6)

    Text {
      id: glyph
      anchors.verticalCenter: parent.verticalCenter
      text: root.lyricsService && root.lyricsService.isPlaying ? "󰎈" : "󰏤"
      color: root.lyricsService && root.lyricsService.isPlaying
        ? (root.bar ? root.bar.barForeground : Color.foreground)
        : Qt.darker(root.bar ? root.bar.barForeground : Color.foreground, 1.5)
      font.family: root.bar ? root.bar.fontFamily : Style.font.family
      font.pixelSize: Style.font.body
    }

  }

  MouseArea {
    anchors.fill: parent
    hoverEnabled: true
    cursorShape: Qt.PointingHandCursor
    acceptedButtons: Qt.LeftButton | Qt.RightButton | Qt.MiddleButton

    onClicked: function(mouse) {
      if (!root.lyricsService) return
      if (mouse.button === Qt.MiddleButton) root.lyricsService.runAction("playPause")
      else if (mouse.button === Qt.RightButton) root.lyricsService.requestLyrics(true)
      else root.toggle()
    }
    onWheel: function(wheel) {
      if (!root.lyricsService) return
      if (wheel.angleDelta.y > 0) root.lyricsService.runAction("previous")
      else if (wheel.angleDelta.y < 0) root.lyricsService.runAction("next")
    }
    onEntered: if (root.bar) root.bar.showTooltip(root, root.trackLabel)
    onExited: if (root.bar) root.bar.hideTooltip(root)
  }
}
