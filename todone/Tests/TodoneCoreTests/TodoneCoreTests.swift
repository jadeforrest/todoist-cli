import Foundation
import XCTest
@testable import TodoneCore

final class FormatTests: XCTestCase {
    func testCalendarBannerFollowsReauthState() {
        XCTAssertTrue(Format.calendarBanner(error: "boom", reauth: "running").contains("consent screen"))
        XCTAssertFalse(Format.calendarBanner(error: "boom", reauth: "running").contains("boom"))
        XCTAssertTrue(Format.calendarBanner(error: "boom", reauth: "failed").hasSuffix("boom"))
        XCTAssertTrue(Format.calendarBanner(error: "boom", reauth: "failed").contains("gog-reauth"))
        XCTAssertEqual(Format.calendarBanner(error: "boom", reauth: nil),
                       "Calendar unavailable, so free time is unknown: boom")
    }

    func testMinutes() {
        XCTAssertEqual(Format.minutes(nil), "—")
        XCTAssertEqual(Format.minutes(0), "0m")
        XCTAssertEqual(Format.minutes(-3), "0m")
        XCTAssertEqual(Format.minutes(45), "45m")
        XCTAssertEqual(Format.minutes(120), "2h")
        XCTAssertEqual(Format.minutes(123), "2h 3m")
    }

    func testParseMinutes() {
        XCTAssertEqual(Format.parseMinutes("1h30m"), 90)
        XCTAssertEqual(Format.parseMinutes("1h 30m"), 90)
        XCTAssertEqual(Format.parseMinutes("2H"), 120)
        XCTAssertEqual(Format.parseMinutes("45"), 45)
        XCTAssertEqual(Format.parseMinutes("0"), 0)
        XCTAssertNil(Format.parseMinutes(""))
        XCTAssertNil(Format.parseMinutes("-5"))
        XCTAssertNil(Format.parseMinutes("h"))
        XCTAssertNil(Format.parseMinutes("lots"))
        XCTAssertNil(Format.parseMinutes("30m1h"))
    }

    /// Property: whatever Format.minutes prints, parseMinutes reads back.
    func testMinutesRoundTrip() {
        var generator = SystemRandomNumberGenerator()
        for _ in 0..<2000 {
            let value = Int.random(in: 0...100_000, using: &generator)
            XCTAssertEqual(Format.parseMinutes(Format.minutes(value)), value, "\(value)")
        }
    }

    func testDates() {
        XCTAssertEqual(Format.iso(Format.date("2026-11-17")!), "2026-11-17")
        XCTAssertEqual(Format.iso(Format.date("2026-11-17T15:00:00")!), "2026-11-17")
        XCTAssertNil(Format.date(nil))
        XCTAssertEqual(Format.shortDate(nil), "—")
        XCTAssertEqual(Format.dayLabel("garbage"), "garbage")
        XCTAssertTrue(Format.dayLabel("2026-11-17").contains("17"))
        XCTAssertTrue(Format.shortDate("2026-11-17").contains("17"))
    }

    func testElapsed() {
        let start = "2026-11-16T09:00:00-08:00"
        let base = Format.parseTimestamp(start)!
        XCTAssertEqual(Format.elapsed(since: start, now: base.addingTimeInterval(65)), "1:05")
        XCTAssertEqual(Format.elapsed(since: start, now: base.addingTimeInterval(3725)), "1:02:05")
        XCTAssertEqual(Format.elapsed(since: start, now: base.addingTimeInterval(-10)), "0:00")
        XCTAssertEqual(Format.elapsed(since: "nope"), "")
        XCTAssertNotNil(Format.parseTimestamp("2026-11-16T09:00:00.123456-08:00"))
    }
}

