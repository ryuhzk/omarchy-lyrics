import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Services.Mpris

Item {
  id: root

  property var shell: null
  // Desktop overlay settings. The service has no `setting()` of its own - that
  // belongs to the bar widget - so the widget pushes these down, the same way
  // it already does for the panel.
  property bool overlayEnabled: true
  property int overlayFontSize: 30
  property int overlayBottomMargin: 180
  property string overlayPosition: "right"
  // The user's manual sync correction, shared with the bar so the overlay can
  // never show a different line than the bar does.
  property int overlayOffsetMs: 0
  // Which source is asked first ("netease" or "lrclib"), and what the line
  // under each lyric shows: "auto" (the translation, or the romanization when
  // there is none), "translation", "romanization" or "off".
  property string lyricsSource: "netease"
  property string secondaryMode: "auto"
  property int playerRevision: 0
  property int requestSerial: 0
  property var queuedRequest: null
  property string fetchOutput: ""
  property string fetchError: ""
  property var lines: []
  property string plainLyrics: ""
  property string lyricsStatus: "idle"
  property string errorText: ""
  property var matchedTrack: ({})
  property bool cached: false
  property int playbackPositionMs: 0
  property int lastTickMs: 0
  property int lastMprisPositionMs: -1
  property int lastMprisChangedAtMs: 0
  property string positionMode: "estimated"

  readonly property string pluginDir: decodeURIComponent(
    String(Qt.resolvedUrl(".")).replace(/^file:\/\//, "").replace(/\/$/, ""))
  readonly property string helperPath: pluginDir + "/lyrics.py"
  readonly property var players: Mpris.players ? Mpris.players.values : []
  readonly property var activePlayer: {
    playerRevision
    return selectActivePlayer()
  }
  readonly property bool hasMedia: activePlayer !== null
    && String(activePlayer.trackTitle || "").trim() !== ""
  readonly property string title: activePlayer ? String(activePlayer.trackTitle || "") : ""
  readonly property string artist: activePlayer ? String(activePlayer.trackArtist || "") : ""
  readonly property string album: activePlayer ? String(activePlayer.trackAlbum || "") : ""
  readonly property string artUrl: activePlayer ? String(activePlayer.trackArtUrl || "") : ""
  // A Jellyfin client (Feishin) ends its MPRIS track id with the item id, which
  // lets the helper ask the server for the library's own lyrics.
  readonly property string itemId: {
    var metadata = activePlayer ? activePlayer.metadata : null
    var trackId = metadata ? String(metadata["mpris:trackid"] || "") : ""
    var match = /\/([0-9a-f]{32})$/.exec(trackId)
    return match ? match[1] : ""
  }
  readonly property string playerIdentity: activePlayer
    ? String(activePlayer.identity || activePlayer.desktopEntry || "") : ""
  readonly property bool isPlaying: activePlayer ? activePlayer.isPlaying === true : false
  readonly property real durationSec: {
    if (activePlayer && activePlayer.lengthSupported && Number(activePlayer.length) > 0)
      return Number(activePlayer.length)
    return Number(matchedTrack.duration || 0)
  }
  readonly property string trackKey: activePlayer ? [
    playerKey(activePlayer), title, artist, album, artUrl
  ].join("\u001f") : ""
  readonly property int currentLineIndex: lineIndexAt(playbackPositionMs)
  readonly property string currentLine: currentLineIndex >= 0 && currentLineIndex < lines.length
    ? String(lines[currentLineIndex].text || "") : ""

  // The second line for a lyric: a translation to read the meaning, or a
  // romanization to sing along to, as the setting asks.
  function secondaryFor(line) {
    if (!line || secondaryMode === "off") return ""
    var translation = String(line.translation || "")
    var romanization = String(line.romanization || "")
    if (secondaryMode === "translation") return translation
    if (secondaryMode === "romanization") return romanization
    return translation !== "" ? translation : romanization
  }

  // The reading of each part of a line, to set above its characters, when the
  // setting wants a romanization and the backend could pair it with the words.
  function rubyFor(line) {
    if (!line || (secondaryMode !== "auto" && secondaryMode !== "romanization")) return []
    return Array.isArray(line.ruby) ? line.ruby : []
  }

  // What still goes under a line whose romanization already sits above it.
  function secondaryBesideRuby(line) {
    return line && secondaryMode === "auto" ? String(line.translation || "") : ""
  }

  onLyricsSourceChanged: requestLyrics(false)

  function playerKey(player) {
    if (!player) return ""
    return String(player.dbusName || player.desktopEntry || player.identity || "")
  }

  function boundedMetadata(value) {
    return String(value || "").slice(0, 512)
  }

  function isProxyPlayer(player) {
    var key = playerKey(player).toLowerCase()
    return key.indexOf("playerctld") !== -1
  }

  function playerScore(player) {
    if (!player) return -1
    var score = player.isPlaying ? 1000 : 0
    if (String(player.trackTitle || "") !== "") score += 100
    if (String(player.trackArtist || "") !== "") score += 40
    if (player.canTogglePlaying || player.canPlay || player.canPause) score += 10
    if (isProxyPlayer(player)) score -= 1
    return score
  }

  function selectActivePlayer() {
    var selected = null
    var selectedScore = -1
    for (var i = 0; i < players.length; i++) {
      var score = playerScore(players[i])
      if (score > selectedScore) {
        selected = players[i]
        selectedScore = score
      }
    }
    return selectedScore >= 100 ? selected : null
  }

  function lineIndexAt(positionMs) {
    var target = Math.max(0, Number(positionMs) || 0)
    var low = 0
    var high = lines.length - 1
    var found = -1
    while (low <= high) {
      var middle = Math.floor((low + high) / 2)
      var atMs = Number(lines[middle].atMs) || 0
      if (atMs <= target) {
        found = middle
        low = middle + 1
      } else {
        high = middle - 1
      }
    }
    return found
  }

  function clearLyrics(status) {
    lines = []
    plainLyrics = ""
    lyricsStatus = status || "idle"
    errorText = ""
    matchedTrack = ({})
    cached = false
  }

  function resetPositionTracking() {
    var now = Date.now()
    var observed = activePlayer && activePlayer.positionSupported
      ? Math.max(0, Math.round(Number(activePlayer.position) * 1000)) : -1
    playbackPositionMs = observed > 0 ? observed : 0
    lastMprisPositionMs = observed
    lastMprisChangedAtMs = now
    lastTickMs = now
    positionMode = observed > 0 ? "mpris" : "estimated"
  }

  function requestLyrics(refresh) {
    requestSerial += 1
    resetPositionTracking()
    if (!hasMedia) {
      queuedRequest = null
      clearLyrics("idle")
      return
    }
    queuedRequest = {
      serial: requestSerial,
      trackKey: trackKey,
      title: boundedMetadata(title),
      artist: boundedMetadata(artist),
      album: boundedMetadata(album),
      duration: activePlayer && activePlayer.lengthSupported ? Number(activePlayer.length) : 0,
      itemId: itemId,
      refresh: refresh === true
    }
    lyricsStatus = "loading"
    errorText = ""
    pumpFetch()
  }

  function pumpFetch() {
    if (fetchProcess.running || !queuedRequest) return
    var request = queuedRequest
    queuedRequest = null
    fetchOutput = ""
    fetchError = ""
    fetchProcess.serial = request.serial
    fetchProcess.trackKey = request.trackKey
    var command = [
      "python3", helperPath, "fetch",
      "--title", request.title,
      "--artist", request.artist,
      "--album", request.album,
      "--duration", String(Math.max(0, Number(request.duration) || 0)),
      "--source", lyricsSource === "lrclib" ? "lrclib" : "netease",
      "--item-id", request.itemId || ""
    ]
    if (request.refresh) command.push("--refresh")
    fetchProcess.command = command
    fetchProcess.running = true
  }

  // [text, reading] pairs from the backend, checked like everything else it sends.
  function safeRuby(value) {
    if (!Array.isArray(value)) return []
    var result = []
    for (var i = 0; i < value.length && i < 200; i++) {
      var pair = value[i]
      if (!Array.isArray(pair) || pair.length < 2) return []
      result.push([String(pair[0] || "").slice(0, 64), String(pair[1] || "").slice(0, 64)])
    }
    return result
  }

  function applyResponse(raw, exitCode) {
    if (fetchProcess.serial !== requestSerial || fetchProcess.trackKey !== trackKey) return
    try {
      var response = JSON.parse(String(raw || "{}"))
      if (!response || response.schemaVersion !== 1) throw new Error("unsupported response")
      if (response.ok !== true) {
        clearLyrics("error")
        errorText = String(response.error || fetchError || "Could not fetch lyrics")
        return
      }
      var safeLines = []
      var sourceLines = Array.isArray(response.lines) ? response.lines : []
      for (var i = 0; i < sourceLines.length && i < 5000; i++) {
        var line = sourceLines[i]
        if (!line || !isFinite(Number(line.atMs))) continue
        safeLines.push({
          atMs: Math.max(0, Math.round(Number(line.atMs))),
          text: String(line.text || "").slice(0, 4096),
          translation: String(line.translation || "").slice(0, 4096),
          romanization: String(line.romanization || "").slice(0, 4096),
          ruby: safeRuby(line.ruby)
        })
      }
      lines = safeLines
      plainLyrics = String(response.plainLyrics || "").slice(0, 1000000)
      lyricsStatus = String(response.status || "not_found")
      matchedTrack = response.track && typeof response.track === "object" ? response.track : ({})
      cached = response.cached === true
      errorText = ""
    } catch (error) {
      clearLyrics("error")
      errorText = exitCode === 0
        ? "The lyrics helper returned invalid data"
        : (fetchError || "Could not fetch lyrics")
    }
  }

  function updatePlaybackPosition() {
    var now = Date.now()
    var elapsed = lastTickMs > 0 ? Math.max(0, Math.min(1000, now - lastTickMs)) : 0
    lastTickMs = now
    var player = activePlayer
    if (!player) {
      playbackPositionMs = 0
      positionMode = "estimated"
      return
    }

    var observed = player.positionSupported
      ? Math.max(0, Math.round(Number(player.position) * 1000)) : -1
    if (observed >= 0 && (lastMprisPositionMs < 0 || Math.abs(observed - lastMprisPositionMs) >= 50)) {
      playbackPositionMs = observed
      lastMprisPositionMs = observed
      lastMprisChangedAtMs = now
      positionMode = "mpris"
      return
    }

    if (player.isPlaying) playbackPositionMs += elapsed
    if (observed < 0 || now - lastMprisChangedAtMs > 1500) positionMode = "estimated"
  }

  function runAction(action) {
    var player = activePlayer
    if (!player) return false
    if (action === "next" && player.canGoNext) player.next()
    else if (action === "previous" && player.canGoPrevious) player.previous()
    else if (action === "playPause" && player.isPlaying && player.canPause) player.pause()
    else if (action === "playPause" && !player.isPlaying && player.canPlay) player.play()
    else if (action === "playPause" && player.canTogglePlaying) player.togglePlaying()
    else return false
    return true
  }

  onTrackKeyChanged: requestLyrics(false)
  Component.onCompleted: requestLyrics(false)

  Instantiator {
    model: root.players
    delegate: Connections {
      required property var modelData
      target: modelData
      function onIsPlayingChanged() { root.playerRevision += 1 }
    }
  }

  Timer {
    interval: 250
    running: true
    repeat: true
    onTriggered: root.updatePlaybackPosition()
  }

  Process {
    id: fetchProcess
    property int serial: 0
    property string trackKey: ""

    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.fetchOutput = String(text || "")
    }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.fetchError = String(text || "").trim().slice(0, 4096)
    }
    onExited: function(exitCode) {
      root.applyResponse(root.fetchOutput, exitCode)
      Qt.callLater(root.pumpFetch)
    }
  }

  // The desktop overlay, one per monitor. Variants rather than a single window
  // because a layer surface belongs to one output: with two monitors a lone
  // window would appear on whichever the compositor picked and be missing from
  // the other.
  Variants {
    model: root.overlayEnabled ? Quickshell.screens : []

    Overlay {
      required property var modelData
      screen: modelData
      lyricsService: root
      fontSize: root.overlayFontSize
      bottomMargin: root.overlayBottomMargin
      position: root.overlayPosition
    }
  }

  IpcHandler {
    target: "lyrics"

    function refresh(): string {
      root.requestLyrics(true)
      return "ok"
    }

    function status(): string {
      return JSON.stringify({
        hasMedia: root.hasMedia,
        title: root.title,
        artist: root.artist,
        lyricsStatus: root.lyricsStatus,
        lineCount: root.lines.length,
        currentLine: root.currentLine,
        positionMs: root.playbackPositionMs,
        positionMode: root.positionMode,
        cached: root.cached,
        error: root.errorText
      })
    }
  }
}
