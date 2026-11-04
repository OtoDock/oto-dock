import Foundation
import WebKit
import OtoDockShell

/// The bundled setup page's small set of calls (the shim's `window.Android`
/// on `capacitor://localhost/setup.html`): add a server after a native
/// health probe, switch to one from the "unreachable" picker, star one,
/// cancel the add screen, open the privacy policy. The page reads the
/// installs from the list the shell pushes after each load.
final class SetupPage {

    private weak var shell: ShellViewController?

    init(shell: ShellViewController) {
        self.shell = shell
    }

    func handle(method: String, args: [Any]) {
        guard let shell else { return }
        func arg(_ n: Int) -> String? { n < args.count ? args[n] as? String : nil }
        switch method {
        case "setup.setFavorite":
            if let id = arg(0) { shell.store.setFavorite(id) }
        case "setup.openPrivacyPolicy":
            shell.openPrivacyPolicy()
        case "setup.cancelAddInstallation":
            if shell.store.active() != nil { shell.switchRecreate() }
        case "setup.switchFromSetup":
            // No route capture (the page is the setup page) and no guards (no
            // live session on it): the way out when the favorite is down.
            if let id = arg(0), shell.store.byId(id) != nil {
                shell.store.setActive(id)
                shell.switchRecreate()
            }
        case "setup.addInstallation":
            addInstallation(arg(0))
        default:
            ShellViewController.log.warning("Setup page: unknown method \(method, privacy: .public)")
        }
    }

    /// The picker's list, pushed after the page finished (its own render ran
    /// while parsing, with an empty list), and only while the main frame is
    /// still the bundled page: the list carries every server's address. The
    /// picker itself renders only over a server that did not answer, as on
    /// Android: the add screen shows the form alone.
    func pushInstallations() {
        guard let shell, let webView = shell.webView, shell.isSetupPage(webView.url) else { return }
        let json = shell.store.publicJSON()
        var js = "window.__otodockSetInstalls && window.__otodockSetInstalls(\(json))"
        if ChannelJudge.isUnreachablePage(webView.url?.absoluteString) {
            js += "; window.renderUnreachablePicker && window.renderUnreachablePicker()"
        }
        webView.evaluateJavaScript(js, completionHandler: nil)
    }

    /// Validate a typed address natively (no CORS, no forward auth in the
    /// way), then add it and switch to it. Any HTTP answer, a sign-in
    /// redirect included, means the server is there; the first attempt may
    /// fail while the system's local-network question is up, so one retry.
    private func addInstallation(_ raw: String?) {
        guard let url = NativeBridgePolicy.serverUrl(raw) else {
            postValidationResult("error", "This address can't be used. Use an https address, or http only for a server on your local network.")
            return
        }
        guard let health = URL(string: url + "/health") else {
            postValidationResult("error", "The server did not answer. Check the address and try again.")
            return
        }
        probe(health, attempt: 1) { [weak self] reachable in
            // The probe answers on the session's queue; the switch is main-thread state.
            DispatchQueue.main.async {
                guard let self, let shell = self.shell else { return }
                if reachable {
                    shell.commitInstall(url)
                } else {
                    self.postValidationResult("error", "The server did not answer. Check the address and try again.")
                }
            }
        }
    }

    private func probe(_ url: URL, attempt: Int, done: @escaping (Bool) -> Void) {
        var request = URLRequest(url: url, timeoutInterval: 8)
        request.httpMethod = "GET"
        ShellViewController.ephemeralSession.dataTask(with: request) { _, response, error in
            if response is HTTPURLResponse {
                done(true)
            } else if attempt == 1 {
                DispatchQueue.global().asyncAfter(deadline: .now() + 1) { self.probe(url, attempt: 2, done: done) }
            } else {
                ShellViewController.log.info("health probe failed: \((error as NSError?)?.code ?? 0)")
                done(false)
            }
        }.resume()
    }

    /// The page's callback, on the page only while it is still the setup page.
    private func postValidationResult(_ type: String, _ message: String) {
        DispatchQueue.main.async {
            guard let shell = self.shell, let webView = shell.webView, shell.isSetupPage(webView.url) else { return }
            let js = "window._onValidationResult && window._onValidationResult(\(ShellViewController.jsQuote(type)), \(ShellViewController.jsQuote(message)))"
            webView.evaluateJavaScript(js, completionHandler: nil)
        }
    }
}

extension ShellViewController {
    /// A message from the bundled page (judged by NativeChannel): the header
    /// alone, `{m: 'setup.<name>', a: [...]}`.
    func handleSetupMessage(_ data: String) {
        let head = NativeBridgePolicy.splitMessage(data).head
        guard let headData = head.data(using: .utf8),
              let object = (try? JSONSerialization.jsonObject(with: headData)) as? [String: Any],
              let method = object["m"] as? String, method.hasPrefix("setup.") else { return }
        setupPage.handle(method: method, args: object["a"] as? [Any] ?? [])
    }
}
