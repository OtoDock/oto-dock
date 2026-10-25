package com.otodock;

import android.app.KeyguardManager;
import android.content.Context;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageManager;
import android.content.res.Configuration;
import android.hardware.display.DisplayManager;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.os.SystemClock;
import android.util.DisplayMetrics;
import android.util.Log;
import android.view.Display;
import android.view.View;
import android.view.ViewTreeObserver;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.WebView;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;
import androidx.lifecycle.DefaultLifecycleObserver;
import androidx.lifecycle.LifecycleOwner;
import androidx.webkit.WebViewCompat;
import androidx.webkit.WebViewFeature;
import androidx.webkit.WebViewRenderProcess;
import androidx.webkit.WebViewRenderProcessClient;
import com.getcapacitor.BridgeActivity;
import com.getcapacitor.WebViewListener;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;
import java.util.function.Supplier;

/**
 * The fold log (logcat tag {@code OtoDockFold}): one line per event around a
 * display switch, so a foldable's black front screen can be read from a
 * capture instead of guessed at. The manifest's configChanges keeps the
 * activity alive across a fold, so the events are the configuration change,
 * the frames that follow it, the displays' changes, the window focus, the
 * lifecycle and the WebView's renderer; the page adds its own lines through
 * the channel ({@link #page}). Every line is {@code Log.i} or {@code Log.w}:
 * phones of Huawei's line hide an app's debug level. The lines cost a few
 * per configuration change and ship in every build; the display listener,
 * which runs on every display change (an LTPO panel's refresh-rate switches
 * among them), is registered only where it can matter: a debuggable build,
 * or a phone with a hinge.
 */
final class FoldDiagnostics {

    static final String TAG = "OtoDockFold";
    /** A page's note is one short sentence; the rest is cut. */
    static final int MAX_PAGE_CHARS = 200;
    private static final long NO_FRAME_WATCHDOG_MS = 10_000L;

    private final BridgeActivity activity;
    private final WebView webView;
    private final Supplier<String> flags;
    private final DisplayManager displayManager;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Map<Integer, FoldSnapshot> lastByDisplay = new HashMap<>();
    private Configuration lastConfig;
    private long configChangedAt = 0L;
    private boolean frameSeen = true;
    private long visualRequest = 0L;
    private boolean displaysWatched = false;

    private final DisplayManager.DisplayListener displayListener = new DisplayManager.DisplayListener() {
        @Override
        public void onDisplayAdded(int displayId) {
            logDisplay("added", displayId);
        }

        @Override
        public void onDisplayRemoved(int displayId) {
            lastByDisplay.remove(displayId);
            Log.i(TAG, "display " + displayId + " removed");
        }

        @Override
        public void onDisplayChanged(int displayId) {
            logDisplay("changed", displayId);
        }
    };

    private final DefaultLifecycleObserver lifecycle = new DefaultLifecycleObserver() {
        @Override
        public void onStart(LifecycleOwner owner) {
            lifecycleLine("start");
        }

        @Override
        public void onResume(LifecycleOwner owner) {
            lifecycleLine("resume (the page gets force-health-check)");
        }

        @Override
        public void onPause(LifecycleOwner owner) {
            lifecycleLine("pause");
        }

        @Override
        public void onStop(LifecycleOwner owner) {
            lifecycleLine("stop");
        }
    };

    private final View.OnAttachStateChangeListener attachListener = new View.OnAttachStateChangeListener() {
        @Override
        public void onViewAttachedToWindow(View v) {
            Log.i(TAG, "webview attached to window");
        }

        @Override
        public void onViewDetachedFromWindow(View v) {
            Log.i(TAG, "webview detached from window");
        }
    };

    private FoldDiagnostics(BridgeActivity activity, WebView webView, Supplier<String> flags) {
        this.activity = activity;
        this.webView = webView;
        this.flags = flags;
        this.displayManager = (DisplayManager) activity.getSystemService(Context.DISPLAY_SERVICE);
        this.lastConfig = new Configuration(activity.getResources().getConfiguration());
    }

    /** Attached once the bridge's WebView exists; {@code savedState} says
     *  whether the activity was recreated with a saved state. */
    static FoldDiagnostics attach(BridgeActivity activity, WebView webView, boolean savedState,
                                  Supplier<String> flags) {
        FoldDiagnostics d = new FoldDiagnostics(activity, webView, flags);
        d.displaysWatched = d.displayManager != null && watchesDisplays(activity);
        Log.i(TAG, "created saved=" + savedState + " displays=" + (d.displaysWatched ? "watched" : "unwatched")
                + " " + d.windowLine() + " " + flags.get());
        if (d.displaysWatched) {
            d.displayManager.registerDisplayListener(d.displayListener, d.handler);
            for (Display display : d.displayManager.getDisplays()) {
                d.logDisplay("present", display.getDisplayId());
            }
        }
        activity.getLifecycle().addObserver(d.lifecycle);
        webView.addOnAttachStateChangeListener(d.attachListener);
        activity.getBridge().addWebViewListener(new WebViewListener() {
            @Override
            public boolean onRenderProcessGone(WebView view, RenderProcessGoneDetail detail) {
                boolean crash = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && detail.didCrash();
                Log.w(TAG, "webview renderer gone crash=" + crash);
                return false;
            }
        });
        if (WebViewFeature.isFeatureSupported(WebViewFeature.WEB_VIEW_RENDERER_CLIENT_BASIC_USAGE)) {
            try {
                WebViewCompat.setWebViewRenderProcessClient(webView, new WebViewRenderProcessClient() {
                    @Override
                    public void onRenderProcessUnresponsive(WebView view, WebViewRenderProcess renderer) {
                        Log.w(TAG, "webview renderer unresponsive");
                    }

                    @Override
                    public void onRenderProcessResponsive(WebView view, WebViewRenderProcess renderer) {
                        Log.i(TAG, "webview renderer responsive again");
                    }
                });
            } catch (RuntimeException e) {
                Log.w(TAG, "no renderer client: " + e.getMessage());
            }
        }
        return d;
    }

