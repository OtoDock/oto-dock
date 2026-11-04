import UIKit
import WebKit
import OSLog
import StoreKit
import Capacitor
import OtoDockShell

/// The app's one screen: Capacitor's bridge view controller around the
/// dashboard of the active installation, with the native shell the Android
/// app's MainActivity gives (the installations and their switch, the
/// origin-scoped channel, the sign-ins through the system browser, the
/// boot hold, the safe areas and the keyboard, the bars' colours, video
/// fullscreen, downloads). A switch between installations replaces the
/// window's root with a new controller, the twin of Android's recreate():
/// the server URL and the allowed host are frozen when the bridge is built.
final class ShellViewController: CAPBridgeViewController {

    static let log = Logger(subsystem: "com.otodock", category: "shell")

    static let setupPagePath = ChannelJudge.setupPagePath
    static let localOrigin = ChannelJudge.localOrigin
    static let addressNotUsable = "This server address can't be opened in the app. Switch to another server, or add it again by its https address."
    static let shellNotReady = "The app could not start safely. Close it and open it again."

    // Process-wide, as the Android statics: a switch skips the boot hold and
    // the favorite's override; the favorite opens only at the first boot.
    static var switchInProgress = false
    static var coldStarted = false
    private(set) static weak var current: ShellViewController?

    // The boot hold's bounds, the Android ones.
    private static let bootHoldMax: TimeInterval = 2.5
    private static let bootHoldCap: TimeInterval = 8.0
    private static let bootVisualWait: TimeInterval = 0.15
    private static let bootFade: TimeInterval = 0.25
    private static let containerFade: TimeInterval = 0.2

    let store = InstallStore(backing: UserDefaultsBacking())
    let flow = AuthFlow(backing: UserDefaultsBacking())
    private(set) var activeOrigin: String?
    private(set) var activeHost: String?
    private var pendingRoute: String?
    private var setupQuery: String?
    private var blockedMessage: String?
    /// Set when the channel's judge could not be installed: no handler stays
    /// and no page loads.
    private var judgeMissing = false
    /// The last finished main-frame load, cleared before any main-frame
    /// navigation away: the setup page's gate and the install id's bind read it.
    var currentLoadedURL: String?
    /// True once the loaded SPA signalled dashboardReady; reset each load.
    var dashboardIsReady = false
    /// The one local navigation the shell started (the setup page); any
    /// other main-frame load of the local scheme is refused.
    private var expectingLocalLoad = false
    /// Set while this controller is being replaced: a channel call arriving
    /// then belongs to the page being replaced.
    private(set) var retiring = false

    private let container = ContainerView()
    private let bootView = BootView()
    private var bootHeld = false
    private var pageCommitted = false
    private var bootStart = Date()
    private var splashExpected = false
    private var splashGone = false
    private var keyboardOverlap: CGFloat = 0
    private(set) var videoFullscreen = false
    private var videoLandscapeCue = false
    private var barLight = false
    private var barTop: CGFloat = -1
    private var barBottom: CGFloat = -1

    var isStreaming = false
    private var authInProgress = false
    private var authInProgressUntil: Int64 = 0
    var switchBusyFlag = false
    private var backgroundTask: UIBackgroundTaskIdentifier = .invalid
    private var privacyPolicyOpenedAt: Int64 = 0

    private var channel: NativeChannel?
    private var bridgeForwarder: BridgeForwarder?
    private var navigationForwarder: NavigationForwarder?
    private var uiForwarder: UIForwarder?
    lazy var downloads = Downloads(shell: self)
    lazy var setupPage = SetupPage(shell: self)

    // MARK: - The boot: which page, which origin

    override func instanceDescriptor() -> InstanceDescriptor {
        // A fresh descriptor: the inherited one reads a persisted base path
        // (the WebView plugin's) that would point the bridge at a folder the
        // app never has.
        let descriptor = InstanceDescriptor()
        let firstLoad = !Self.coldStarted
        Self.coldStarted = true
        if firstLoad && !Self.switchInProgress && !PushBridge.shared.launchedByNotification, let favorite = store.favorite() {
            store.setActive(favorite.id)
        }
        let active = store.active()
        let origin = active.flatMap { NativeBridgePolicy.origin($0.url) }
        if let active, let origin, NativeBridgePolicy.bootable(origin), let host = NativeBridgePolicy.host(origin) {
            // The saved route belongs to the switch that wrote it and is spent
            // by the boot that follows: a switch-in reopens there, every other
            // boot starts at the root, where the SPA opens the favorite agent.
            if let route = active.lastRoute {
                if Self.switchInProgress && Routes.isSafeRoutePath(route) { pendingRoute = route }
                store.setLastRoute(active.id, nil)
            }
            activeOrigin = origin
            activeHost = host
            descriptor.serverURL = origin
            descriptor.allowedNavigationHostnames = [host]
        } else {
            // No usable install: the bundled setup page (a real file, so the
            // bridge's own start check passes), with the picker over an
            // address the current rule refuses.
            descriptor.serverURL = nil
            descriptor.appStartPath = "setup.html"
            expectingLocalLoad = true
            if let active {
                blockedMessage = Self.addressNotUsable
                setupQuery = "error=unreachable&url=" + Self.queryEncode(active.url)
            }
        }
        return descriptor
    }

