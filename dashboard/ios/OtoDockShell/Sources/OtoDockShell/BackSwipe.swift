import Foundation

/// The swipe from the screen's left edge that goes back one page of the
/// dashboard. iOS has no system back, and WebKit's own back-forward gesture
/// stays off: it travels the web view's whole list, another site's page
/// included (a sign-in gateway's), and it would run beside the dashboard's
/// own swipes. The shell's recognizer asks these rules instead.
public enum BackSwipe {

    /// The dashboard's own swipes ignore a touch that starts this close to
    /// the edge (`EDGE_DEAD_ZONE_PX` in the settings shell), so one swipe is
    /// never counted by both.
    public static let edgeZone: Double = 20
    /// The dashboard's swipe thresholds (`useSwipeGesture`).
    public static let minDistance: Double = 50
    public static let directionRatio: Double = 1.5

    /// Whether a finished pan counts as back: it began inside the edge zone
    /// (a negative start is a touch beside a web view inset for the sensor
    /// housing), ended far enough to the right, and ran mostly sideways. A
    /// finger that returns toward the edge before it lifts takes it back.
    public static func counts(startX: Double, dx: Double, dy: Double) -> Bool {
        return startX < edgeZone && dx >= minDistance && dx >= abs(dy) * directionRatio
    }

    /// Run in the dashboard's page when a swipe counts; the value names what
    /// it did. A layer that owns the screen (a dialog, the image viewer, a
    /// fullscreen video, the workspace's selection) keeps its own controls.
    /// The first entry of the page's history has nothing of the dashboard
    /// behind it. A chat is the home page, and its own swipe opens the
    /// history drawer. Every other page goes back one entry, as the browser's
    /// back does.
    public static let script = """
    (function () {
      if (window.__otodockDialogActive === true || window.__otodockVideoFullscreenActive === true ||
          window.__otodockLightboxActive === true || window.__otodockWorkspaceSelectionActive === true) {
        return 'layer';
      }
      var idx = (window.history.state && window.history.state.idx) || 0;
      if (idx <= 0) return 'first';
      var path = window.location.pathname;
      if (path === '/' || path.indexOf('/chat/') === 0) return 'chat';
      window.history.back();
      return 'back';
    })()
    """
}
