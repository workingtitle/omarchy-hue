import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "io.github.workingtitle.hue"
  ipcTarget: "io.github.workingtitle.hue"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  readonly property var barIdentity: hostWidget || root
  property var lights: []
  property int onCount: 0
  property bool configured: false
  property bool present: false
  property bool loading: false
  property string message: "Loading…"
  property string bridgeAddress: setting("bridge", "")
  property string output: ""
  property bool outputTooLarge: false
  property var pendingActionArgs: null
  property string selectedLightId: ""
  property real detailHue: 0
  property real detailSaturation: 100
  readonly property color detailColor: Qt.hsla(detailHue / 360, detailSaturation / 100, 0.5, 1)
  readonly property string script: Qt.resolvedUrl("huectl.py").toString().replace("file://", "")
  readonly property int maxHelperOutputLength: 1024 * 1024
  readonly property int maxDisplayTextLength: 240
  readonly property int maxLightNameLength: 128
  readonly property string tooltip: configured
    ? (onCount + " of " + lights.length + " Hue lights on")
    : "Set up Philips Hue"
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property color accent: bar ? bar.foreground : Color.accent

  function run(args) {
    if (backend.running) return
    output = ""
    outputTooLarge = false
    loading = true
    backend.command = ["python3", script].concat(args)
    backend.running = true
  }
  function boundedText(value, fallback, limit) {
    var text = value === undefined || value === null ? fallback : String(value)
    text = text.replace(/\u0000/g, "")
    return text.length <= limit ? text : text.slice(0, Math.max(1, limit - 1)) + "…"
  }
  function refresh() { run(["status"]) }
  function refreshPresence() { run(["presence"]) }
  function discover() { run(["discover"]) }
  function pair() {
    var address = bridgeField.text.trim()
    if (address === "") { message = "Find a bridge or enter its IP address first"; return }
    run(["pair", address])
  }
  function setLight(id, on, brightness) {
    var args = ["set", id]
    if (on !== undefined && on !== null) args.push("--on", on ? "true" : "false")
    if (brightness !== undefined && brightness !== null) args.push("--brightness", String(Math.round(brightness)))
    run(args)
  }
  function setColor(light, color) {
    var args = ["set", light.id, "--color", color]
    if (light.gamut) args.push("--gamut", JSON.stringify(light.gamut))
    queueAction(args, "Setting color for " + boundedText(light.name, "Light", maxLightNameLength) + "…")
  }
  function setTemperature(light, mirek) {
    var bounded = Math.max(light.mirek_min || 153, Math.min(light.mirek_max || 500, mirek))
    queueAction(["set", light.id, "--mirek", String(Math.round(bounded))], "Setting white tone for " + boundedText(light.name, "Light", maxLightNameLength) + "…")
  }
  function queueAction(args, statusMessage) {
    message = boundedText(statusMessage, "Working…", maxDisplayTextLength)
    if (backend.running) {
      pendingActionArgs = args
      return
    }
    run(args)
  }
  function selectedLight() {
    for (var i = 0; i < lights.length; i++) if (lights[i].id === selectedLightId) return lights[i]
    return null
  }
  // HueSlider assigns `value` imperatively, which drops any declarative
  // binding, so the shared detail sliders are synced explicitly.
  function syncDetailBrightness() {
    var light = selectedLight()
    if (!light || brightnessSlider.dragging || detailBrightnessDebounce.running) return
    brightnessSlider.value = light.brightness
  }
  // Commit edits still waiting on a debounce to the light they were made on.
  function flushDetailEdits() {
    var light = selectedLight()
    if (detailBrightnessDebounce.running) {
      detailBrightnessDebounce.stop()
      if (light) queueAction(["set", light.id, "--brightness", String(Math.round(brightnessSlider.value))], "Setting brightness for " + boundedText(light.name, "Light", maxLightNameLength) + "…")
    }
    if (detailColorDebounce.running) {
      detailColorDebounce.stop()
      if (light) setColor(light, detailColor.toString())
    }
  }
  function showLight(light) {
    flushDetailEdits()
    flushRowBrightness()
    selectedLightId = light.id
    detailHue = typeof light.hue === "number" ? light.hue : 0
    detailSaturation = typeof light.saturation === "number" ? light.saturation : 100
    hueSlider.value = detailHue
    saturationSlider.value = detailSaturation
    syncDetailBrightness()
  }
  // Wheel or arrow keys on an overview row adjust that light's brightness.
  // The value is shown immediately and sent once adjusting pauses; raising
  // the brightness of a light that is off also switches it on.
  property string wheelLightId: ""
  property real wheelBrightness: 0
  property bool wheelTurnOn: false
  function rowBrightness(light) {
    return light.id === wheelLightId ? Math.round(wheelBrightness) : light.brightness
  }
  function rowOn(light) {
    return light.on || (light.id === wheelLightId && wheelTurnOn)
  }
  function wheelRow(light, event) {
    var units = event.pixelDelta.y !== 0
      ? -event.pixelDelta.y / 8
      : -event.angleDelta.y / 120
    adjustRowBrightness(light, units)
  }
  function adjustRowBrightness(light, units) {
    if (units === 0) return
    if (light.id !== wheelLightId) {
      flushRowBrightness()
      wheelLightId = light.id
      wheelBrightness = light.brightness
      wheelTurnOn = false
    }
    if (units > 0 && !light.on) wheelTurnOn = true
    wheelBrightness = Math.max(1, Math.min(100, wheelBrightness + units * 5))
    rowBrightnessDebounce.restart()
  }
  function flushRowBrightness() {
    if (!rowBrightnessDebounce.running) return
    rowBrightnessDebounce.stop()
    sendRowBrightness()
  }
  function sendRowBrightness() {
    for (var i = 0; i < lights.length; i++) {
      if (lights[i].id !== wheelLightId) continue
      var args = ["set", wheelLightId, "--brightness", String(Math.round(wheelBrightness))]
      if (wheelTurnOn) args.push("--on", "true")
      queueAction(args, "Setting brightness for " + boundedText(lights[i].name, "Light", maxLightNameLength) + "…")
      return
    }
  }

  // Keyboard navigation in the overview: Up/Down move the row cursor,
  // Left/Right dim or brighten, Enter opens the light, Space toggles it.
  property int cursorIndex: -1
  property bool returnPressed: false
  function moveCursor(dx, dy) {
    if (selectedLightId !== "" || lights.length === 0) return
    if (dy !== 0) {
      cursorIndex = cursorIndex < 0
        ? (dy > 0 ? 0 : lights.length - 1)
        : Math.max(0, Math.min(lights.length - 1, cursorIndex + dy))
    } else if (cursorIndex >= 0 && cursorIndex < lights.length) {
      adjustRowBrightness(lights[cursorIndex], dx)
    }
  }
  function activateCursor() {
    var openDetail = returnPressed
    returnPressed = false
    if (selectedLightId !== "" || cursorIndex < 0 || cursorIndex >= lights.length) return
    var light = lights[cursorIndex]
    if (openDetail) showLight(light)
    else setLight(light.id, !rowOn(light), null)
  }
  function escapePressed() {
    if (selectedLightId !== "") showOverview()
    else close()
  }
  function showOverview() {
    flushDetailEdits()
    for (var i = 0; i < lights.length; i++) if (lights[i].id === selectedLightId) cursorIndex = i
    selectedLightId = ""
  }
  function setAll(desired) {
    if (lights.length === 0 || backend.running) return
    batchQueue = []
    for (var i = 0; i < lights.length; i++) batchQueue.push(["set", lights[i].id, "--on", desired ? "true" : "false"])
    runNextBatch()
  }
  property var batchQueue: []
  function runNextBatch() {
    if (batchQueue.length === 0) { refresh(); return }
    var next = batchQueue.shift()
    run(next)
  }
  function open() {
    // Always start on the overview, without a keyboard selection.
    showOverview()
    cursorIndex = -1
    controller.show()
    Qt.callLater(refresh)
  }
  function close() { controller.hide() }
  function toggle() { opened ? close() : open() }
  function closeForPopoutSwitch() { controller.hide() }

  Component.onCompleted: Qt.callLater(root.refreshPresence)

  Process {
    id: backend
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        root.outputTooLarge = text.length > root.maxHelperOutputLength
        root.output = root.outputTooLarge ? "" : text
      }
    }
    stderr: StdioCollector { waitForEnd: true }
    onExited: function(exitCode) {
      loading = false
      if (outputTooLarge) { message = "Hue client response is too large"; return }
      var data
      try { data = JSON.parse(output || "{}") }
      catch (e) { message = "Invalid response from the Hue client"; return }
      if (batchQueue.length > 0) { runNextBatch(); return }
      if (command.indexOf("discover") >= 0 && data.ok) {
        if (data.bridges.length > 0) {
          bridgeAddress = data.bridges[0].bridge
          bridgeField.text = bridgeAddress
          message = data.bridges.length === 1 ? "Bridge found. Press its link button, then select Pair." : data.bridges.length + " bridges found; selected the first one."
        } else message = "No Hue Bridge found"
      } else if (command.indexOf("pair") >= 0 && data.ok) {
        configured = true
        message = "Paired"
        refreshTimer.start()
      } else if (command.indexOf("presence") >= 0) {
        configured = data.configured === true
        if (data.ok) {
          present = data.present === true
          if (data.bridge) bridgeAddress = data.bridge
          if (configured) {
            lights = data.lights || []
            onCount = data.on_count || 0
            message = lights.length === 0 ? "No reachable lights found" : onCount + " of " + lights.length + " lights on"
          } else {
            lights = []
            onCount = 0
            if (data.bridges && data.bridges.length > 0 && bridgeAddress === "") bridgeAddress = data.bridges[0].bridge
            message = present ? "Hue Bridge found. Pair it to control your lights." : "No local Hue Bridge found"
          }
        } else {
          present = false
          message = root.boundedText(data.error, "Hue Bridge is unreachable", root.maxDisplayTextLength)
        }
      } else if (command.indexOf("status") >= 0) {
        configured = data.configured === true
        if (data.ok) {
          lights = data.lights || []
          onCount = data.on_count || 0
          present = lights.length > 0
          bridgeAddress = data.bridge || bridgeAddress
          message = lights.length === 0 ? "No reachable lights found" : onCount + " of " + lights.length + " lights on"
          root.syncDetailBrightness()
          // Keep the optimistic row value until the bridge reports it.
          if (!rowBrightnessDebounce.running && !root.pendingActionArgs) root.wheelLightId = ""
          if (root.cursorIndex >= lights.length) root.cursorIndex = lights.length - 1
        } else {
          if (data.configured === true) present = false
          message = root.boundedText(data.error, "Hue Bridge is unreachable", root.maxDisplayTextLength)
        }
      } else if (!data.ok) message = root.boundedText(data.error, "Action failed", root.maxDisplayTextLength)
      else refreshTimer.start()
      if (pendingActionArgs) {
        var pending = pendingActionArgs
        pendingActionArgs = null
        Qt.callLater(function() { root.run(pending) })
      }
    }
  }

  Timer { id: refreshTimer; interval: 350; onTriggered: root.refresh() }
  Timer { id: rowBrightnessDebounce; interval: 250; onTriggered: root.sendRowBrightness() }
  Timer { interval: 30000; running: true; repeat: true; onTriggered: root.refreshPresence() }
  Timer { interval: 15000; running: root.opened; repeat: true; onTriggered: root.refresh() }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    centerOnBar: false
    contentWidth: fittedContentWidth(Style.space(440))
    contentHeight: fittedContentHeight(content.implicitHeight)
    focusTarget: keyCatcher

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: bridgeField.activeFocus
      onMoveRequested: function(dx, dy) { root.moveCursor(dx, dy) }
      onReturnRequested: root.returnPressed = true
      onActivateRequested: root.activateCursor()
      onCloseRequested: root.escapePressed()
      onTextKey: function(t) { if (t === "r" && root.selectedLightId === "") root.refresh() }

      Column {
        id: content
        width: parent.width
        spacing: Style.space(10)

        Item {
          width: parent.width
          height: Style.space(28)
          Row {
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(5)
            Text {
              anchors.verticalCenter: parent.verticalCenter
              visible: root.selectedLightId === ""
              text: "PHILIPS HUE"
              color: root.foreground
              font.family: bar ? bar.fontFamily : Style.font.family
              font.pixelSize: Style.font.heading
              font.bold: true
            }
            PanelActionButton {
              anchors.verticalCenter: parent.verticalCenter
              visible: root.selectedLightId === ""
              iconText: "↻"
              tooltipText: "Refresh lights"
              foreground: root.foreground
              fontFamily: bar ? bar.fontFamily : Style.font.family
              enabled: !root.loading
              onClicked: root.refresh()
            }
            Button {
              anchors.verticalCenter: parent.verticalCenter
              visible: root.selectedLightId !== ""
              text: "‹ Back"
              bordered: true
              foreground: root.foreground
              fontFamily: bar ? bar.fontFamily : Style.font.family
              onClicked: root.showOverview()
            }
          }
          Row {
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(6)
            Rectangle {
              anchors.verticalCenter: parent.verticalCenter
              width: Style.space(7); height: width; radius: width / 2
              color: root.loading ? root.dim : (root.onCount > 0 ? Color.accent : root.dim)
            }
            Text {
              text: root.loading
                ? "Refreshing…"
                : (root.lights.length > 0 ? root.onCount + " / " + root.lights.length + " on" : "")
              color: root.dim
              font.family: bar ? bar.fontFamily : Style.font.family
              font.pixelSize: Style.font.bodySmall
            }
          }
        }
        Text {
          width: parent.width
          text: root.loading ? "Refreshing…" : root.message
          textFormat: Text.PlainText
          visible: !root.configured
          color: root.dim
          wrapMode: Text.Wrap
          font.family: bar ? bar.fontFamily : Style.font.family
        }

        Column {
          visible: !root.configured
          width: parent.width
          spacing: Style.space(8)
          Text { width: parent.width; text: "1. Find bridge  2. Press link button  3. Pair"; color: root.foreground; wrapMode: Text.Wrap; font.family: bar ? bar.fontFamily : Style.font.family }
          TextField {
            id: bridgeField
            width: parent.width
            placeholderText: "Bridge IP, e.g. 192.168.1.20"
            text: root.bridgeAddress
          }
          Row {
            spacing: Style.space(8)
            Button { text: "Find bridge"; bordered: true; enabled: !root.loading; onClicked: root.discover() }
            Button { text: "Pair"; bordered: true; enabled: !root.loading && bridgeField.text.trim() !== ""; onClicked: root.pair() }
          }
        }

        Column {
          visible: root.selectedLightId === ""
          width: parent.width
          spacing: 0

          Repeater {
            model: root.lights
            delegate: Rectangle {
              id: compactLight
              required property var modelData
              required property int index
              width: content.width
              height: Style.space(40)
              color: root.cursorIndex === index ?Style.hoverFillFor(root.foreground, Color.accent) : "transparent"

              Rectangle {
                anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom
                height: 1
                color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.1)
              }

              Rectangle {
                id: rowSwatch
                anchors.left: parent.left; anchors.leftMargin: Style.space(10)
                anchors.verticalCenter: parent.verticalCenter
                width: Style.space(12); height: width; radius: width / 2
                color: modelData.swatch || root.dim
                opacity: root.rowOn(modelData) ? 1 : 0.3
                border.width: 1
                border.color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.35)
              }
              Text {
                anchors.left: rowSwatch.right; anchors.leftMargin: Style.space(8)
                anchors.verticalCenter: parent.verticalCenter
                text: root.boundedText(modelData.name, "Light", root.maxLightNameLength)
                textFormat: Text.PlainText
                color: root.foreground
                font.family: bar ? bar.fontFamily : Style.font.family
              }
              Text {
                anchors.right: rowSwitch.left; anchors.rightMargin: Style.space(8)
                anchors.verticalCenter: parent.verticalCenter
                text: root.rowBrightness(modelData) + "%  ›"
                color: root.dim
                font.family: bar ? bar.fontFamily : Style.font.family
              }
              ToggleSwitch {
                id: rowSwitch
                anchors.right: parent.right; anchors.rightMargin: Style.space(6)
                anchors.verticalCenter: parent.verticalCenter
                checked: root.rowOn(modelData)
                busy: root.loading
                foreground: root.foreground
                accent: Color.accent
                trackHeight: Style.space(18)
                onToggled: root.setLight(modelData.id, !checked, null)
              }
              MouseArea {
                id: rowMouse
                anchors.left: parent.left; anchors.right: rowSwitch.left
                anchors.top: parent.top; anchors.bottom: parent.bottom
                hoverEnabled: true
                // The pointer moves the keyboard cursor too, so keys continue from it.
                onContainsMouseChanged: if (containsMouse) root.cursorIndex = compactLight.index
                onClicked: root.showLight(compactLight.modelData)
              }
              MouseArea {
                anchors.fill: parent
                acceptedButtons: Qt.NoButton
                onWheel: function(event) {
                  root.wheelRow(compactLight.modelData, event)
                  event.accepted = true
                }
              }
            }
          }

          Row {
            id: overviewActions
            visible: root.configured && root.lights.length > 0
            width: parent.width
            topPadding: Style.space(10)
            spacing: Style.space(8)
            readonly property real actionWidth: (width - spacing) / 2
            Button { width: overviewActions.actionWidth; text: "All on"; bordered: true; enabled: !root.loading; onClicked: root.setAll(true) }
            Button { width: overviewActions.actionWidth; text: "All off"; bordered: true; enabled: !root.loading; onClicked: root.setAll(false) }
          }
        }

        Column {
          id: detailView
          readonly property var light: root.selectedLight()
          visible: root.selectedLightId !== "" && light !== null
          width: parent.width
          spacing: Style.space(10)

          Row {
            width: parent.width
            Text {
              anchors.verticalCenter: parent.verticalCenter
              text: root.boundedText(detailView.light ? detailView.light.name : "", "", root.maxLightNameLength)
              textFormat: Text.PlainText
              color: root.foreground
              font.family: bar ? bar.fontFamily : Style.font.family
              font.pixelSize: Style.font.title
              font.bold: true
            }
          }

          Toggle {
            width: parent.width
            label: "Power"
            description: detailView.light && detailView.light.on ? "On" : "Off"
            checked: detailView.light ? detailView.light.on : false
            foreground: root.foreground
            accent: Color.accent
            fontFamily: bar ? bar.fontFamily : Style.font.family
            onClicked: if (detailView.light) root.setLight(detailView.light.id, !checked, null)
          }

          PanelSeparator { foreground: root.foreground }
          PanelSectionHeader { text: "BRIGHTNESS"; foreground: root.foreground; fontFamily: bar ? bar.fontFamily : Style.font.family }
          HueSlider {
            id: brightnessSlider
            width: parent.width
            bar: root.bar
            minimum: 1; maximum: 100
            integer: true
            wheelStep: 5
            value: 1
            onAdjusted: detailBrightnessDebounce.restart()
            Timer { id: detailBrightnessDebounce; interval: 250; onTriggered: if (detailView.light) root.setLight(detailView.light.id, null, brightnessSlider.value) }
          }

          Column {
            visible: detailView.light ? detailView.light.temperature_capable : false
            width: parent.width
            spacing: Style.space(6)
            PanelSeparator { foreground: root.foreground }
            PanelSectionHeader { text: "WHITE TONE"; foreground: root.foreground; fontFamily: bar ? bar.fontFamily : Style.font.family }
            Row {
              id: whiteButtons
              width: parent.width
              spacing: Style.space(8)
              readonly property real buttonWidth: (width - spacing * 2) / 3
              Button { width: whiteButtons.buttonWidth; text: "Warm"; bordered: true; onClicked: root.setTemperature(detailView.light, 370) }
              Button { width: whiteButtons.buttonWidth; text: "Neutral"; bordered: true; onClicked: root.setTemperature(detailView.light, 250) }
              Button { width: whiteButtons.buttonWidth; text: "Cool"; bordered: true; onClicked: root.setTemperature(detailView.light, 153) }
            }
          }

          Column {
            visible: detailView.light ? detailView.light.color_capable : false
            width: parent.width
            spacing: Style.space(4)
            PanelSeparator { foreground: root.foreground }
            PanelSectionHeader { text: "COLOR"; foreground: root.foreground; fontFamily: bar ? bar.fontFamily : Style.font.family }
            Column {
              width: parent.width
              spacing: Style.space(2)
              Text { text: "Hue"; color: root.foreground }
              HueSlider {
                id: hueSlider
                width: parent.width; bar: root.bar; minimum: 0; maximum: 360; wheelStep: 15; value: root.detailHue
                fillColor: root.detailColor
                knobColor: root.detailColor
                onAdjusted: function(adjustedValue) { root.detailHue = adjustedValue; detailColorDebounce.restart() }
              }
            }
            Column {
              width: parent.width
              spacing: Style.space(2)
              Item {
                width: parent.width
                height: Style.font.body
                Text { anchors.left: parent.left; text: "Saturation"; color: root.foreground }
                Rectangle {
                  anchors.right: parent.right
                  anchors.verticalCenter: parent.verticalCenter
                  width: Style.space(16); height: width; radius: width / 2
                  color: root.detailColor
                  border.width: 1; border.color: root.foreground
                }
              }
              HueSlider {
                id: saturationSlider
                width: parent.width; bar: root.bar; minimum: 0; maximum: 100; wheelStep: 8; value: root.detailSaturation
                fillColor: root.detailColor
                knobColor: root.detailColor
                onAdjusted: function(adjustedValue) { root.detailSaturation = adjustedValue; detailColorDebounce.restart() }
              }
            }
            Timer {
              id: detailColorDebounce
              interval: 250
              onTriggered: if (detailView.light) root.setColor(detailView.light, root.detailColor.toString())
            }
          }
        }
      }
    }
  }
}