    override func webViewConfiguration(for instanceConfiguration: InstanceConfiguration) -> WKWebViewConfiguration {
        // Capacitor's version adds a cookie observer that copies WebKit's jar
        // into the shared HTTP cookie storage, where its cookie plugin reads
        // any host's cookies; the shell leaves the jar inside WebKit.
        let configuration = WKWebViewConfiguration()
        configuration.allowsInlineMediaPlayback = true
        configuration.suppressesIncrementalRendering = false
        configuration.allowsAirPlayForMediaPlayback = true
        configuration.mediaTypesRequiringUserActionForPlayback = []
        configuration.preferences.isElementFullscreenEnabled = true
        return configuration
    }

    override func capacitorDidLoad() {
        guard let webView, let bridge = bridge as? CapacitorBridge else {
            // Fail closed: without the judge Capacitor's own handler answers
            // nobody, and viewDidLoad loads nothing.
            webView?.configuration.userContentController.removeScriptMessageHandler(forName: "bridge")
            judgeMissing = true
            return
        }
        let content = webView.configuration.userContentController
        // The dashboard channel: one shim for the main frame, called with the
        // active origin so the object exists on that origin's pages and the
        // bundled setup page only, then judged per message by the sender's
        // origin (NativeChannel).
        if let url = Bundle.main.url(forResource: "OtoDockNative", withExtension: "js"),
           let shim = try? String(contentsOf: url, encoding: .utf8) {
            let source = shim + "(" + Self.jsQuote(activeOrigin ?? "") + ");"
            content.addUserScript(WKUserScript(source: source, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        } else {
            Self.log.error("OtoDockNative.js is not in the bundle: no channel")
        }
        let channel = NativeChannel(shell: self)
        content.addScriptMessageHandler(channel, contentWorld: .page, name: NativeBridgePolicy.channel)
        self.channel = channel
        // Capacitor's own handler, under the same judge: a page of another
        // origin, a child frame or the setup page reaches no plugin.
        let forwarder = BridgeForwarder(shell: self, original: bridge.webViewDelegationHandler)
        content.removeScriptMessageHandler(forName: "bridge")
        content.add(forwarder, name: "bridge")
        bridgeForwarder = forwarder
        let navigation = NavigationForwarder(shell: self, original: bridge.webViewDelegationHandler)
        webView.navigationDelegate = navigation
        navigationForwarder = navigation
        let ui = UIForwarder(shell: self, original: bridge.webViewDelegationHandler)
        webView.uiDelegate = ui
        uiForwarder = ui
        PushBridge.shared.attach(bridge: bridge)
    }

    override func viewDidLoad() {
        let switching = Self.switchInProgress
        Self.switchInProgress = false
        if judgeMissing {
            // Capacitor's viewDidLoad starts the page load: skipped, so the
            // shell shows the dialog over the empty container and nothing else.
            Self.current = self
            view = container
            DispatchQueue.main.async { self.showBlockedDialog(Self.shellNotReady) }
            return
        }
        installBoot(switching: switching)
        super.viewDidLoad()
        Self.current = self
        guard let webView else { return }

        // The web view inside a container that shows the page colour behind
        // the bars: the container is the controller's view from here on.
        container.frame = webView.frame
        container.setColors(bootField, bootFieldDeep)
        view = container
        container.addSubview(webView)
        webView.backgroundColor = bootField
        webView.scrollView.backgroundColor = bootField
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        if bootHeld {
            bootView.frame = container.bounds
            container.addSubview(bootView)
        }
        setNeedsStatusBarAppearanceUpdate()

        let center = NotificationCenter.default
        center.addObserver(self, selector: #selector(keyboardWillChange(_:)), name: UIResponder.keyboardWillChangeFrameNotification, object: nil)
        center.addObserver(self, selector: #selector(keyboardWillChange(_:)), name: UIResponder.keyboardWillHideNotification, object: nil)
        center.addObserver(self, selector: #selector(deviceOrientationChanged), name: UIDevice.orientationDidChangeNotification, object: nil)
        installSwipes(on: webView)
        UIDevice.current.beginGeneratingDeviceOrientationNotifications()

        // The first load is the server's root (or the bare setup page); a
        // route or a query replaces it at once, which cancels that load.
        if let route = pendingRoute, let origin = activeOrigin, let url = URL(string: origin + route) {
            pendingRoute = nil
            webView.load(URLRequest(url: url))
        }
        if let query = setupQuery {
            setupQuery = nil
            loadSetupPage(query: query)
        }
        if let message = blockedMessage {
            blockedMessage = nil
            DispatchQueue.main.async { self.showBlockedDialog(message) }
        }
    }

    deinit {
        NotificationCenter.default.removeObserver(self)
        Self.log.info("shell released")
    }

    // MARK: - Layout: the safe areas, the keyboard, fullscreen

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        layoutWebView()
    }

    override func viewSafeAreaInsetsDidChange() {
        super.viewSafeAreaInsetsDidChange()
        layoutWebView()
    }

    /// The web view's frame by hand: the container's bounds inset by the
    /// safe area on all four sides, the bottom raised to the keyboard's
    /// overlap, as Android pads its container; the whole bounds during a
    /// video's fullscreen. The page reads zero safe-area insets.
    private func layoutWebView() {
        guard let webView else { return }
        let bounds = container.bounds
        bootView.frame = bounds
        if videoFullscreen {
            webView.frame = bounds
            return
        }
        let safe = Self.pageInsets(of: container)
        let bottom = max(safe.bottom, keyboardOverlap)
        webView.frame = CGRect(x: safe.left, y: safe.top,
                               width: max(0, bounds.width - safe.left - safe.right),
                               height: max(0, bounds.height - safe.top - bottom))
        Self.log.debug("layout: web view \(NSCoder.string(for: webView.frame), privacy: .public) in \(NSCoder.string(for: bounds), privacy: .public), insets \(NSCoder.string(for: safe), privacy: .public)")
        pushBarInsets(top: safe.top, bottom: safe.bottom, force: false)
    }

    /// The safe area, with the window controls' corner on iPadOS 26: in a
    /// window the plain safe area leaves them out and they would cover the
    /// dashboard's menu button, so the corner-adapted region starts the page
    /// below them. Full screen has no such corner, so it reads the same.
    private static func pageInsets(of view: UIView) -> UIEdgeInsets {
        if #available(iOS 26, *) {
            return view.edgeInsets(for: .safeArea(cornerAdaptation: .vertical))
        }
        return view.safeAreaInsets
    }

    @objc private func keyboardWillChange(_ notification: Notification) {
        guard let info = notification.userInfo, let window = view.window else { return }
        var overlap: CGFloat = 0
        if notification.name != UIResponder.keyboardWillHideNotification,
           let end = (info[UIResponder.keyboardFrameEndUserInfoKey] as? NSValue)?.cgRectValue {
            // The end frame is in the screen's space: converting from the
            // window's puts it off by the window's origin in Split View and
            // Stage Manager, where the window is not at the screen's corner.
            let inContainer = container.convert(end, from: window.screen.coordinateSpace)
            overlap = max(0, container.bounds.maxY - inContainer.minY)
        }
        keyboardOverlap = overlap
        let duration = (info[UIResponder.keyboardAnimationDurationUserInfoKey] as? Double) ?? 0.25
        let curve = (info[UIResponder.keyboardAnimationCurveUserInfoKey] as? UInt) ?? UInt(UIView.AnimationCurve.easeInOut.rawValue)
        UIView.animate(withDuration: duration, delay: 0, options: UIView.AnimationOptions(rawValue: curve << 16)) {
            self.layoutWebView()
        }
    }

    override var preferredStatusBarStyle: UIStatusBarStyle {
        return barLight ? .darkContent : .lightContent
    }

    override var prefersStatusBarHidden: Bool {
        return videoFullscreen
    }

    override var supportedInterfaceOrientations: UIInterfaceOrientationMask {
        if videoLandscapeCue { return .landscape }
        return UIDevice.current.userInterfaceIdiom == .pad ? .all : .allButUpsideDown
    }

    // MARK: - The boot hold

    private var bootField: UIColor { UIColor(named: "BootField") ?? UIColor(red: 0.114, green: 0.310, blue: 0.502, alpha: 1) }
    private var bootFieldDeep: UIColor { UIColor(named: "BootFieldDeep") ?? bootField }

    /// The launch screen's twin is held over the page until the dashboard's
    /// first real screen (the channel method firstScreen), at most 2.5 s
    /// once a page has committed and 8 s with no page; never on a switch,
    /// which shows no launch screen.
    private func installBoot(switching: Bool) {
        bootHeld = !switching
        splashExpected = bootHeld
        bootStart = Date()
        guard bootHeld else { return }
        Self.log.info("boot held")
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.bootHoldMax) { [weak self] in
            guard let self, self.bootHeld, self.pageCommitted else { return }
            self.releaseBoot("deadline")
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.bootHoldCap) { [weak self] in
            self?.releaseBoot("deadline, no page")
        }
    }

    private func releaseBoot(_ why: String) {
        guard bootHeld else { return }
        bootHeld = false
        Self.log.info("boot released (\(why, privacy: .public)) +\(Int(Date().timeIntervalSince(self.bootStart) * 1000))ms")
        tellSplash("leaving")
        UIView.animate(withDuration: Self.bootFade, animations: { self.bootView.alpha = 0 }, completion: { _ in
            self.bootView.removeFromSuperview()
            self.splashGone = true
            self.tellSplash("gone")
        })
    }

    /// The page's first real screen is painted: the hold ends once the web
    /// view had a moment to draw it.
    func firstScreen() {
        guard bootHeld else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.bootVisualWait) { [weak self] in
            self?.releaseBoot("signal")
        }
    }

    /// Where the launch picture is, for the page's boot screen.
    private func tellSplash(_ state: String) {
        guard let webView, showsDashboard(webView.url) else { return }
        webView.evaluateJavaScript("window.__otodockSplash && window.__otodockSplash('\(state)')", completionHandler: nil)
    }

    /// The bar heights for the page's boot screen, which centres its mark on
    /// the screen where the launch picture draws it: pushed when they change
    /// and again when a document first finishes, into the active install's
    /// dashboard only.
    private func pushBarInsets(top: CGFloat, bottom: CGFloat, force: Bool) {
        guard let webView else { return }
        let changed = top != barTop || bottom != barBottom
        barTop = top
        barBottom = bottom
        if (!force && !changed) || !showsDashboard(webView.url) { return }
        let js = String(format: "document.documentElement.style.setProperty('--oto-bar-top','%.1fpx');"
                        + "document.documentElement.style.setProperty('--oto-bar-bottom','%.1fpx')", top, bottom)
        webView.evaluateJavaScript(js, completionHandler: nil)
    }

    /// Whether the main frame shows the active install's dashboard: the
    /// shell's calls into the page go nowhere else.
    func showsDashboard(_ url: URL?) -> Bool {
        guard let activeOrigin, let url else { return false }
        return NativeBridgePolicy.sameOrigin(activeOrigin, url.absoluteString)
    }

    /// The theme's page colour behind the bars (the container, cross-faded),
    /// the web view's own background and the bar icons by luminance. The
    /// boot field a page sends for older apps is what the shell already shows.
    func setContainerColor(_ hex: String) {
        guard let color = UIColor(hex: hex) else { return }
        if color.isEqualRGB(to: bootField) { return }
        DispatchQueue.main.async {
            self.webView?.backgroundColor = color
            self.webView?.scrollView.backgroundColor = color
            self.barLight = color.luminance > 0.5
            UIView.animate(withDuration: Self.containerFade) {
                self.setNeedsStatusBarAppearanceUpdate()
            }
            self.container.setColors(color, color, animated: Self.containerFade)
        }
    }

    /// The bundled setup page follows the device appearance, a change of it
    /// included (the Android shell repaints only at the page's next load).
    override func traitCollectionDidChange(_ previousTraitCollection: UITraitCollection?) {
        super.traitCollectionDidChange(previousTraitCollection)
        guard previousTraitCollection?.userInterfaceStyle != traitCollection.userInterfaceStyle,
              isSetupPage(webView?.url) else { return }
        paintSetupPage()
    }

    private func paintSetupPage() {
        let dark = traitCollection.userInterfaceStyle == .dark
        let page = UIColor(named: dark ? "PageDark" : "PageLight") ?? (dark ? .black : .white)
        setContainerColor(page.hexString)
    }

    // MARK: - Pages: the setup page, the commits, the errors

    /// Load the bundled setup page; a deep link held meanwhile waits for the
    /// next dashboard's dashboardReady.
    func loadSetupPage(query: String) {
        dashboardIsReady = false
        guard let webView else { return }
        let url = Self.localOrigin + Self.setupPagePath + (query.isEmpty ? "" : "?" + query)
        guard let target = URL(string: url) else { return }
        expectingLocalLoad = true
        DispatchQueue.main.async { webView.load(URLRequest(url: target)) }
    }

    /// The one local navigation the shell armed: taken once.
    func takeExpectedLocalLoad() -> Bool {
        let expected = expectingLocalLoad
        expectingLocalLoad = false
        return expected
    }

    /// True when the main frame's URL is the bundled setup page (the asset
    /// handler's origin): a server cannot serve its own /setup.html to reach
    /// the setup-only methods.
    func isSetupPage(_ url: URL?) -> Bool {
        return ChannelJudge.isSetupPage(url?.absoluteString)
    }

    func pageStarted() {
        currentLoadedURL = nil
    }

    func pageCommittedNow() {
        pageCommitted = true
        if bootHeld && Date().timeIntervalSince(bootStart) >= Self.bootHoldMax { releaseBoot("deadline") }
    }

    func pageFinished(_ url: URL?) {
        currentLoadedURL = url?.absoluteString
        pushBarInsets(top: barTop, bottom: barBottom, force: true)
        if !splashExpected || splashGone { tellSplash("gone") }
        if isSetupPage(url) {
            paintSetupPage()
            releaseBoot("setup")
            setupPage.pushInstallations()
        }
        bindInstallId()
    }

    /// A main-frame failure on the active host: the unreachable page with
    /// the picker. A failure elsewhere (a sign-in's sibling host) and a load
    /// cancelled by the shell's own next load are not the install's.
    func mainFrameFailed(_ error: Error, failedURL: URL?) {
        let ns = error as NSError
        if ns.domain == NSURLErrorDomain && ns.code == NSURLErrorCancelled { return }
        if ns.domain == "WebKitErrorDomain" && ns.code == 102 { return }
        guard let active = store.active(), let activeOrigin else { return }
        let failed = (ns.userInfo[NSURLErrorFailingURLErrorKey] as? URL) ?? failedURL
        guard let failed, NativeBridgePolicy.sameOrigin(activeOrigin, failed.absoluteString) else {
            Self.log.warning("off-host main-frame error suppressed: \(ns.code)")
            return
        }
        Self.log.error("main-frame load failed for the active server (\(ns.code)): the setup page")
        loadSetupPage(query: "error=unreachable&url=" + Self.queryEncode(active.url))
    }

    /// Fetch this proxy's install id once signed in and bind it to the active
    /// install (fetched natively, never trusted from the page). A no-op once
    /// bound, or until the person is signed in: retried at each page load.
    private func bindInstallId() {
        guard let active = store.active(), active.installId == nil,
              let current = currentLoadedURL, let activeOrigin,
              NativeBridgePolicy.sameOrigin(activeOrigin, current),
              let url = URL(string: activeOrigin + "/v1/app/identity") else { return }
        let localId = active.id
        WKWebsiteDataStore.default().httpCookieStore.getAllCookies { cookies in
            let mine = cookies.filter { Self.cookieMatches($0, url: url) }
            var request = URLRequest(url: url, timeoutInterval: 8)
            let header = HTTPCookie.requestHeaderFields(with: mine)["Cookie"]
            if let header { request.setValue(header, forHTTPHeaderField: "Cookie") }
            Self.ephemeralSession.dataTask(with: request) { data, response, _ in
                guard let data, (response as? HTTPURLResponse)?.statusCode == 200,
                      let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                      let pid = json["install_id"] as? String, !pid.isEmpty else { return }
                self.store.setInstallId(localId, pid)
            }.resume()
        }
    }

    static func cookieMatches(_ cookie: HTTPCookie, url: URL) -> Bool {
        guard let host = url.host?.lowercased() else { return false }
        let domain = cookie.domain.lowercased()
        let hostMatch = domain == host || (domain.hasPrefix(".") && (host == domain.dropFirst() || host.hasSuffix(domain)))
        if !hostMatch { return false }
        if cookie.isSecure && url.scheme != "https" { return false }
        return url.path.hasPrefix(cookie.path) || cookie.path == "/"
    }

    /// Native requests (the health probe, the identity fetch) share nothing
    /// with WebKit's jar and keep no cookies of their own.
    static let ephemeralSession: URLSession = {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpCookieStorage = nil
        configuration.httpShouldSetCookies = false
        return URLSession(configuration: configuration, delegate: NoRedirects(), delegateQueue: nil)
    }()

    private final class NoRedirects: NSObject, URLSessionTaskDelegate {
        func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                        newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
            completionHandler(nil)
        }
    }

