package com.otodock;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

/** The fold log's display filter: a line when a display really changed,
 *  none for the refresh-rate switches an LTPO panel makes all day. */
public class FoldSnapshotTest {

    private static FoldSnapshot on(float hz) {
        return new FoldSnapshot(0, 2, 0, 2156, 2344, 402, 0x8b, hz);
    }

    @Test
    public void aRefreshRateChangeIsTheSameDisplay() {
        assertTrue(on(120f).sameAs(on(60f)));
        assertTrue(on(1f).sameAs(on(120f)));
    }

    @Test
    public void everyComparedFieldMakesADifference() {
        FoldSnapshot base = on(120f);
        assertFalse("id", base.sameAs(new FoldSnapshot(1, 2, 0, 2156, 2344, 402, 0x8b, 120f)));
        assertFalse("state", base.sameAs(new FoldSnapshot(0, 1, 0, 2156, 2344, 402, 0x8b, 120f)));
        assertFalse("rotation", base.sameAs(new FoldSnapshot(0, 2, 1, 2156, 2344, 402, 0x8b, 120f)));
        assertFalse("size", base.sameAs(new FoldSnapshot(0, 2, 0, 1060, 2376, 402, 0x8b, 120f)));
        assertFalse("density", base.sameAs(new FoldSnapshot(0, 2, 0, 2156, 2344, 420, 0x8b, 120f)));
        assertFalse("flags", base.sameAs(new FoldSnapshot(0, 2, 0, 2156, 2344, 402, 0x8f, 120f)));
        assertFalse("nothing before", base.sameAs(null));
    }

    @Test
    public void theLineNamesTheStateAndEveryField() {
        assertEquals("display 0 ON rot=0 2156x2344 dpi=402 flags=0x8b hz=120", on(120f).describe());
        assertEquals("OFF", FoldSnapshot.stateName(1));
        assertEquals("DOZE", FoldSnapshot.stateName(3));
        assertEquals("UNKNOWN(9)", FoldSnapshot.stateName(9));
    }
}
