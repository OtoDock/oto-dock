package com.otodock;

/**
 * The rules behind the alarm's settings offers and the store review's floor.
 * Pure Java (no android.*), so the once flags, the battery test and the
 * floor are unit-tested on the JVM; the activity backs {@link Flags} with
 * its SharedPreferences.
 */
final class OneTimeOffers {

    /** Where an offer records that it was made. */
    interface Flags {
        boolean get(String key);

        void set(String key);
    }

    private OneTimeOffers() {}

    /** True the first time for this key, recording it: any answer to the
     *  offer, "Not now" included, counts as the one time. */
    static boolean take(Flags flags, String key) {
        if (flags.get(key)) return false;
        flags.set(key);
        return true;
    }

    /**
     * Whether the app's battery setting would help its alarms: Android may
     * hold back the app (it is not exempt from battery optimization), or the
     * person restricted it. The one-time offer and the App Settings row ask
     * the same question.
     */
    static boolean batteryHelps(boolean ignoringOptimizations, boolean backgroundRestricted) {
        return !ignoringOptimizations || backgroundRestricted;
    }

    /** At most one store review hand-over a day for the whole app, whatever
     *  the installations' pages ask. */
    static final long REVIEW_GAP_MS = 24L * 60 * 60 * 1000;

    /** Whether the app may hand the store a review request now: never
     *  before, a day since the last, or the clock went back (a stamp in the
     *  future would hold every ask for as long as the clock is off). */
    static boolean reviewDue(long lastAskedAt, long now) {
        return lastAskedAt <= 0 || now < lastAskedAt || now - lastAskedAt >= REVIEW_GAP_MS;
    }
}