    // MARK: - The channel's calls

    /// One channel call, judged already (NativeChannel), on the main queue in
    /// posting order. `reply` is answered exactly once on every path.
    func dispatchNative(_ data: String, reply: @escaping (Any?) -> Void) {
        if retiring { reply(nil); return }
        // A deep-link switch moves the active pointer before its replacement:
        // a call arriving in between belongs to the page being replaced.
        guard let active = store.active(), NativeBridgePolicy.sameOrigin(activeOrigin, active.url) else { reply(nil); return }
        let parts = NativeBridgePolicy.splitMessage(data)
        guard let headData = parts.head.data(using: .utf8),
              let head = (try? JSONSerialization.jsonObject(with: headData)) as? [String: Any] else {
            Self.log.warning("Native channel refused an unreadable message")
            reply(nil)
            return
        }
        let method = head["m"] as? String ?? ""
        let args = head["a"] as? [Any] ?? []
        let id = head["id"] as? Int ?? 0
        func arg(_ n: Int) -> String? { n < args.count ? args[n] as? String : nil }
        func flag(_ n: Int) -> Bool { n < args.count ? (args[n] as? Bool ?? false) : false }
        func answer(_ value: Any?) { reply(Self.replyJSON(id: id, value: value)) }

        switch method {
        case "setStreaming": setStreaming(flag(0)); reply(nil)
        case "setContainerColor": if let hex = arg(0) { setContainerColor(hex) }; reply(nil)
        case "enterVideoFullscreen": enterVideoFullscreen(landscape: flag(0)); reply(nil)
        case "exitVideoFullscreen": exitVideoFullscreen(); reply(nil)
        case "flushCookies": reply(nil)
        case "dashboardReady": dashboardReady(); reply(nil)
        case "firstScreen": firstScreen(); reply(nil)
        case "setAuthInProgress": setAuthInProgress(flag(0)); reply(nil)
        case "setSwitchBusy": switchBusyFlag = flag(0); reply(nil)
        case "openAuthBrowser": openAuthBrowser(arg(0), kind: arg(1)) { answer($0) }
        case "saveImageFromBase64": downloads.saveBase64(parts.body, name: arg(0), mime: arg(1)); reply(nil)
        case "getInstallations": answer(Self.jsonValue(store.publicJSON()))
        case "setFavorite": if let id = arg(0) { store.setFavorite(id) }; reply(nil)
        case "renameInstallation": if let id = arg(0) { store.rename(id, arg(1)) }; reply(nil)
        case "switchInstallation": switchInstallation(arg(0)); reply(nil)
        case "switchToInstall": answer(switchToInstall(arg(0), clickUrl: arg(1)))
        case "openAddInstallation": loadSetupPage(query: "mode=add"); reply(nil)
        case "openPrivacyPolicy": openPrivacyPolicy(); reply(nil)
        case "getBatteryAdvice": answer(false)
        case "openBatterySettings": reply(nil)
        case "requestStoreReview": Task { answer(await requestStoreReview()) }
        case "removeInstallation": removeInstallation(arg(0)); reply(nil)
        case "disconnectServer": disconnectServer(); reply(nil)
        case "foldNote": reply(nil)
        default:
            Self.log.warning("Native channel: unknown method \(method, privacy: .public)")
            reply(nil)
        }
    }

