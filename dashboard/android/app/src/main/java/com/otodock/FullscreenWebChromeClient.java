package com.otodock;

import android.view.View;
import com.getcapacitor.Bridge;
import com.getcapacitor.BridgeWebChromeClient;

/**
 * Capacitor's WebChromeClient with full screen done as Android documents it:
 * the WebView hands over a view, the activity shows it above the WebView and
 * keeps the callback, and the view goes when the WebView says so. Everything
 * else (file choosers, permissions, dialogs, the console) stays Capacitor's.
 *
 * The WebView offers full screen to a page only when the client's class
 * declares both methods below: it looks for them by name. They never call
 * super: Capacitor's onShowCustomView answers the callback at once, which
 * ends the full screen it was just asked for.
 */
final class FullscreenWebChromeClient extends BridgeWebChromeClient {

    /** The activity, which owns the view's place, the bars and Back. */
    interface Host {
        void showCustomView(View view, CustomViewCallback callback);

        void hideCustomView();
    }

    private final Host host;

    FullscreenWebChromeClient(Bridge bridge, Host host) {
        super(bridge);
        this.host = host;
    }

    @Override
    public void onShowCustomView(View view, CustomViewCallback callback) {
        host.showCustomView(view, callback);
    }

    @Override
    public void onHideCustomView() {
        host.hideCustomView();
    }
}