    /** A debuggable build, or a phone with a hinge: where a display change
     *  is worth a line. */
    private static boolean watchesDisplays(BridgeActivity activity) {
        boolean debuggable = (activity.getApplicationInfo().flags & ApplicationInfo.FLAG_DEBUGGABLE) != 0;
        return debuggable || activity.getPackageManager().hasSystemFeature(PackageManager.FEATURE_SENSOR_HINGE_ANGLE);
    }

    /** From the activity's onDestroy, with its reason: an install switch
     *  recreates with {@code changingConfigurations} true and an empty mask,
     *  so it is named apart. */
    void detach(boolean changingConfigurations, int changingConfigurationsMask, boolean switching) {
        Log.i(TAG, String.format(Locale.ROOT, "destroyed changingConfigurations=%b mask=0x%x switching=%b",
                changingConfigurations, changingConfigurationsMask, switching));
        handler.removeCallbacksAndMessages(null);
        if (displaysWatched) displayManager.unregisterDisplayListener(displayListener);
        activity.getLifecycle().removeObserver(lifecycle);
        webView.removeOnAttachStateChangeListener(attachListener);
    }

    /** The configuration change (a fold among them): the diff against the
     *  previous one, the new metrics, the window and the WebView as they are
     *  BEFORE the layout pass, then the frames that follow. */
    void onConfigurationChanged(Configuration newConfig) {
        int diff = lastConfig.diff(newConfig);
        lastConfig = new Configuration(newConfig);
        configChangedAt = SystemClock.uptimeMillis();
        frameSeen = false;
        int night = newConfig.uiMode & Configuration.UI_MODE_NIGHT_MASK;
        Log.i(TAG, String.format(Locale.ROOT,
                "config diff=0x%x orientation=%d %dx%ddp smallest=%ddp dpi=%d layoutSize=%d night=%s %s %s",
                diff, newConfig.orientation, newConfig.screenWidthDp, newConfig.screenHeightDp,
                newConfig.smallestScreenWidthDp, newConfig.densityDpi,
                newConfig.screenLayout & Configuration.SCREENLAYOUT_SIZE_MASK,
                night == Configuration.UI_MODE_NIGHT_YES ? "yes" : "no",
                windowLine(), flags.get()));
        armFrameWatch();
    }

