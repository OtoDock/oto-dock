package com.otodock;

import java.util.Locale;

/**
 * One display's state as the fold log sees it. Pure Java (no android.*), so
 * the "did anything worth a line change" rule is unit-tested on the JVM: the
 * refresh rate and the mode are shown but never compared, since LTPO panels
 * switch them constantly and a line per switch would bury the fold.
 */
final class FoldSnapshot {

    final int id;
    final int state;
    final int rotation;
    final int width;
    final int height;
    final int densityDpi;
    final int flags;
    final float refreshRate;

    FoldSnapshot(int id, int state, int rotation, int width, int height,
                 int densityDpi, int flags, float refreshRate) {
        this.id = id;
        this.state = state;
        this.rotation = rotation;
        this.width = width;
        this.height = height;
        this.densityDpi = densityDpi;
        this.flags = flags;
        this.refreshRate = refreshRate;
    }

    /** True when no compared field differs (the refresh rate is not one). */
    boolean sameAs(FoldSnapshot o) {
        return o != null && id == o.id && state == o.state && rotation == o.rotation
                && width == o.width && height == o.height && densityDpi == o.densityDpi
                && flags == o.flags;
    }

    /** android.view.Display's state constants, named. */
    static String stateName(int state) {
        switch (state) {
            case 1: return "OFF";
            case 2: return "ON";
            case 3: return "DOZE";
            case 4: return "DOZE_SUSPEND";
            case 5: return "VR";
            case 6: return "ON_SUSPEND";
            default: return "UNKNOWN(" + state + ")";
        }
    }

    String describe() {
        return String.format(Locale.ROOT, "display %d %s rot=%d %dx%d dpi=%d flags=0x%x hz=%.0f",
                id, stateName(state), rotation, width, height, densityDpi, flags, refreshRate);
    }
}
