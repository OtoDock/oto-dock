package com.otodock;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import java.util.HashSet;
import java.util.Set;
import org.junit.Test;

/** The alarm's settings offers: each one once, whatever the answer, the
 *  battery row shown only while the battery setting would help. */
public class OneTimeOffersTest {

    private static final class MemoryFlags implements OneTimeOffers.Flags {
        final Set<String> set = new HashSet<>();

        @Override
        public boolean get(String key) {
            return set.contains(key);
        }

        @Override
        public void set(String key) {
            set.add(key);
        }
    }

    @Test
    public void anOfferIsMadeOnce() {
        MemoryFlags flags = new MemoryFlags();
        assertTrue(OneTimeOffers.take(flags, "battery_settings_offered"));
        assertFalse(OneTimeOffers.take(flags, "battery_settings_offered"));
        assertFalse(OneTimeOffers.take(flags, "battery_settings_offered"));
    }

    @Test
    public void eachOfferHasItsOwnFlag() {
        MemoryFlags flags = new MemoryFlags();
        assertTrue(OneTimeOffers.take(flags, "full_screen_offered"));
        assertTrue(OneTimeOffers.take(flags, "battery_settings_offered"));
        assertFalse(OneTimeOffers.take(flags, "full_screen_offered"));
    }

    @Test
    public void theBatterySettingHelpsUnlessTheAppIsExemptAndUnrestricted() {
        assertTrue(OneTimeOffers.batteryHelps(false, false));
        assertTrue(OneTimeOffers.batteryHelps(false, true));
        assertTrue(OneTimeOffers.batteryHelps(true, true));
        assertFalse(OneTimeOffers.batteryHelps(true, false));
    }

    @Test
    public void theStoreReviewIsHandedOverAtMostOnceADay() {
        long day = OneTimeOffers.REVIEW_GAP_MS;
        long now = 1_800_000_000_000L;
        assertTrue("never before", OneTimeOffers.reviewDue(0L, now));
        assertFalse(OneTimeOffers.reviewDue(now - 1000L, now));
        assertFalse(OneTimeOffers.reviewDue(now - day + 1, now));
        assertTrue(OneTimeOffers.reviewDue(now - day, now));
        assertTrue("a clock that went back never holds the ask", OneTimeOffers.reviewDue(now + day, now));
    }

    @Test
    public void theActivityUsesTheseRules() throws Exception {
        String main = SourceText.javaSource("MainActivity");
        String offer = SourceText.body(main, "private boolean offerBatterySettingsOnce(");
        assertTrue(offer.contains("OneTimeOffers.batteryHelps("));
        assertTrue(SourceText.body(main, "private boolean offerOnce(").contains("OneTimeOffers.take("));
        assertTrue(SourceText.body(main, "private boolean batteryAdvice(").contains("OneTimeOffers.batteryHelps("));
        assertTrue(SourceText.body(main, "private void requestStoreReview(").contains("OneTimeOffers.reviewDue("));
    }
}