    /// A replying call's answer, `{"id": n, "r": value}`, or nil for a call
    /// with no id.
    static func replyJSON(id: Int, value: Any?) -> String? {
        guard id > 0 else { return nil }
        let object: [String: Any] = ["id": id, "r": value ?? NSNull()]
        guard let data = try? JSONSerialization.data(withJSONObject: object), let text = String(data: data, encoding: .utf8) else { return nil }
        return text
    }

    private static func jsonValue(_ text: String) -> Any? {
        guard let data = text.data(using: .utf8) else { return nil }
        return try? JSONSerialization.jsonObject(with: data)
    }

    // MARK: - Streaming in the background

    private func setStreaming(_ streaming: Bool) {
        isStreaming = streaming
        if !streaming { endBackgroundTask() }
    }

    func sceneDidEnterBackground() {
        guard isStreaming, backgroundTask == .invalid else { return }
        backgroundTask = UIApplication.shared.beginBackgroundTask(withName: "OtoDock streaming") { [weak self] in
            self?.endBackgroundTask()
        }
    }

    func sceneDidBecomeActive() {
        endBackgroundTask()
        PushBridge.shared.sweepTurnComplete()
        // The socket's health check: a short lock can leave the socket dead
        // with no visibility change the page saw.
        webView?.evaluateJavaScript("window.dispatchEvent(new Event('otodock:force-health-check'));", completionHandler: nil)
    }

