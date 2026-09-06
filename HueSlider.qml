import QtQuick
import qs.Ui

// Omarchy-native panel slider with reversed, high-sensitivity touchpad input.
PanelSlider {
  id: root

  // Amount applied for one traditional wheel notch. Smooth touchpad deltas
  // are scaled proportionally, which keeps two-finger adjustment precise.
  property real wheelStep: 1
  signal adjusted(real adjustedValue)

  onMoved: function(next) {
    root.value = next
    root.adjusted(next)
  }

  MouseArea {
    anchors.fill: parent
    acceptedButtons: Qt.NoButton
    onWheel: function(event) {
      var units = event.pixelDelta.y !== 0
        ? -event.pixelDelta.y / 8
        : -event.angleDelta.y / 120
      if (units === 0) return
      var next = Math.max(root.minimum, Math.min(root.maximum, root.value + units * root.wheelStep))
      root.value = next
      root.adjusted(next)
      event.accepted = true
    }
  }
}
