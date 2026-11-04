import Foundation
import WebKit
import Capacitor
import OtoDockShell

/// The web view's navigation delegate: Capacitor's own handler behind a
/// forwarder that decides what the shell must decide first. A main-frame
/// https navigation to a sibling of the active host (a forward-auth
/// redirect) stays in the web view; a main-frame load of the local scheme
/// is allowed only when the shell started it; a link marked for download
/// and a response the view cannot show become downloads; a main-frame
/// failure on the active host loads the unreachable page.
final class NavigationForwarder: NSObject, WKNavigationDelegate {

    private weak var shell: ShellViewController?
    private weak var original: WebViewDelegationHandler?

    init(shell: ShellViewController, original: WebViewDelegationHandler) {
        self.shell = shell
        self.original = original
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let shell, let original else { decisionHandler(.cancel); return }
        let mainFrame = navigationAction.targetFrame == nil || navigationAction.targetFrame?.isMainFrame == true
        let url = navigationAction.request.url
        if navigationAction.shouldPerformDownload {
            decisionHandler(.download)
            return
        }
        if mainFrame {
            shell.pageStarted()
            if let url, url.scheme?.lowercased() == "capacitor" {
                if shell.takeExpectedLocalLoad() {
                    decisionHandler(.allow)
                } else {
                    ShellViewController.log.warning("a local navigation the shell did not start was refused")
                    decisionHandler(.cancel)
                }
                return
            }
            if let url, AuthSibling.isSibling(scheme: url.scheme, requestHost: url.host, activeHost: shell.activeHost) {
                decisionHandler(.allow)
                return
            }
        }
        original.webView(webView, decidePolicyFor: navigationAction, decisionHandler: decisionHandler)
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        if navigationResponse.isForMainFrame {
            let disposition = (navigationResponse.response as? HTTPURLResponse)?
                .value(forHTTPHeaderField: "Content-Disposition")?.lowercased() ?? ""
            if !navigationResponse.canShowMIMEType || disposition.hasPrefix("attachment") {
                decisionHandler(.download)
                return
            }
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        shell?.downloads.adopt(download)
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        shell?.downloads.adopt(download)
    }

    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
        original?.webView(webView, didStartProvisionalNavigation: navigation)
    }

    func webView(_ webView: WKWebView, didCommit navigation: WKNavigation!) {
        shell?.pageCommittedNow()
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        original?.webView(webView, didFinish: navigation)
        shell?.pageFinished(webView.url)
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        original?.webView(webView, didFail: navigation, withError: error)
        shell?.mainFrameFailed(error, failedURL: webView.url)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        original?.webView(webView, didFailProvisionalNavigation: navigation, withError: error)
        shell?.mainFrameFailed(error, failedURL: nil)
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        original?.webViewWebContentProcessDidTerminate(webView)
    }

    func webView(_ webView: WKWebView, didReceive challenge: URLAuthenticationChallenge,
                 completionHandler: @escaping @MainActor (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        guard let original else { completionHandler(.performDefaultHandling, nil); return }
        original.webView(webView, didReceive: challenge, completionHandler: completionHandler)
    }
}

/// The web view's UI delegate: the media capture permission goes to the
/// main frame of the active origin only; the prompt channel Capacitor's
/// cookie and HTTP plugins use from any frame is answered empty here and
/// never forwarded; a new window opens in the system browser for http(s)
/// addresses only; the dialogs are Capacitor's.
final class UIForwarder: NSObject, WKUIDelegate {

    private weak var shell: ShellViewController?
    private weak var original: WebViewDelegationHandler?

    init(shell: ShellViewController, original: WebViewDelegationHandler) {
        self.shell = shell
        self.original = original
    }

    private func isActiveMainFrame(_ origin: WKSecurityOrigin, _ frame: WKFrameInfo) -> Bool {
        guard frame.isMainFrame, let active = shell?.activeOrigin else { return false }
        return NativeBridgePolicy.sameOrigin(active, ChannelJudge.originString(protocol: origin.protocol, host: origin.host, port: origin.port))
    }

    func webView(_ webView: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin, initiatedByFrame frame: WKFrameInfo,
                 type: WKMediaCaptureType, decisionHandler: @escaping (WKPermissionDecision) -> Void) {
        decisionHandler(isActiveMainFrame(origin, frame) ? .grant : .deny)
    }

    func webView(_ webView: WKWebView, requestDeviceOrientationAndMotionPermissionFor origin: WKSecurityOrigin,
                 initiatedByFrame frame: WKFrameInfo, decisionHandler: @escaping (WKPermissionDecision) -> Void) {
        decisionHandler(isActiveMainFrame(origin, frame) ? .grant : .deny)
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping () -> Void) {
        guard let original else { completionHandler(); return }
        original.webView(webView, runJavaScriptAlertPanelWithMessage: message, initiatedByFrame: frame, completionHandler: completionHandler)
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (Bool) -> Void) {
        guard let original else { completionHandler(false); return }
        original.webView(webView, runJavaScriptConfirmPanelWithMessage: message, initiatedByFrame: frame, completionHandler: completionHandler)
    }

    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String, defaultText: String?,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (String?) -> Void) {
        // Capacitor's cookie and HTTP plugins' prompt() payloads are answered by
        // the judge, never forwarded: no page writes a cookie for another host.
        if let answer = ChannelJudge.promptAnswer(prompt) {
            completionHandler(answer)
            return
        }
        guard let original else { completionHandler(nil); return }
        original.webView(webView, runJavaScriptTextInputPanelWithPrompt: prompt, defaultText: defaultText,
                         initiatedByFrame: frame, completionHandler: completionHandler)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for navigationAction: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url, let scheme = url.scheme?.lowercased(), scheme == "http" || scheme == "https" {
            UIApplication.shared.open(url)
        }
        return nil
    }
}
