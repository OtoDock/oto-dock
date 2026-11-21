package com.otodock;

/**
 * Who holds the screen for a full screen video: the WebView's own full
 * screen view (the activity shows it above the WebView), or the page, which
 * asks through the channel. The bars are hidden while either does. Pure Java
 * (no android.*), so the transitions are unit-tested on the JVM; the activity
 * uses it on the UI thread only.
 *
 * Each method returns whether {@link #on()} changed, so the caller touches
 * the bars only on a real change: every dashboard also calls the channel
 * beside a shown view, and those calls must change nothing.
 */
final class VideoFullscreen {

    private boolean view;
    private boolean page;

    /** True while the bars are hidden for a video. */
    boolean on() {
        return view || page;
    }

    /** The WebView's full screen view went up. */
    boolean viewShown() {
        boolean was = on();
        view = true;
        return was != on();
    }

    /** The WebView left full screen. The page's request ends with it, so the
     *  bars return in the same pass and the page's own exit, which arrives
     *  later through the channel, finds nothing to do. */
    boolean viewHidden() {
        boolean was = on();
        view = false;
        page = false;
        return was != on();
    }

    /** The page asked for the bars to go (enterVideoFullscreen). */
    boolean pageEntered() {
        boolean was = on();
        page = true;
        return was != on();
    }

    /** The page gave the bars back (exitVideoFullscreen). A shown view keeps
     *  them hidden: an exit that arrives late must not show the bars over it. */
    boolean pageLeft() {
        boolean was = on();
        page = false;
        return was != on();
    }

    /** A new document is never in full screen: a page that was replaced
     *  while it held the bars never sent its exit. */
    boolean newDocument() {
        return pageLeft();
    }
}
