pragma ComponentBehavior: Bound

import QtQuick
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "io.github.ryuhzk.lyrics"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  property var lyricsService: null
  property int panelWidth: 520
  property int offsetMs: 0

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property int activeLineIndex: lyricsService
    ? lyricsService.lineIndexAt(lyricsService.playbackPositionMs + offsetMs) : -1

  function formatTime(milliseconds) {
    var total = Math.max(0, Math.floor((Number(milliseconds) || 0) / 1000))
    var minutes = Math.floor(total / 60)
    var seconds = String(total % 60).padStart(2, "0")
    return minutes + ":" + seconds
  }

  function open() {
    controller.show()
    Qt.callLater(scrollToCurrentLine)
  }

  function close() {
    controller.hide()
  }

  function toggle() {
    opened ? close() : open()
  }

  function switchPanel(direction) {
    if (bar && typeof bar.switchPanelFrom === "function")
      return bar.switchPanelFrom(hostWidget || root, direction)
    return false
  }

  function scrollToCurrentLine() {
    if (!opened || activeLineIndex < 0 || lyricList.moving || lyricList.flicking) return
    lyricList.positionViewAtIndex(activeLineIndex, ListView.Center)
  }

  onActiveLineIndexChanged: Qt.callLater(scrollToCurrentLine)

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.hostWidget || root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(root.panelWidth))
    contentHeight: panel.fittedContentHeight(Style.space(560))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Column {
        anchors.fill: parent
        spacing: Style.space(10)

        Row {
          width: parent.width
          spacing: Style.space(10)

          BorderSurface {
            width: Style.space(58)
            height: Style.space(58)
            radius: Style.spacing.labelGap
            color: Style.normalFillFor(root.foreground, Color.accent)
            borderSpec: Border.controlSpec("normal", root.foreground, Color.accent)

            Image {
              id: artwork
              anchors.fill: parent
              anchors.margins: Style.space(2)
              source: root.lyricsService ? root.lyricsService.artUrl : ""
              fillMode: Image.PreserveAspectCrop
              asynchronous: true
              visible: source !== ""
            }

            Text {
              anchors.centerIn: parent
              visible: !artwork.visible
              text: "󰎈"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.displayLarge
            }
          }

          Column {
            width: parent.width - Style.space(126)
            spacing: Style.space(3)
            anchors.verticalCenter: parent.verticalCenter

            Text {
              width: parent.width
              text: root.lyricsService ? (root.lyricsService.title || "Nothing playing") : "Nothing playing"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.subtitle
              font.bold: true
              elide: Text.ElideRight
            }

            Text {
              width: parent.width
              text: root.lyricsService ? [
                root.lyricsService.artist,
                root.lyricsService.album
              ].filter(function(value) { return String(value || "") !== "" }).join(" · ") : ""
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              elide: Text.ElideRight
            }

            Text {
              width: parent.width
              text: root.lyricsService
                ? root.formatTime(root.lyricsService.playbackPositionMs)
                  + (root.lyricsService.durationSec > 0
                    ? " / " + root.formatTime(root.lyricsService.durationSec * 1000) : "")
                  + " · " + (root.lyricsService.positionMode === "mpris" ? "MPRIS sync" : "estimated sync")
                : ""
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              elide: Text.ElideRight
            }
          }

          Rectangle {
            width: Style.space(48)
            height: Style.space(36)
            radius: Style.space(7)
            color: playArea.containsMouse
              ? Style.hoverFillFor(root.foreground, Color.accent)
              : Style.normalFillFor(root.foreground, Color.accent)
            anchors.verticalCenter: parent.verticalCenter

            Text {
              anchors.centerIn: parent
              text: root.lyricsService && root.lyricsService.isPlaying ? "󰏤" : "󰐊"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.iconLarge
            }

            MouseArea {
              id: playArea
              anchors.fill: parent
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onClicked: if (root.lyricsService) root.lyricsService.runAction("playPause")
            }
          }
        }

        PanelSeparator { foreground: root.foreground }

        Item {
          width: parent.width
          height: parent.height - Style.space(86)

          Text {
            anchors.centerIn: parent
            width: parent.width - Style.space(40)
            visible: !root.lyricsService || root.lyricsService.lyricsStatus !== "ready"
              || (root.lyricsService.lines.length === 0 && root.lyricsService.plainLyrics === "")
            text: {
              if (!root.lyricsService || !root.lyricsService.hasMedia) return "Nothing playing"
              if (root.lyricsService.lyricsStatus === "loading") return "Looking for lyrics…"
              if (root.lyricsService.lyricsStatus === "instrumental") return "Instrumental track"
              if (root.lyricsService.lyricsStatus === "error") return root.lyricsService.errorText
              return "No lyrics found"
            }
            color: root.lyricsService && root.lyricsService.lyricsStatus === "error" ? Color.urgent : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
          }

          ListView {
            id: lyricList
            anchors.fill: parent
            visible: root.lyricsService && root.lyricsService.lines.length > 0
            model: root.lyricsService ? root.lyricsService.lines : []
            currentIndex: root.activeLineIndex
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            spacing: Style.space(4)

            delegate: Rectangle {
              id: lyricRow
              required property int index
              required property var modelData
              width: ListView.view.width
              height: lyricText.implicitHeight + Style.space(14)
              radius: Style.space(7)
              color: index === root.activeLineIndex
                ? Style.selectedFillFor(root.foreground, Color.accent) : "transparent"

              Text {
                id: lyricText
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.leftMargin: Style.space(12)
                anchors.rightMargin: Style.space(12)
                text: String(lyricRow.modelData.text || "")
                color: root.foreground
                opacity: lyricRow.index === root.activeLineIndex ? 1.0 : 0.62
                font.family: root.fontFamily
                font.pixelSize: lyricRow.index === root.activeLineIndex
                  ? Style.font.subtitle : Style.font.body
                font.bold: lyricRow.index === root.activeLineIndex
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
              }
            }
          }

          Flickable {
            anchors.fill: parent
            visible: root.lyricsService && root.lyricsService.lines.length === 0
              && root.lyricsService.plainLyrics !== ""
            contentWidth: width
            contentHeight: plainText.implicitHeight
            clip: true
            boundsBehavior: Flickable.StopAtBounds

            Text {
              id: plainText
              width: parent.width
              text: root.lyricsService ? root.lyricsService.plainLyrics : ""
              color: root.foreground
              opacity: 0.78
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              horizontalAlignment: Text.AlignHCenter
              wrapMode: Text.WordWrap
            }
          }
        }
      }
    }
  }
}