    private func endBackgroundTask() {
        guard backgroundTask != .invalid else { return }
        UIApplication.shared.endBackgroundTask(backgroundTask)
        backgroundTask = .invalid
    }

    // MARK: - Video fullscreen

    /// Edge to edge, the status bar hidden, and on iPhone a landscape cue for
    /// a wide video, released once the person holds the phone in landscape
    /// (iPadOS keeps its own rotation under multitasking).
    private func enterVideoFullscreen(landscape: Bool) {
        DispatchQueue.main.async {
            self.videoFullscreen = true
            self.videoLandscapeCue = landscape && UIDevice.current.userInterfaceIdiom == .phone
            self.layoutWebView()
            self.setNeedsStatusBarAppearanceUpdate()
            self.applyOrientations()
        }
    }

    private func exitVideoFullscreen() {
        DispatchQueue.main.async {
            self.videoFullscreen = false
            self.videoLandscapeCue = false
            self.layoutWebView()
            self.setNeedsStatusBarAppearanceUpdate()
            self.applyOrientations()
        }
    }

    @objc private func deviceOrientationChanged() {
        guard videoLandscapeCue, UIDevice.current.orientation.isLandscape else { return }
        videoLandscapeCue = false
        applyOrientations()
    }

    private func applyOrientations() {
        setNeedsUpdateOfSupportedInterfaceOrientations()
        guard videoLandscapeCue, let scene = view.window?.windowScene else { return }
        scene.requestGeometryUpdate(.iOS(interfaceOrientations: .landscape)) { _ in }
    }

