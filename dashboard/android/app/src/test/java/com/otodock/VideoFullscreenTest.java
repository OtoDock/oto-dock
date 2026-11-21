package com.otodock;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

/** Who holds the screen for a video: each method says whether the bars
 *  change, so a call beside a shown view, or one that arrives late, changes
 *  nothing. */
public class VideoFullscreenTest {

    @Test
    public void theViewAloneHidesTheBarsAndGivesThemBack() {
        VideoFullscreen fs = new VideoFullscreen();
        assertFalse(fs.on());
        assertTrue(fs.viewShown());
        assertTrue(fs.on());
        assertTrue(fs.viewHidden());
        assertFalse(fs.on());
    }

    @Test
    public void thePageAloneHidesTheBarsAndGivesThemBack() {
        VideoFullscreen fs = new VideoFullscreen();
        assertTrue(fs.pageEntered());
        assertTrue(fs.on());
        assertTrue(fs.pageLeft());
        assertFalse(fs.on());
    }

    /** Every dashboard calls the channel on a full screen change, beside the view. */
    @Test
    public void thePagesCallsBesideAShownViewChangeNothing() {
        VideoFullscreen fs = new VideoFullscreen();
        assertTrue(fs.viewShown());
        assertFalse(fs.pageEntered());
        assertFalse(fs.pageEntered());
        assertFalse("an exit beside a shown view keeps the bars hidden", fs.pageLeft());
        assertTrue(fs.on());
    }

    @Test
    public void theBarsReturnWithTheViewAndThePagesLateExitFindsNothing() {
        VideoFullscreen fs = new VideoFullscreen();
        fs.viewShown();
        fs.pageEntered();
        assertTrue("the view's hide ends the page's request with it", fs.viewHidden());
        assertFalse(fs.on());
        assertFalse(fs.pageLeft());
    }

    @Test
    public void aLateExitDoesNotShowTheBarsOverANewView() {
        VideoFullscreen fs = new VideoFullscreen();
        fs.viewShown();
        fs.pageEntered();
        fs.viewHidden();
        assertTrue(fs.viewShown());
        assertFalse(fs.pageLeft());
        assertTrue(fs.on());
    }

    @Test
    public void repeatsChangeNothing() {
        VideoFullscreen fs = new VideoFullscreen();
        assertFalse(fs.viewHidden());
        assertFalse(fs.pageLeft());
        assertTrue(fs.viewShown());
        assertFalse(fs.viewShown());
        assertTrue(fs.viewHidden());
        assertFalse(fs.viewHidden());
    }

    /** A page replaced while it held the bars never sent its exit. */
    @Test
    public void aNewDocumentGivesThePagesBarsBackAndLeavesAViewAlone() {
        VideoFullscreen fs = new VideoFullscreen();
        fs.pageEntered();
        assertTrue(fs.newDocument());
        assertFalse(fs.on());
        assertFalse(fs.newDocument());

        fs.viewShown();
        fs.pageEntered();
        assertFalse(fs.newDocument());
        assertTrue(fs.on());
    }
}