final class ProtocolTests: XCTestCase {
    func testLineBufferSplitsAcrossChunks() {
        var buffer = LineBuffer()
        XCTAssertEqual(buffer.append(Data("{\"a\":".utf8)), [])
        let lines = buffer.append(Data("1}\n{\"b\":2}\n{\"c\"".utf8))
        XCTAssertEqual(lines.map { String(decoding: $0, as: UTF8.self) }, ["{\"a\":1}", "{\"b\":2}"])
        XCTAssertEqual(buffer.append(Data(":3}\n".utf8)).count, 1)
    }

    /// Property: any split of a stream yields the same lines.
    func testLineBufferIgnoresChunkBoundaries() {
        let text = (0..<50).map { "{\"id\":\($0),\"result\":\"line \($0)\"}" }.joined(separator: "\n") + "\n"
        let bytes = Array(text.utf8)
        for _ in 0..<200 {
            var buffer = LineBuffer()
            var lines: [Data] = []
            var index = 0
            while index < bytes.count {
                let size = Int.random(in: 1...40)
                let end = min(bytes.count, index + size)
                lines += buffer.append(Data(bytes[index..<end]))
                index = end
            }
            XCTAssertEqual(lines.count, 50)
            XCTAssertEqual(String(decoding: lines[49], as: UTF8.self), "{\"id\":49,\"result\":\"line 49\"}")
        }
    }

    func testRequestIsOneJSONLine() throws {
        let data = try Protocol.request(id: 7, method: "move", params: ["task_id": "t1"])
        XCTAssertEqual(data.last, 0x0A)
        let object = try JSONSerialization.jsonObject(with: data.dropLast()) as! [String: Any]
        XCTAssertEqual(object["id"] as? Int, 7)
        XCTAssertEqual(object["method"] as? String, "move")
    }