    // MARK: - Deep links and the sign-in flow

    private static let pendingDeepLinkKey = "otodock_prefs.pending_deeplink"

    /// The SPA signals it has mounted; deliver any deep link held for it.
    private func dashboardReady() {
        dashboardIsReady = true
        let defaults = UserDefaults.standard
        if let pending = defaults.string(forKey: Self.pendingDeepLinkKey) {
            defaults.removeObject(forKey: Self.pendingDeepLinkKey)
            if let url = URL(string: pending) { deliverDeepLink(url) }
        }
    }

    /// An otodock:// link from the sign-in the app opened: taken only while a
    /// flow of its kind is in flight, for the install that started it.
    func handleDeepLink(_ url: URL) {
        guard let target = deepLinkTarget(url) else {
            Self.log.warning("Dropped an otodock:// link: no sign-in in progress")
            return
        }
        let defaults = UserDefaults.standard
        guard let active = store.active(), target.id == active.id else {
            defaults.set(url.absoluteString, forKey: Self.pendingDeepLinkKey)
            store.setActive(target.id)
            switchRecreate()
            return
        }
        if dashboardIsReady {
            deliverDeepLink(url)
        } else {
            defaults.set(url.absoluteString, forKey: Self.pendingDeepLinkKey)
        }
    }

    private func deliverDeepLink(_ url: URL) {
        guard let webView, let active = store.active(), let activeOrigin else { return }
        guard let target = deepLinkTarget(url), target.id == active.id else {
            Self.log.warning("Dropped an otodock:// link: no sign-in in progress")
            return
        }
        let components = URLComponents(url: url, resolvingAgainstBaseURL: false)
        let query = components?.queryItems ?? []
        if url.host == "auth" && url.path == "/callback" {
            if let code = query.first(where: { $0.name == "code" })?.value, let state = query.first(where: { $0.name == "state" })?.value {
                var callback = URLComponents(string: activeOrigin + "/auth/callback")
                callback?.queryItems = [URLQueryItem(name: "code", value: code), URLQueryItem(name: "state", value: state)]
                if let callbackURL = callback?.url {
                    currentLoadedURL = nil
                    webView.load(URLRequest(url: callbackURL))
                }
            }
        } else if url.host == "oauth" {
            let quoted = Self.jsQuote(url.absoluteString)
            webView.evaluateJavaScript("window._handleDeepLink && window._handleDeepLink(\(quoted))", completionHandler: nil)
        }
        flow.clear()
        authInProgress = false
    }

    private func deepLinkTarget(_ url: URL) -> InstallStore.Install? {
        guard let kind = AuthFlow.linkKind(ofHost: url.host) else { return nil }
        let tag = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.first { $0.name == "install" }?.value
        let tagged = tag.flatMap { store.byInstallId($0) }
        guard let id = flow.target(linkKind: kind, taggedId: tagged?.id) else { return nil }
        return store.byId(id)
    }

    /// Open an https address in the system's sign-in browser for a sign-in
    /// (`auth`) or a connect (`oauth`); the system names the site itself.
    private func openAuthBrowser(_ url: String?, kind: String?, reply: @escaping (Bool) -> Void) {
        guard let target = NativeBridgePolicy.authBrowserUrl(url), let kind, NativeBridgePolicy.isFlowKind(kind),
              let active = store.active(), let targetURL = URL(string: target) else {
            reply(false)
            return
        }
        // One session at a time: a second request while the sheet is up is
        // refused before anything is armed, so the running sign-in keeps its
        // flow and its gate and its result still lands.
        guard flow.arm(installId: active.id, kind: kind, unlessOpen: AuthBrowser.shared.isOpen) else {
            Self.log.info("sign-in browser already up: a second request refused")
            reply(false)
            return
        }
        beginAuth()
        // The session outlives this controller: its result goes to the shell
        // that is current when it ends (AuthBrowser), since a notification
        // tap can replace this one while the sheet is up.
        let started = AuthBrowser.shared.open(targetURL)
        if !started {
            flow.clear()
            authInProgress = false
        }
        reply(started)
    }

    /// The sign-in browser closed with no result: the flow and the switch
    /// gate clear, and the page's own waiter ends at its timeout.
    func signInBrowserClosed() {
        Self.log.info("sign-in browser closed with no result")
        flow.clear()
        authInProgress = false
    }

    private func setAuthInProgress(_ active: Bool) {
        if active { beginAuth() } else { authInProgress = false }
    }

    private func beginAuth() {
        authInProgress = true
        authInProgressUntil = MonotonicClock.nowMs() + 5 * 60 * 1000
    }

