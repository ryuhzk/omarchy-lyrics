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
    resetSearchText()
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

  readonly property bool searching: lyricsService !== null && lyricsService.searchStatus !== "idle"

  // The search starts from what the player says is playing, so a wrong match
  // usually needs only a tweak (the Chinese title for a romanized one) rather
  // than typing it all out. Left alone while the box is being edited.
  readonly property string defaultSearch: lyricsService
    ? [lyricsService.title, lyricsService.artist].filter(function(value) {
        return String(value || "").trim() !== ""
      }).join(" ") : ""

  function resetSearchText() {
    if (!searchField.activeFocus) searchField.text = defaultSearch
  }

  onDefaultSearchChanged: resetSearchText()

  function runSearch() {
    if (lyricsService) lyricsService.searchSongs(searchField.text)
  }

  // A transport button in the header: the same fill and hover as the rest of
  // the panel's controls, with a glyph from the icon font.
  component ControlButton: Rectangle {
    id: control

    property string glyph: ""
    property bool emphasized: false
    signal clicked()

    width: Style.space(emphasized ? 44 : 36)
    height: Style.space(36)
    radius: Style.space(7)
    color: controlArea.containsMouse
      ? Style.hoverFillFor(root.foreground, Color.accent)
      : (emphasized ? Style.normalFillFor(root.foreground, Color.accent) : "transparent")
    enabled: root.lyricsService !== null && root.lyricsService.hasMedia
    opacity: enabled ? 1 : 0.4

    Text {
      anchors.centerIn: parent
      text: control.glyph
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: control.emphasized ? Style.font.iconLarge : Style.font.icon
    }

    MouseArea {
      id: controlArea
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: control.clicked()
    }
  }

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
      // Typing in the search box must reach the box, not the panel's own keys.
      blocked: searchField.activeFocus
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
            width: parent.width - Style.space(58) - transport.width - Style.space(20)
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

          Row {
            id: transport
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(2)

            ControlButton {
              glyph: "󰒮"
              onClicked: root.lyricsService.runAction("previous")
            }

            ControlButton {
              glyph: root.lyricsService && root.lyricsService.isPlaying ? "󰏤" : "󰐊"
              emphasized: true
              onClicked: root.lyricsService.runAction("playPause")
            }

            ControlButton {
              glyph: "󰒭"
              onClicked: root.lyricsService.runAction("next")
            }
          }
        }

        // Finding the right song by hand, for a track whose name finds the
        // wrong one: search NetEase, pick a result, and it is remembered.
        Row {
          width: parent.width
          spacing: Style.space(8)

          TextField {
            id: searchField
            width: parent.width - searchButton.width - parent.spacing
            anchors.verticalCenter: parent.verticalCenter
            placeholderText: "Wrong lyrics? Search NetEase by song or a line"
            Component.onCompleted: text = root.defaultSearch
            // Everything selected on focus, so typing replaces the suggestion.
            onActiveFocusChanged: if (activeFocus) selectAll()
            foreground: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            onAccepted: root.runSearch()
            Keys.onEscapePressed: {
              if (root.searching) root.lyricsService.closeSearch()
              else keyCatcher.forceActiveFocus()
            }
          }

          Button {
            id: searchButton
            anchors.verticalCenter: parent.verticalCenter
            height: searchField.height
            text: "Search"
            iconText: "󰍉"
            bordered: true
            foreground: root.foreground
            fontFamily: root.fontFamily
            fontSize: Style.font.bodySmall
            enabled: searchField.text.trim() !== ""
            opacity: enabled ? 1 : 0.5
            onClicked: root.runSearch()
          }
        }

        Row {
          width: parent.width
          spacing: Style.space(6)
          visible: root.lyricsService !== null && root.lyricsService.chosenId > 0 && !root.searching

          Text {
            anchors.verticalCenter: parent.verticalCenter
            text: "󰄬  Using the NetEase song you picked for this track"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          Text {
            anchors.verticalCenter: parent.verticalCenter
            text: "Undo"
            color: undoArea.containsMouse ? Color.accent : root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            font.underline: true

            MouseArea {
              id: undoArea
              anchors.fill: parent
              anchors.margins: -Style.space(4)
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onClicked: root.lyricsService.chooseSong(0)
            }
          }
        }

        PanelSeparator { foreground: root.foreground }

        Item {
          width: parent.width
          height: parent.height - y

          Column {
            anchors.fill: parent
            spacing: Style.space(8)
            visible: root.searching

            Row {
              width: parent.width

              Text {
                width: parent.width - backButton.width
                anchors.verticalCenter: parent.verticalCenter
                text: root.lyricsService ? "Results for “" + root.lyricsService.searchKeyword + "”" : ""
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
              }

              Button {
                id: backButton
                text: "Back to lyrics"
                foreground: root.foreground
                fontFamily: root.fontFamily
                fontSize: Style.font.caption
                onClicked: root.lyricsService.closeSearch()
              }
            }

            Item {
              width: parent.width
              height: parent.height - y

              Text {
                anchors.centerIn: parent
                width: parent.width - Style.space(40)
                visible: resultList.count === 0
                text: {
                  if (!root.lyricsService) return ""
                  if (root.lyricsService.searchStatus === "searching") return "Searching…"
                  if (root.lyricsService.searchStatus === "error") return root.lyricsService.searchError
                  return "No songs found. Try the title in Chinese, or a line of the lyrics."
                }
                color: root.lyricsService && root.lyricsService.searchStatus === "error" ? Color.urgent : root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
              }

              ListView {
                id: resultList
                anchors.fill: parent
                model: root.lyricsService ? root.lyricsService.searchResults : []
                clip: true
                boundsBehavior: Flickable.StopAtBounds
                spacing: Style.space(2)

                delegate: Rectangle {
                  id: resultRow
                  required property var modelData
                  readonly property bool current: root.lyricsService !== null
                    && root.lyricsService.chosenId === modelData.id
                  width: ListView.view.width
                  height: resultTitle.implicitHeight + resultDetail.implicitHeight + Style.space(14)
                  radius: Style.space(7)
                  color: resultArea.containsMouse
                    ? Style.hoverFillFor(root.foreground, Color.accent)
                    : (current ? Style.selectedFillFor(root.foreground, Color.accent) : "transparent")

                  Text {
                    id: resultTitle
                    anchors.left: parent.left
                    anchors.right: pickLabel.left
                    anchors.top: parent.top
                    anchors.topMargin: Style.space(7)
                    anchors.leftMargin: Style.space(12)
                    anchors.rightMargin: Style.space(8)
                    text: resultRow.modelData.title
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.body
                    font.bold: true
                    elide: Text.ElideRight
                  }

                  Text {
                    id: resultDetail
                    anchors.left: resultTitle.left
                    anchors.right: resultTitle.right
                    anchors.top: resultTitle.bottom
                    anchors.topMargin: Style.space(2)
                    text: [resultRow.modelData.artist, resultRow.modelData.album,
                           resultRow.modelData.duration > 0 ? root.formatTime(resultRow.modelData.duration * 1000) : ""]
                      .filter(function(value) { return value !== "" }).join(" · ")
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    elide: Text.ElideRight
                  }

                  Text {
                    id: pickLabel
                    anchors.right: parent.right
                    anchors.rightMargin: Style.space(12)
                    anchors.verticalCenter: parent.verticalCenter
                    text: resultRow.current ? "󰄬 In use" : "Use"
                    color: resultArea.containsMouse || resultRow.current ? Color.accent : root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                  }

                  MouseArea {
                    id: resultArea
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.lyricsService.chooseSong(resultRow.modelData.id)
                  }
                }
              }
            }
          }

          Item {
            anchors.fill: parent
            visible: !root.searching

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
                readonly property string secondary: root.lyricsService
                  ? root.lyricsService.secondaryFor(modelData) : ""
                height: lyricText.implicitHeight + (secondaryText.visible
                  ? secondaryText.implicitHeight + Style.space(2) : 0) + Style.space(14)
                radius: Style.space(7)
                color: index === root.activeLineIndex
                  ? Style.selectedFillFor(root.foreground, Color.accent) : "transparent"

                Text {
                  id: lyricText
                  anchors.left: parent.left
                  anchors.right: parent.right
                  anchors.top: parent.top
                  anchors.topMargin: Style.space(7)
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

                Text {
                  id: secondaryText
                  anchors.left: parent.left
                  anchors.right: parent.right
                  anchors.top: lyricText.bottom
                  anchors.topMargin: Style.space(2)
                  anchors.leftMargin: Style.space(12)
                  anchors.rightMargin: Style.space(12)
                  visible: text !== ""
                  text: lyricRow.secondary
                  color: root.foreground
                  opacity: lyricRow.index === root.activeLineIndex ? 0.8 : 0.45
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
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
}
