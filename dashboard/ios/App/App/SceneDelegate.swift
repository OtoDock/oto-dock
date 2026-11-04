import UIKit

class SceneDelegate: UIResponder, UIWindowSceneDelegate {
    var window: UIWindow?
    /// The scene's own minimum size, put back once the window has opened at
    /// the screen's size.
    private var relaxedMinimumSize: CGSize?
    private var minimumReapplied = false

    func scene(_ scene: UIScene, willConnectTo session: UISceneSession, options connectionOptions: UIScene.ConnectionOptions) {
        guard let windowScene = scene as? UIWindowScene else { return }
        // A launch from a notification routes itself (the tap's install and
        // page), so the shell must not open the favorite over it.
        PushBridge.shared.launchedByNotification = connectionOptions.notificationResponse != nil
        openAtScreenSize(windowScene)
        let window = UIWindow(windowScene: windowScene)
        window.rootViewController = ShellViewController()
        self.window = window
        window.makeKeyAndVisible()
    }

    /// iPadOS 26 opens an app in the window it last had, which can be a small
    /// one, and `UIRequiresFullScreen` would take the resizing away. So the
    /// scene's minimum size is raised while the window opens, which opens it
    /// full screen, and put back once the window is there, so the person can
    /// resize it afterwards. The minimum is one point short of the screen on
    /// both axes: the screen's own size is refused as a size no window has,
    /// and the point less is met by full screen alone.
    private func openAtScreenSize(_ windowScene: UIWindowScene) {
        guard UIDevice.current.userInterfaceIdiom == .pad,
              let restrictions = windowScene.sizeRestrictions else { return }
        relaxedMinimumSize = restrictions.minimumSize
        restrictions.minimumSize = Self.nearlyTheScreen(of: windowScene, short: 1)
    }

    private static func nearlyTheScreen(of windowScene: UIWindowScene, short: CGFloat) -> CGSize {
        let screen = windowScene.screen.bounds.size
        return CGSize(width: screen.width - short, height: screen.height - short)
    }

    /// Once the window is at the screen's size the scene's own minimum comes
    /// back. Before that a relaxed minimum would leave a restored small
    /// window where it was. The first launch after a rotation or an
    /// appearance change opens with the minimum set at connect ignored, and
    /// setting the same value again changes nothing: a window still small
    /// once the scene is active gets a different minimum, then the one
    /// point short again. A window the system leaves where it is keeps its
    /// size, and the scene's own minimum comes back after a moment either
    /// way, so the person is never held at a raised minimum.
    private func relaxWhenOpen(_ windowScene: UIWindowScene) {
        guard let size = relaxedMinimumSize, let restrictions = windowScene.sizeRestrictions else { return }
        let window = windowScene.coordinateSpace.bounds.size
        let wanted = Self.nearlyTheScreen(of: windowScene, short: 1)
        ShellViewController.log.debug("window: \(NSCoder.string(for: window), privacy: .public) on a screen of \(NSCoder.string(for: windowScene.screen.bounds.size), privacy: .public)")
        guard window.width >= wanted.width, window.height >= wanted.height else {
            if windowScene.activationState == .foregroundActive, !minimumReapplied {
                minimumReapplied = true
                restrictions.minimumSize = Self.nearlyTheScreen(of: windowScene, short: 2)
                DispatchQueue.main.async { restrictions.minimumSize = wanted }
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in
                    guard let self, let size = self.relaxedMinimumSize else { return }
                    self.relaxedMinimumSize = nil
                    restrictions.minimumSize = size
                }
            }
            return
        }
        relaxedMinimumSize = nil
        restrictions.minimumSize = size
    }

    func windowScene(_ windowScene: UIWindowScene, didUpdate previousCoordinateSpace: UICoordinateSpace,
                     interfaceOrientation previousInterfaceOrientation: UIInterfaceOrientation,
                     traitCollection previousTraitCollection: UITraitCollection) {
        relaxWhenOpen(windowScene)
    }

    func sceneDidBecomeActive(_ scene: UIScene) {
        ShellViewController.current?.sceneDidBecomeActive()
        if let windowScene = scene as? UIWindowScene { relaxWhenOpen(windowScene) }
    }

    func sceneDidEnterBackground(_ scene: UIScene) {
        ShellViewController.current?.sceneDidEnterBackground()
    }
}