    func testResponseResult() throws {
        let (id, result) = Protocol.response(Data(#"{"id": 3, "result": [1, 2]}"#.utf8))
        XCTAssertEqual(id, 3)
        XCTAssertEqual(try JSONSerialization.jsonObject(with: result.get()) as? [Int], [1, 2])
    }

    func testResponseFragmentResult() throws {
        let (_, result) = Protocol.response(Data(#"{"id": 3, "result": null}"#.utf8))
        XCTAssertEqual(String(decoding: try result.get(), as: UTF8.self), "null")
    }

    func testResponseError() {
        let (id, result) = Protocol.response(Data(#"{"id": 4, "error": "nope"}"#.utf8))
        XCTAssertEqual(id, 4)
        XCTAssertEqual(result.failure, .remote("nope"))
    }

    func testResponseMalformed() {
        XCTAssertNil(Protocol.response(Data("garbage".utf8)).id)
        if case .malformed = Protocol.response(Data(#"{"id": 1}"#.utf8)).result.failure {} else {
            XCTFail("expected malformed")
        }
    }

    func testErrorDescriptions() {
        XCTAssertEqual(HelperError.remote("x").errorDescription, "x")
        XCTAssertTrue(HelperError.exited(1).errorDescription!.contains("exit 1"))
        XCTAssertTrue(HelperError.malformed("y").errorDescription!.contains("y"))
    }
}

final class HelperProcessTests: XCTestCase {
    /// Runs the real Python helper in fake mode and decodes a snapshot.
    func testFakeHelperRoundTrip() async throws {
        let repo = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent()
        let python = repo.appendingPathComponent(".venv/bin/python")
        try XCTSkipUnless(FileManager.default.fileExists(atPath: python.path), "no venv")
        let helper = try Helper(python: python, module: "todoist_cli.todone_server",
                                arguments: ["--fake"], environment: ProcessInfo.processInfo.environment)
        defer { helper.stop() }
        let snap = try await helper.call("refresh", as: Snapshot.self)
        XCTAssertFalse(snap.planning)
        XCTAssertFalse(snap.todayTasks.isEmpty)
        let colors = try await helper.call("colors", as: [EventColor].self)
        XCTAssertNil(colors.first?.colorId)
        do {
            _ = try await helper.call("stop", ["task_id": "t3"])
            XCTFail("expected an error")
        } catch let error as HelperError {
            XCTAssertEqual(error, .remote("that task has no running timer"))
        }
        let logged = try await helper.call("start_log", ["title": "Acme call", "description": ""], as: Snapshot.self)
        XCTAssertEqual(logged.timers.map(\.isLog), [true])
        let stopped = try await helper.call("stop", ["task_id": logged.timers[0].taskId, "color": 5], as: Snapshot.self)
        XCTAssertTrue(stopped.timers.isEmpty)
        let details = try await helper.call("details", ["task_id": "t3"], as: Details.self)
        XCTAssertEqual(details.comments.last?.attachment?.fileName, "outline.md")
    }

    func testCallAfterExitFails() async throws {
        let helper = try Helper(python: URL(fileURLWithPath: "/usr/bin/true"), module: "x",
                                arguments: [], environment: [:])
        try await Task.sleep(nanoseconds: 300_000_000)
        do {
            _ = try await helper.call("snapshot")
            XCTFail("expected exit error")
        } catch let error as HelperError {
            if case .exited = error {} else { XCTFail("\(error)") }
        }
    }
}

final class DetailChangesTests: XCTestCase {
    private let task = TaskDetail(
        id: "t1", content: "Write", description: "", priority: 4, dueString: "today", deadline: nil,
        labels: ["a"], sizeMinutes: 30, hasNativeDuration: false, projectId: "p1", sectionId: nil, url: "u"
    )

    func testNoChangesIsEmpty() {
        XCTAssertTrue(DetailChanges(from: task).diff(from: task).isEmpty)
    }

    func testOnlyChangedFields() {
        var form = DetailChanges(from: task)
        form.content = "Write more"
        form.priority = 1
        form.labels = ["a", "b"]
        form.sizeMinutes = 45
        form.dueString = ""
        form.description = "notes"
        let diff = form.diff(from: task)
        XCTAssertEqual(Set(diff.keys), ["content", "priority", "labels", "size_minutes", "due_string", "description"])
        XCTAssertEqual(diff["priority"] as? Int, 1)
    }

    func testClearedDeadlineIsNull() {
        var withDeadline = task
        withDeadline.deadline = "2026-11-20"
        var form = DetailChanges(from: withDeadline)
        form.deadline = nil
        XCTAssertTrue(form.diff(from: withDeadline)["deadline"] is NSNull)
        form.deadline = "2026-11-21"
        XCTAssertEqual(form.diff(from: withDeadline)["deadline"] as? String, "2026-11-21")
    }

    func testProjectChangeSendsBoth() {
        var form = DetailChanges(from: task)
        form.sectionId = "s1"
        let diff = form.diff(from: task)
        XCTAssertEqual(diff["project_id"] as? String, "p1")
        XCTAssertEqual(diff["section_id"] as? String, "s1")
        form.projectId = nil
        form.sectionId = nil
        XCTAssertTrue(form.diff(from: task)["project_id"] is NSNull)
    }
}

final class DecodingTests: XCTestCase {
    func testColorDecodesNullId() throws {
        let colors = try Decoding.decode([EventColor].self, from: Data(##"[{"id": null, "name": "Default", "hex": null}, {"id": 5, "name": "Banana", "hex": "#fbd75b"}]"##.utf8))
        XCTAssertNil(colors[0].colorId)
        XCTAssertEqual(colors[1].colorId, 5)
        XCTAssertEqual(colors[1].id, "Banana")
    }
}

private extension Result {
    var failure: Failure? {
        if case .failure(let error) = self { return error }
        return nil
    }
}

final class DetailHelpersTests: XCTestCase {
    private let task = TaskDetail(
        id: "t1", content: "Write", description: "", priority: 4, dueString: "", deadline: nil,
        labels: ["a"], sizeMinutes: 30, hasNativeDuration: false, projectId: "p1", sectionId: nil, url: "u"
    )

    func testPayloadReadsSize() throws {
        let form = DetailChanges(from: task)
        XCTAssertTrue(try form.payload(sizeText: "30m", original: task).isEmpty)
        XCTAssertEqual(try form.payload(sizeText: "1h", original: task)["size_minutes"] as? Int, 60)
        XCTAssertThrowsError(try form.payload(sizeText: "soon", original: task)) {
            XCTAssertEqual($0 as? DetailError, .badSize("soon"))
            XCTAssertTrue($0.localizedDescription.contains("soon"))
        }
    }

    func testLabels() {
        var form = DetailChanges(from: task)
        form.toggleLabel("b")
        XCTAssertEqual(form.labels, ["a", "b"])
        form.toggleLabel("a")
        XCTAssertEqual(form.labels, ["b"])
        form.addLabel("  c ")
        form.addLabel("b")
        form.addLabel("   ")
        XCTAssertEqual(form.labels, ["b", "c"])
    }
}

final class FlowTests: XCTestCase {
    func testWrapsWhenFull() {
        let sizes = [CGSize(width: 40, height: 10), CGSize(width: 40, height: 12), CGSize(width: 40, height: 10)]
        let out = Flow.arrange(sizes: sizes, width: 90, spacing: 5)
        XCTAssertEqual(out.origins, [CGPoint(x: 0, y: 0), CGPoint(x: 45, y: 0), CGPoint(x: 0, y: 17)])
        XCTAssertEqual(out.size, CGSize(width: 85, height: 27))
    }

    func testOversizedItemStillPlaced() {
        let out = Flow.arrange(sizes: [CGSize(width: 200, height: 10)], width: 50, spacing: 5)
        XCTAssertEqual(out.origins, [.zero])
    }

    /// Property: nothing after the first item on a line overflows the width.
    func testNoOverflow() {
        for _ in 0..<300 {
            let sizes = (0..<Int.random(in: 0...20)).map { _ in
                CGSize(width: CGFloat(Int.random(in: 1...60)), height: CGFloat(Int.random(in: 1...20)))
            }
            let width = CGFloat(Int.random(in: 60...200))
            let out = Flow.arrange(sizes: sizes, width: width, spacing: 4)
            for (origin, size) in zip(out.origins, sizes) where origin.x > 0 {
                XCTAssertLessThanOrEqual(origin.x + size.width, width)
            }
        }
    }
}

final class WindowLayoutTests: XCTestCase {
    private let screen = CGRect(x: 0, y: 25, width: 2560, height: 1400)

    func testWidths() {
        XCTAssertEqual(WindowLayout.width(planner: false, dayColumns: 5), WindowLayout.narrowWidth())
        XCTAssertGreaterThan(WindowLayout.width(planner: true, dayColumns: 5), 2000)
        XCTAssertGreaterThan(WindowLayout.wideWidth(dayColumns: 6), WindowLayout.wideWidth(dayColumns: 5))
    }

    func testDefaultScaleKeepsTheOriginalWidths() {
        XCTAssertEqual(WindowLayout.narrowWidth(), 480)
        // inbox 266+21, today 461, board 20 + 6 panels of 266 + 5 gaps
        XCTAssertEqual(WindowLayout.wideWidth(dayColumns: 5), 287 + 461 + 20 + 6 * 266 + 50)
        // Doubled text: panels 500+16, today 920+1; gaps and padding stay put.
        XCTAssertEqual(WindowLayout.wideWidth(dayColumns: 5, scale: 2), 537 + 921 + 20 + 6 * 516 + 50)
        XCTAssertEqual(WindowLayout.narrowWidth(scale: 2), 960)
    }

    /// Property: bigger text never makes the window narrower, in either mode.
    func testWidthsGrowWithTextSize() {
        for planner in [false, true] {
            for columns in 1...8 {
                let widths = TextSize.allCases.map { WindowLayout.width(planner: planner, dayColumns: columns, scale: $0.scale) }
                XCTAssertEqual(widths, widths.sorted(), "planner \(planner), \(columns) columns")
                XCTAssertEqual(Set(widths).count, widths.count)
            }
        }
    }

    func testKeepsPositionWhenItFits() {
        let current = CGRect(x: 100, y: 200, width: 480, height: 700)
        XCTAssertEqual(WindowLayout.frame(current: current, screen: screen, width: 1200),
                       CGRect(x: 100, y: 200, width: 1200, height: 700))
    }

    func testPullsLeftToStayOnScreen() {
        let current = CGRect(x: 2000, y: 200, width: 480, height: 700)
        XCTAssertEqual(WindowLayout.frame(current: current, screen: screen, width: 1200).minX, 1360)
    }

    func testNeverWiderThanScreen() {
        let current = CGRect(x: 500, y: 200, width: 480, height: 700)
        let out = WindowLayout.frame(current: current, screen: screen, width: 9000)
        XCTAssertEqual(out.minX, 0)
        XCTAssertEqual(out.width, 2560)
    }

    /// Property: the result is always inside the screen horizontally and keeps height and y.
    func testAlwaysOnScreen() {
        for _ in 0..<1000 {
            let screen = CGRect(x: CGFloat(Int.random(in: -2000...2000)), y: 0,
                                width: CGFloat(Int.random(in: 800...4000)), height: 1000)
            let current = CGRect(x: CGFloat(Int.random(in: -3000...5000)), y: CGFloat(Int.random(in: 0...500)),
                                 width: 480, height: CGFloat(Int.random(in: 300...900)))
            let out = WindowLayout.frame(current: current, screen: screen, width: CGFloat(Int.random(in: 400...5000)))
            XCTAssertGreaterThanOrEqual(out.minX, screen.minX)
            XCTAssertLessThanOrEqual(out.maxX, screen.maxX)
            XCTAssertEqual(out.height, current.height)
            XCTAssertEqual(out.minY, current.minY)
        }
    }
}

final class TextSizeTests: XCTestCase {
    func testStepsStopAtTheEnds() {
        XCTAssertEqual(TextSize.standard.bigger, .large)
        XCTAssertEqual(TextSize.standard.smaller, .small)
        XCTAssertEqual(TextSize.largest.bigger, .largest)
        XCTAssertEqual(TextSize.small.smaller, .small)
    }

    /// Property: stepping up then down returns to the start except at the top, and the
    /// scale rises strictly with each step.
    func testStepsAndScalesAreOrdered() {
        let all = TextSize.allCases
        for size in all where size != .largest {
            XCTAssertEqual(size.bigger.smaller, size)
            XCTAssertGreaterThan(size.bigger.scale, size.scale)
        }
        for size in all where size != .small { XCTAssertEqual(size.smaller.bigger, size) }
        XCTAssertEqual(TextSize.standard.scale, 1)
        XCTAssertEqual(Set(all.map(\.label)).count, all.count)
    }

    func testStoredValueRoundTripsAndFallsBack() {
        for size in TextSize.allCases { XCTAssertEqual(TextSize(stored: size.rawValue), size) }
        XCTAssertEqual(TextSize(stored: nil), .standard)
        XCTAssertEqual(TextSize(stored: "huge"), .standard)
    }

    func testPointSizes() {
        XCTAssertEqual(TextStyleSize.points(.body, scale: 1), 13)
        XCTAssertEqual(TextStyleSize.points(.body, scale: 1.5), 20)  // 19.5 rounds up
        XCTAssertEqual(TextStyleSize.points(.caption, scale: 0.9), 9)
        XCTAssertEqual(TextStyleSize.base(.headline), TextStyleSize.base(.body))
        let styles: [TextStyleSize.Style] = [.caption, .callout, .body, .title3, .title2]
        XCTAssertEqual(styles.map(TextStyleSize.base), styles.map(TextStyleSize.base).sorted())
        for size in TextSize.allCases {
            for style in styles {
                XCTAssertLessThanOrEqual(abs(TextStyleSize.points(style, scale: size.scale) - TextStyleSize.base(style) * size.scale), 0.5)
            }
        }
    }
}
