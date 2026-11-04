import UIKit
import AuthenticationServices

/// The system's sign-in browser for a sign-in or a connect: an
/// `ASWebAuthenticationSession` that shares Safari's passwords and passkeys,
/// names the site in its own sheet, and hands the `otodock://` callback
/// straight back. One session at a time, process-wide: the shell that
/// opened it may be replaced while the sheet is up (a notification tap
/// switches installations), so the result goes to the shell that is current
/// when the session ends, and the flow it answers is the persisted one.
final class AuthBrowser: NSObject, ASWebAuthenticationPresentationContextProviding {

    static let shared = AuthBrowser()

    private var session: ASWebAuthenticationSession?

    /// True while a session is up: the caller refuses a second request before
    /// it arms anything, so the running sign-in keeps its flow.
    var isOpen: Bool { session != nil }

    /// True when the session started; false when one is already up or the
    /// system refused to start it.
    func open(_ url: URL) -> Bool {
        if isOpen { return false }
        let session = ASWebAuthenticationSession(url: url, callbackURLScheme: "otodock") { [weak self] callback, error in
            self?.session = nil
            if let error, callback == nil, (error as? ASWebAuthenticationSessionError)?.code != .canceledLogin {
                ShellViewController.log.warning("sign-in browser ended: \((error as NSError).code)")
            }
            DispatchQueue.main.async {
                guard let shell = ShellViewController.current else { return }
                if let callback {
                    shell.handleDeepLink(callback)
                } else {
                    shell.signInBrowserClosed()
                }
            }
        }
        session.prefersEphemeralWebBrowserSession = false
        session.presentationContextProvider = self
        self.session = session
        if session.start() {
            return true
        }
        self.session = nil
        return false
    }

    func presentationAnchor(for session: ASWebAuthenticationSession) -> ASPresentationAnchor {
        return ShellViewController.current?.view.window ?? UIApplication.shared.connectedScenes
            .compactMap { ($0 as? UIWindowScene)?.keyWindow }.first ?? ASPresentationAnchor()
    }
}
