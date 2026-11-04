import UIKit
import UserNotifications
import Capacitor
import FirebaseCore
import FirebaseMessaging

/// Push for the app: Firebase Messaging turns Apple's device token into the
/// FCM token the relay sends to, and that token is what Capacitor's push
/// plugin hands the dashboard as its registration. The notification
/// center's delegate is this object, set before the launch ends (a tap that
/// opened the app reaches only a delegate set there) and never replaced by
/// a bridge; it forwards to the current bridge's router, where the push
/// plugin listens, and buffers a response that arrives before a bridge
/// exists. A build without the Firebase config refuses the registration
/// instead of ending the app.
final class PushBridge: NSObject, UNUserNotificationCenterDelegate, MessagingDelegate {

    static let shared = PushBridge()
    static let notConfigured = "Push is not configured in this build of the app"

    private(set) var configured = false
    /// Set by the scene before the first shell: a launch from a notification
    /// routes itself, so the favorite is not forced over it.
    var launchedByNotification = false
    private weak var router: NotificationRouter?
    private var pendingResponse: UNNotificationResponse?

    func start() {
        if Bundle.main.url(forResource: "GoogleService-Info", withExtension: "plist") != nil {
            FirebaseApp.configure()
            Messaging.messaging().delegate = self
            configured = true
        } else {
            ShellViewController.log.info("no Firebase config in the bundle: this build runs without push")
        }
        UNUserNotificationCenter.current().delegate = self
    }

    /// The current bridge's router, where the push plugin registered its
    /// handler; a response held since the launch is delivered now.
    func attach(bridge: CapacitorBridge) {
        router = bridge.notificationRouter
        if let response = pendingResponse {
            pendingResponse = nil
            bridge.notificationRouter.pushNotificationHandler?.didReceive(response: response)
        }
    }

    // MARK: - The token

    /// The FCM token Firebase hands the delegate, once per app start and on
    /// every refresh. Firebase 13 deprecates every token fetch for its
    /// installation-id registration, which the relay cannot send to, so the
    /// delegate is the one source. Each registration the dashboard asks for
    /// (every sign-in, so a second person on the device subscribes too) is
    /// answered from it; a token not yet known is posted when it comes.
    private var fcmToken: String?

    func apnsTokenArrived(_ deviceToken: Data) {
        guard configured else {
            registrationFailed(NSError(domain: "com.otodock.push", code: 1,
                                       userInfo: [NSLocalizedDescriptionKey: Self.notConfigured]))
            return
        }
        let messaging = Messaging.messaging()
        // Firebase reads the push environment from the embedded provisioning
        // profile and takes a simulator for a device without push, so it
        // registers the simulator's token as a production one and FCM's
        // pushes never arrive there. A Debug build runs on the sandbox
        // gateway wherever it runs (the simulator, a device from Xcode); a
        // store build reads its profile.
        #if DEBUG
        messaging.setAPNSToken(deviceToken, type: .sandbox)
        #else
        messaging.apnsToken = deviceToken
        #endif
        // Firebase's held start (the Info.plist keeps auto-init off): from the
        // first registration on, Firebase fetches and refreshes the token by
        // itself and hands it to the delegate.
        messaging.isAutoInitEnabled = true
        if let fcmToken { postRegistration(fcmToken) }
    }

    func registrationFailed(_ error: Error) {
        NotificationCenter.default.post(name: .capacitorDidFailToRegisterForRemoteNotifications, object: error)
    }

    func messaging(_ messaging: Messaging, didReceiveRegistrationToken fcmToken: String?) {
        guard let fcmToken else { return }
        self.fcmToken = fcmToken
        postRegistration(fcmToken)
    }

    /// Capacitor's push plugin listens for this and raises the dashboard's
    /// `registration` event with the token.
    private func postRegistration(_ token: String) {
        ShellViewController.log.info("push token ready")
        NotificationCenter.default.post(name: .capacitorDidRegisterForRemoteNotifications, object: token)
    }

    // MARK: - The notification center

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        let options = router?.pushNotificationHandler?.willPresent(notification: notification) ?? [.banner, .list, .sound]
        completionHandler(options)
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        if let handler = router?.pushNotificationHandler {
            handler.didReceive(response: response)
        } else {
            pendingResponse = response
        }
        completionHandler()
    }

    /// The end-of-turn entries pile up while the person is away: swept when
    /// the app comes to the foreground, the alerts stay.
    func sweepTurnComplete() {
        let center = UNUserNotificationCenter.current()
        center.getDeliveredNotifications { delivered in
            let ids = delivered.filter { ($0.request.content.userInfo["ephemeral"] as? String) == "true" }.map { $0.request.identifier }
            if !ids.isEmpty { center.removeDeliveredNotifications(withIdentifiers: ids) }
        }
    }
}
