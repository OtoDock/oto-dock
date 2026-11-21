import XCTest
import JavaScriptCore
@testable import OtoDockShell

final class BackSwipeTests: XCTestCase {

    func testOnlyASwipeFromTheLeftEdgeToTheRightCounts() {
        XCTAssertTrue(BackSwipe.counts(startX: 4, dx: 120, dy: 10))
        XCTAssertTrue(BackSwipe.counts(startX: -30, dx: 50, dy: 0), "beside an inset web view")
        XCTAssertFalse(BackSwipe.counts(startX: 20, dx: 120, dy: 0), "the dashboard's own swipes start here")
        XCTAssertFalse(BackSwipe.counts(startX: 4, dx: 49, dy: 0), "too short, or returned toward the edge")
        XCTAssertFalse(BackSwipe.counts(startX: 4, dx: 60, dy: 50), "not sideways")
        XCTAssertFalse(BackSwipe.counts(startX: 4, dx: -80, dy: 0))
    }

    /// The script's answer on a page, and how many times it went back.
    private func run(path: String, idx: Int? = 1, open layer: String? = nil) throws -> (String, Int32) {
        let context = try XCTUnwrap(JSContext())
        let state = idx.map { "{ idx: \($0) }" } ?? "null"
        context.evaluateScript("""
        var backs = 0;
        var window = {
          location: { pathname: '\(path)' },
          history: { state: \(state), back: function () { backs += 1; } }
        };
        """)
        if let layer { context.evaluateScript("window.\(layer) = true;") }
        let answer = try XCTUnwrap(context.evaluateScript(BackSwipe.script)?.toString())
        XCTAssertNil(context.exception, "the script runs")
        return (answer, context.evaluateScript("backs").toInt32())
    }

    func testAPageOfTheDashboardGoesBackOneEntry() throws {
        for path in ["/user-settings", "/user-settings/audio", "/agent-settings/helper/general", "/admin", "/apps/x"] {
            let (answer, backs) = try run(path: path)
            XCTAssertEqual("back", answer, path)
            XCTAssertEqual(1, backs, path)
        }
    }

    func testAChatKeepsItsOwnSwipe() throws {
        for path in ["/", "/chat/helper", "/chat/helper/3f2a"] {
            let (answer, backs) = try run(path: path, idx: 4)
            XCTAssertEqual("chat", answer, path)
            XCTAssertEqual(0, backs, path)
        }
    }

    func testTheFirstEntryHasNothingOfTheDashboardBehindIt() throws {
        for idx in [0, nil] as [Int?] {
            let (answer, backs) = try run(path: "/user-settings", idx: idx)
            XCTAssertEqual("first", answer)
            XCTAssertEqual(0, backs)
        }
    }

    func testAnOpenLayerKeepsItsOwnControls() throws {
        for layer in ["__otodockDialogActive", "__otodockVideoFullscreenActive", "__otodockLightboxActive", "__otodockWorkspaceSelectionActive"] {
            let (answer, backs) = try run(path: "/user-settings/audio", open: layer)
            XCTAssertEqual("layer", answer, layer)
            XCTAssertEqual(0, backs, layer)
        }
    }
}
