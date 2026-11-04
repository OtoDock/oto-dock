import Foundation
import WebKit
import Capacitor
import OtoDockShell

/// The dashboard's channel, `OtoDockNative`: every message goes to the
/// judge (`ChannelJudge`, in OtoDockShell) with the sender as WebKit reports
/// it, and a refusal gets one log line and a null reply. The bundled setup
/// page's `setup.*` methods and the dashboard's calls are the two verdicts
/// that act. Every path answers the reply handler exactly once.
final class NativeChannel: NSObject, WKScriptMessageHandlerWithReply {

    private weak var shell: ShellViewController?

    init(shell: ShellViewController) {
        self.shell = shell
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage,
                               replyHandler: @escaping (Any?, String?) -> Void) {
        guard let shell, !shell.retiring else { replyHandler(nil, nil); return }
        let sender = Self.sender(of: message)
        switch ChannelJudge.judgeChannel(sender, body: message.body, activeOrigin: shell.activeOrigin) {
        case .refused(let why):
            ShellViewController.log.warning("Native channel refused a message from \(why, privacy: .public)")
            replyHandler(nil, nil)
        case .setup:
            shell.handleSetupMessage(message.body as? String ?? "")
            replyHandler(nil, nil)
        case .dashboard:
            shell.dispatchNative(message.body as? String ?? "") { value in replyHandler(value, nil) }
        }
    }

    /// The sender as WebKit reports it: the posting frame, its security
    /// origin in the policy's form, the main frame's address.
    static func sender(of message: WKScriptMessage) -> ChannelJudge.Sender {
        let origin = message.frameInfo.securityOrigin
        return ChannelJudge.Sender(
            isMainFrame: message.frameInfo.isMainFrame,
            origin: ChannelJudge.originString(protocol: origin.protocol, host: origin.host, port: origin.port),
            pageURL: message.webView?.url?.absoluteString)
    }
}

/// Capacitor's own message handler, `bridge`, under the same judge: its
/// plugins answer the main frame of the active origin, the setup page's
/// console only, and never the three core plugins the judge names.
final class BridgeForwarder: NSObject, WKScriptMessageHandler {

    private weak var shell: ShellViewController?
    private weak var original: WebViewDelegationHandler?

    init(shell: ShellViewController, original: WebViewDelegationHandler) {
        self.shell = shell
        self.original = original
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let shell, !shell.retiring, let original else { return }
        let pluginId = (message.body as? [String: Any])?["pluginId"] as? String ?? ""
        switch ChannelJudge.judgeBridge(NativeChannel.sender(of: message), pluginId: pluginId, activeOrigin: shell.activeOrigin) {
        case .refused(let why):
            ShellViewController.log.warning("Plugin bridge refused a message from \(why, privacy: .public)")
        case .forwarded:
            original.userContentController(userContentController, didReceive: message)
        }
    }
}