    private void armFrameWatch() {
        final long at = configChangedAt;
        View decor = activity.getWindow().getDecorView();
        ViewTreeObserver observer = decor.getViewTreeObserver();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            // The first frame submitted to the compositor after the change
            // (a draw listener fires when a frame is recorded, not shown).
            observer.registerFrameCommitCallback(() -> frameLine("frame", at));
        } else {
            observer.addOnDrawListener(new ViewTreeObserver.OnDrawListener() {
                @Override
                public void onDraw() {
                    frameLine("draw", at);
                    handler.post(() -> {
                        try { decor.getViewTreeObserver().removeOnDrawListener(this); } catch (RuntimeException ignored) {}
                    });
                }
            });
        }
        // The WebView's own "ready to draw": a frame that reflects the page.
        // It fires only while the WebView is attached and visible.
        final long request = ++visualRequest;
        try {
            webView.postVisualStateCallback(request, new WebView.VisualStateCallback() {
                @Override
                public void onComplete(long id) {
                    // Only the latest change's: an earlier one still pending
                    // would log against its own, older, start.
                    if (id == visualRequest) Log.i(TAG, "visual +" + (SystemClock.uptimeMillis() - at) + "ms");
                }
            });
        } catch (RuntimeException ignored) {}
        handler.postDelayed(() -> {
            if (!frameSeen && configChangedAt == at && webView.getWindowVisibility() == View.VISIBLE) {
                Log.w(TAG, "no frame within " + (NO_FRAME_WATCHDOG_MS / 1000) + " s while visible " + windowLine());
            }
        }, NO_FRAME_WATCHDOG_MS);
    }

    private void frameLine(String what, long since) {
        // A frame left over from an earlier change does not answer this one.
        if (since == configChangedAt) frameSeen = true;
        View decor = activity.getWindow().getDecorView();
        View container = webView.getParent() instanceof View ? (View) webView.getParent() : null;
        String keyboard = "?";
        WindowInsetsCompat insets = ViewCompat.getRootWindowInsets(decor);
        if (insets != null) {
            keyboard = insets.isVisible(WindowInsetsCompat.Type.ime())
                    ? String.valueOf(insets.getInsets(WindowInsetsCompat.Type.ime()).bottom) : "hidden";
        }
        Log.i(TAG, String.format(Locale.ROOT,
                "%s +%dms webview=%dx%d decorPad=%d/%d/%d/%d containerPad=%d/%d/%d/%d keyboard=%s",
                what, SystemClock.uptimeMillis() - since, webView.getWidth(), webView.getHeight(),
                decor.getPaddingLeft(), decor.getPaddingTop(), decor.getPaddingRight(), decor.getPaddingBottom(),
                container != null ? container.getPaddingLeft() : -1, container != null ? container.getPaddingTop() : -1,
                container != null ? container.getPaddingRight() : -1, container != null ? container.getPaddingBottom() : -1,
                keyboard));
    }

    void onWindowFocusChanged(boolean hasFocus) {
        Log.i(TAG, "focus " + hasFocus + " " + windowLine());
    }

    void onTopResumedActivityChanged(boolean isTopResumed) {
        Log.i(TAG, "topResumed " + isTopResumed);
    }

    void onTrimMemory(int level) {
        Log.i(TAG, "trimMemory level=" + level);
    }

    /** The cookie flush that follows a focus loss, timed off the UI thread. */
    static void flushed(long queuedAt, long startedAt, long doneAt) {
        Log.i(TAG, "cookie flush waited " + (startedAt - queuedAt) + "ms took " + (doneAt - startedAt) + "ms");
    }

    /** The page's own line, through the channel: one line (every control
     *  character becomes a space), cut at the cap by code points, so a pair
     *  is never split. */
    void page(String text) {
        if (text == null) return;
        int codePoints = text.codePointCount(0, text.length());
        String t = codePoints > MAX_PAGE_CHARS ? text.substring(0, text.offsetByCodePoints(0, MAX_PAGE_CHARS)) : text;
        StringBuilder sb = new StringBuilder(t.length());
        for (int i = 0; i < t.length(); i++) {
            char c = t.charAt(i);
            sb.append(Character.isISOControl(c) ? ' ' : c);
        }
        Log.i(TAG, "page: " + sb);
    }

    private void lifecycleLine(String what) {
        Log.i(TAG, "lifecycle " + what + " " + flags.get());
    }

    /** One display's line, only when a compared field changed since its last. */
    private void logDisplay(String event, int displayId) {
        if (displayManager == null) return;
        Display display = displayManager.getDisplay(displayId);
        if (display == null) return;
        FoldSnapshot now = snapshot(display);
        FoldSnapshot last = lastByDisplay.get(displayId);
        if (now.sameAs(last)) return;
        lastByDisplay.put(displayId, now);
        Log.i(TAG, event + " " + now.describe() + (displayId == activityDisplayId() ? " (the activity's)" : ""));
    }

    @SuppressWarnings("deprecation")
    private static FoldSnapshot snapshot(Display display) {
        DisplayMetrics metrics = new DisplayMetrics();
        display.getRealMetrics(metrics);
        Display.Mode mode = display.getMode();
        return new FoldSnapshot(display.getDisplayId(), display.getState(), display.getRotation(),
                mode.getPhysicalWidth(), mode.getPhysicalHeight(), metrics.densityDpi, display.getFlags(),
                mode.getRefreshRate());
    }

    @SuppressWarnings("deprecation")
    private int activityDisplayId() {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                Display d = activity.getDisplay();
                return d != null ? d.getDisplayId() : -1;
            }
            return activity.getWindowManager().getDefaultDisplay().getDisplayId();
        } catch (RuntimeException e) {
            return -1;
        }
    }

    /** The window, the system's state and the WebView, at this moment. */
    private String windowLine() {
        StringBuilder sb = new StringBuilder();
        sb.append("display=").append(activityDisplayId());
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            try {
                sb.append(" bounds=").append(activity.getWindowManager().getCurrentWindowMetrics().getBounds().toShortString());
            } catch (RuntimeException ignored) {}
        }
        PowerManager pm = (PowerManager) activity.getSystemService(Context.POWER_SERVICE);
        KeyguardManager km = (KeyguardManager) activity.getSystemService(Context.KEYGUARD_SERVICE);
        sb.append(" interactive=").append(pm != null && pm.isInteractive());
        sb.append(" locked=").append(km != null && km.isKeyguardLocked());
        sb.append(" multiWindow=").append(activity.isInMultiWindowMode());
        sb.append(" webview=").append(webView.getWidth()).append('x').append(webView.getHeight())
                .append(" vis=").append(webView.getVisibility())
                .append(" shown=").append(webView.isShown())
                .append(" winVis=").append(webView.getWindowVisibility());
        return sb.toString();
    }
}
