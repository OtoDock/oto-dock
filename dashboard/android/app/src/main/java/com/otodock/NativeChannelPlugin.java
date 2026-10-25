package com.otodock;

import com.getcapacitor.Plugin;
import com.getcapacitor.annotation.CapacitorPlugin;

/**
 * Capacitor builds this with the app's plugins, after its core plugins (which
 * add their interfaces to the WebView) and before the Bridge's first page load:
 * the one point where the bridge surface can be settled before any document
 * exists. It exposes nothing to pages (no plugin methods).
 */
@CapacitorPlugin(name = "OtoDockNativeChannel")
public class NativeChannelPlugin extends Plugin {

    @Override
    public void load() {
        if (getActivity() instanceof MainActivity) {
            ((MainActivity) getActivity()).onBridgeWebViewReady(getBridge().getWebView());
        }
    }
}