    private func authActive() -> Bool {
        return authInProgress && MonotonicClock.nowMs() < authInProgressUntil
    }

    /// The privacy policy in the system browser, at most once in two seconds.
    func openPrivacyPolicy() {
        DispatchQueue.main.async {
            let now = MonotonicClock.nowMs()
            if self.privacyPolicyOpenedAt != 0 && now - self.privacyPolicyOpenedAt < 2000 { return }
            self.privacyPolicyOpenedAt = now
            guard let url = URL(string: NativeBridgePolicy.privacyPolicyURL) else { return }
            UIApplication.shared.open(url)
        }
    }

    /// The App Store's own review sheet, handed the active window scene. True
    /// only in an App Store build: a DEBUG build answers false (Apple shows the
    /// sheet every time there), TestFlight reads `.sandbox` and never shows it,
    /// and an unknown environment answers false, so a tester never spends a
    /// person's two asks. Apple decides whether the sheet shows (at most three
    /// times in 365 days) and never says.
    private func requestStoreReview() async -> Bool {
        #if DEBUG
        Self.log.info("store review not handed over: debug build")
        return false
        #else
        guard case .verified(let transaction) = try? await AppTransaction.shared,
              transaction.environment == .production else {
            Self.log.info("store review not handed over: not an App Store build")
            return false
        }
        guard let scene = view.window?.windowScene, scene.activationState == .foregroundActive else {
            Self.log.info("store review not handed over: no active window")
            return false
        }
        AppStore.requestReview(in: scene)
        Self.log.info("store review handed over")
        return true
        #endif
    }

    // MARK: - Installations and the switch

    private func switchInstallation(_ id: String?) {
        guard let id, !authActive(), store.byId(id) != nil, id != store.activeIdValue else { return }
        if switchIsBusy() {
            confirmNative(title: "Switch installation", message: "You have an active session here that will end. Switch anyway?") {
                self.captureRouteThenSwitch(id)
            }
        } else {
            captureRouteThenSwitch(id)
        }
    }

    /// Route a notification tap to the install it belongs to; true only when
    /// it switched (the source is a known, different install).
    private func switchToInstall(_ installId: String?, clickUrl: String?) -> Bool {
        guard let target = store.byInstallId(installId), let active = store.active(), target.id != active.id else { return false }
        store.setLastRoute(target.id, Routes.sanitizeClickUrl(clickUrl))
        store.setActive(target.id)
        switchRecreate()
        return true
    }

    private func removeInstallation(_ id: String?) {
        guard let id, store.byId(id) != nil else { return }
        confirmNative(title: "Remove installation", message: "Remove this server from the app? You'll need to add it again to reconnect.") {
            self.doRemoveInstall(id)
        }
    }

    private func disconnectServer() {
        guard let id = store.activeIdValue else { return }
        confirmNative(title: "Disconnect", message: "Disconnect from this server? You'll need to add it again to reconnect.") {
            self.doRemoveInstall(id)
        }
    }

    /// Remove an install: its host's cookies go (best effort), every cookie
    /// and site datum when none remains.
    private func doRemoveInstall(_ id: String) {
        let removedURL = store.byId(id)?.url
        store.remove(id)
        let dataStore = WKWebsiteDataStore.default()
        if store.active() == nil {
            dataStore.removeData(ofTypes: WKWebsiteDataStore.allWebsiteDataTypes(), modifiedSince: .distantPast) {
                HTTPCookieStorage.shared.removeCookies(since: .distantPast)
                self.switchRecreate()
            }
        } else {
            let host = InstallStore.hostOf(removedURL)
            dataStore.httpCookieStore.getAllCookies { cookies in
                let group = DispatchGroup()
                for cookie in cookies where host != nil && Self.cookieDomainMatches(cookie.domain, host: host!) {
                    group.enter()
                    dataStore.httpCookieStore.delete(cookie) { group.leave() }
                }
                group.notify(queue: .main) { self.switchRecreate() }
            }
        }
    }

    private static func cookieDomainMatches(_ domain: String, host: String) -> Bool {
        let d = domain.lowercased()
        return d == host || d == "." + host || (d.hasPrefix(".") && host.hasSuffix(d))
    }

    /// Live work on this page the dashboard reported (a live meeting or voice
    /// conversation in the chat). A generating chat is not that: its turn runs on
    /// the server and the chat picks it up again, so streaming never blocks a
    /// switch.
    private func switchIsBusy() -> Bool {
        return switchBusyFlag
    }

    /// Capture the current install's in-app route (so it reopens there) and switch.
    private func captureRouteThenSwitch(_ targetId: String) {
        guard let webView, let current = store.activeIdValue else {
            store.setActive(targetId)
            switchRecreate()
            return
        }
        webView.evaluateJavaScript("(location.pathname + location.search)") { value, _ in
            if let route = value as? String, Routes.isSafeRoutePath(route), !route.contains("setup") {
                self.store.setLastRoute(current, route)
            }
            self.store.setActive(targetId)
            self.switchRecreate()
        }
    }

    /// Switch to the next or previous install in list order (wraps).
    private func cycleInstall(_ direction: Int) {
        if videoFullscreen || authActive() { return }
        if switchIsBusy() {
            Toast.show("Finish the current activity before switching", in: view.window)
            return
        }
        let list = store.list()
        guard list.count >= 2, let active = store.activeIdValue, let index = list.firstIndex(where: { $0.id == active }) else { return }
        let target = list[(index + direction + list.count) % list.count]
        if target.id == active { return }
        captureRouteThenSwitch(target.id)
    }

    /// Add (or reuse) an install for this URL, make it active, and switch to it.
    func commitInstall(_ url: String) {
        let target = store.add(url: url, label: InstallStore.hostOf(url))
        store.setActive(target.id)
        switchRecreate()
    }

    /// Replace this controller with a new one for the active install: the
    /// twin of Android's recreate(). This one lets its web view go first.
    func switchRecreate() {
        Self.switchInProgress = true
        DispatchQueue.main.async {
            guard let window = self.view.window else { return }
            if let active = self.store.active() { Toast.show("Switching to \(active.label)", in: window) }
            self.retire()
            let next = ShellViewController()
            UIView.transition(with: window, duration: 0.15, options: .transitionCrossDissolve, animations: {
                window.rootViewController = next
            })
        }
    }

    private func retire() {
        retiring = true
        guard let webView else { return }
        let content = webView.configuration.userContentController
        content.removeAllScriptMessageHandlers()
        content.removeAllUserScripts()
        webView.navigationDelegate = nil
        webView.uiDelegate = nil
        webView.stopLoading()
        webView.loadHTMLString("", baseURL: nil)
    }

    // MARK: - The three-finger swipe

    private func installSwipes(on webView: WKWebView) {
        for (direction, step) in [(UISwipeGestureRecognizer.Direction.left, 1), (.right, -1)] {
            let swipe = UISwipeGestureRecognizer(target: self, action: step > 0 ? #selector(swipedLeft) : #selector(swipedRight))
            swipe.direction = direction
            swipe.numberOfTouchesRequired = 3
            swipe.cancelsTouchesInView = false
            swipe.delegate = self
            webView.addGestureRecognizer(swipe)
        }
    }

    @objc private func swipedLeft() { cycleInstall(1) }
    @objc private func swipedRight() { cycleInstall(-1) }

    // MARK: - Dialogs

    /// A native confirm answered exactly once; Cancel and a dismissal answer no.
    func confirmNative(title: String, message: String, onYes: @escaping () -> Void) {
        confirmNative(title: title, message: message) { if $0 { onYes() } }
    }

    func confirmNative(title: String, message: String, result: @escaping (Bool) -> Void) {
        DispatchQueue.main.async {
            var answered = false
            let once: (Bool) -> Void = { value in
                if !answered { answered = true; result(value) }
            }
            guard self.view.window != nil, !self.retiring else { once(false); return }
            let alert = UIAlertController(title: title, message: message, preferredStyle: .alert)
            alert.addAction(UIAlertAction(title: "Cancel", style: .cancel) { _ in once(false) })
            alert.addAction(UIAlertAction(title: "Confirm", style: .default) { _ in once(true) })
            (self.presentedViewController ?? self).present(alert, animated: true)
        }
    }

    private func showBlockedDialog(_ message: String) {
        let alert = UIAlertController(title: "OtoDock", message: message, preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "OK", style: .default))
        present(alert, animated: true)
    }

    // MARK: - Small helpers

    static func queryEncode(_ text: String) -> String {
        return text.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? ""
    }

    /// A JavaScript string literal: JSON's quoting, which never breaks out of
    /// a script string.
    static func jsQuote(_ text: String) -> String {
        guard let data = try? JSONSerialization.data(withJSONObject: [text]), let array = String(data: data, encoding: .utf8) else { return "\"\"" }
        return String(array.dropFirst().dropLast())
    }
}

extension ShellViewController: UIGestureRecognizerDelegate {
    func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer,
                           shouldRecognizeSimultaneouslyWith otherGestureRecognizer: UIGestureRecognizer) -> Bool {
        return true
    }
}

/// The container behind the web view: a vertical gradient (the boot field
/// to its deep end, then the page colour), cross-faded on a change.
final class ContainerView: UIView {
    override class var layerClass: AnyClass { CAGradientLayer.self }

    private var gradient: CAGradientLayer { layer as! CAGradientLayer }

    func setColors(_ top: UIColor, _ bottom: UIColor, animated duration: TimeInterval = 0) {
        let colors = [top.cgColor, bottom.cgColor]
        if duration > 0 {
            let fade = CABasicAnimation(keyPath: "colors")
            fade.fromValue = gradient.colors
            fade.toValue = colors
            fade.duration = duration
            gradient.add(fade, forKey: "colors")
        }
        gradient.colors = colors
    }
}

extension UIColor {
    /// `#rrggbb`, else nil.
    convenience init?(hex: String) {
        var text = hex.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.hasPrefix("#") { text.removeFirst() }
        guard text.count == 6, let value = UInt32(text, radix: 16) else { return nil }
        self.init(red: CGFloat((value >> 16) & 0xff) / 255, green: CGFloat((value >> 8) & 0xff) / 255,
                  blue: CGFloat(value & 0xff) / 255, alpha: 1)
    }

    var hexString: String {
        var r: CGFloat = 0, g: CGFloat = 0, b: CGFloat = 0, a: CGFloat = 0
        getRed(&r, green: &g, blue: &b, alpha: &a)
        return String(format: "#%02x%02x%02x", Int(round(r * 255)), Int(round(g * 255)), Int(round(b * 255)))
    }

    /// The relative luminance, as the Android shell computes it for the bar icons.
    var luminance: CGFloat {
        var r: CGFloat = 0, g: CGFloat = 0, b: CGFloat = 0, a: CGFloat = 0
        getRed(&r, green: &g, blue: &b, alpha: &a)
        func channel(_ c: CGFloat) -> CGFloat { c <= 0.03928 ? c / 12.92 : pow((c + 0.055) / 1.055, 2.4) }
        return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
    }

    func isEqualRGB(to other: UIColor) -> Bool {
        return hexString == other.hexString
    }
}
