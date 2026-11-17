import Foundation
import XCTest
@testable import TodoneCore

final class QuickAddTests: XCTestCase {
    private let projects = [
        Project(id: "work", name: "Consulting"), Project(id: "home", name: "Home"),
        Project(id: "hp", name: "Home Projects"),
    ]

    private func parse(_ text: String, original: String = "", keepDate: Bool = false) -> QuickAdd {
        QuickAdd.parse(text, original: original, projects: projects, keepDateAsText: keepDate)
    }

    func testPlainTitleIsUnchanged() {
        let q = parse("Review 2 drafts of chapter 5")
        XCTAssertEqual(q.content, "Review 2 drafts of chapter 5")
        XCTAssertNil(q.priority)
        XCTAssertNil(q.dueString)
        XCTAssertTrue(q.tokens.isEmpty)
    }

    func testEverythingAtOnce() {
        let q = parse("Send invoice p1 @admin @Billing #Consulting tomorrow at 3pm {fri}")
        XCTAssertEqual(q.content, "Send invoice")
        XCTAssertEqual(q.priority, 1)
        XCTAssertEqual(q.labels, ["admin", "Billing"])
        XCTAssertEqual(q.project?.id, "work")
        XCTAssertEqual(q.dueString, "tomorrow at 3pm")
        guard case .set(let iso)? = q.deadline else { return XCTFail("\(String(describing: q.deadline))") }
        XCTAssertEqual(Format.date(iso).map { Calendar.current.component(.weekday, from: $0) }, 6)
        XCTAssertEqual(q.tokens.map(\.kind), [.deadline, .label, .label, .project, .priority, .due])
    }

    func testPriorityLastWinsAndNeedsAWord() {
        XCTAssertEqual(parse("a p2 b P4").priority, 4)
        XCTAssertEqual(parse("a p2 b P4").content, "a b")
        XCTAssertNil(parse("mp3 p5 p12 top1").priority)
    }

    func testDatePhrases() {
        let cases: [(String, String)] = [
            ("Call mom tom", "tom"), ("Pay rent next friday", "next friday"), ("Ship nov 20", "nov 20"),
            ("Ship 20 Nov 2026", "20 Nov 2026"), ("Meet at 3pm", "at 3pm"), ("Standup 10:30", "10:30"),
            ("Do it in 3 days", "in 3 days"), ("Plan next week", "next week"), ("Call Bob 11/20", "11/20"),
            ("Taxes 2026-12-01", "2026-12-01"), ("Prep mon", "mon"), ("Lunch today at noon", "today at noon"),
            ("Drop it no date", "no date"), ("Water plants every 3 days", "every 3 days"),
            ("Review every other mon at 9am", "every other mon at 9am"),
            ("Pay every 15th", "every 15th"), ("Gym every mon, wed and fri", "every mon, wed and fri"),
        ]
        for (text, phrase) in cases {
            XCTAssertEqual(parse(text).dueString, phrase, text)
            XCTAssertFalse(parse(text).content.lowercased().contains(phrase.lowercased()), text)
        }
    }

    func testNotDates() {
        for text in ["Read chapter 5", "Fix bug 1234", "Do the 1:1 prep", "EV charger", "Review every chapter",
                     "Q4 report", "Monday.com setup", "@tomorrow label", "#mon"] {
            XCTAssertNil(parse(text).dueString, text)
        }
    }

    func testRecurrenceWinsOverADate() {
        let q = parse("Report every fri starting tomorrow")
        XCTAssertEqual(q.dueString, "every fri")
    }

    func testKeepDateAsText() {
        let q = parse("Call Tom", keepDate: true)
        XCTAssertEqual(q.content, "Call Tom")
        XCTAssertNil(q.dueString)
    }

    func testTokensInTheOriginalStayText() {
        let q = parse("Call Tom about Home p2", original: "Call Tom about Home")
        XCTAssertEqual(q.content, "Call Tom about Home")
        XCTAssertEqual(q.priority, 2)
        XCTAssertNil(q.dueString)
        // A new date still counts even when an old one stays.
        XCTAssertEqual(parse("Call Tom friday", original: "Call Tom").dueString, "friday")
        // Same token typed again is still the old one.
        XCTAssertNil(parse("Email @bob", original: "Email @bob").labels.first)
    }

    func testProjects() {
        XCTAssertEqual(parse("Fix fence #Home Projects").project?.id, "hp")
        XCTAssertEqual(parse("Fix fence #homeprojects").project?.id, "hp")
        XCTAssertEqual(parse("Fix fence #home").project?.id, "home")
        XCTAssertEqual(parse("Fix fence #home").content, "Fix fence")
        let unknown = parse("Fix #nowhere now")
        XCTAssertNil(unknown.project)
        XCTAssertEqual(unknown.content, "Fix #nowhere now")
        XCTAssertNil(parse("Fix #Homework").project)
        XCTAssertNil(parse("Issue#home").project)
        XCTAssertNil(parse("Fix fence #Home", original: "Fix fence #Home").project)
    }

    func testLabels() {
        let q = parse("Email @bob.smith and @bob.smith. @Deep-work")
        XCTAssertEqual(q.labels, ["bob.smith", "Deep-work"])
        XCTAssertEqual(q.content, "Email and .")
        XCTAssertTrue(parse("me@example.com").labels.isEmpty)
    }

    func testDeadlines() {
        let now = Format.date("2026-11-16")!  // a Monday
        XCTAssertEqual(QuickAdd.deadline("2026-12-01", now: now), .set("2026-12-01"))
        XCTAssertEqual(QuickAdd.deadline(" tom ", now: now), .set("2026-11-17"))
        XCTAssertEqual(QuickAdd.deadline("today", now: now), .set("2026-11-16"))
        XCTAssertEqual(QuickAdd.deadline("", now: now), .clear)
        XCTAssertEqual(QuickAdd.deadline("none", now: now), .clear)
        XCTAssertEqual(QuickAdd.deadline("whenever", now: now), .invalid("whenever"))
        XCTAssertEqual(QuickAdd.deadline("call fri", now: now), .invalid("call fri"))
        if case .set? = Optional(QuickAdd.deadline("dec 3", now: now)) {} else { XCTFail("dec 3") }
    }

    func testChanges() throws {
        let q = parse("Outline p1 @deep #Home next week {2026-12-01}")
        let diff = try q.changes(content: "Outline", priority: 3, labels: ["writing", "DEEP"])
        XCTAssertEqual(Set(diff.keys), ["priority", "project_id", "section_id", "due_string", "deadline"])
        XCTAssertEqual(diff["priority"] as? Int, 1)
        XCTAssertEqual(diff["deadline"] as? String, "2026-12-01")
        XCTAssertTrue(diff["section_id"] is NSNull)
        let added = try parse("Outline @new").changes(content: "Outline", priority: 4, labels: ["a"])
        XCTAssertEqual(added["labels"] as? [String], ["a", "new"])
        XCTAssertEqual(parse("x @new @A").mergedLabels(["a"]), ["a", "new"])
    }

    func testChangesRefusals() {
        XCTAssertThrowsError(try parse("p1").changes(content: "x", priority: 4, labels: [])) {
            XCTAssertEqual($0 as? QuickAdd.ParseError, .emptyTitle)
        }
        XCTAssertThrowsError(try parse("x {soonish}").changes(content: "x", priority: 4, labels: [])) {
            XCTAssertTrue(($0 as? LocalizedError)?.errorDescription?.contains("soonish") == true)
        }
        XCTAssertTrue(QuickAdd.ParseError.emptyTitle.errorDescription!.contains("empty"))
    }

    func testClearedDeadlineAndNoChange() throws {
        XCTAssertTrue(try parse("x {}").changes(content: "x", priority: 4, labels: [])["deadline"] is NSNull)
        XCTAssertTrue(try parse("same").changes(content: "same", priority: 2, labels: []).isEmpty)
        XCTAssertTrue(try parse("same p2").changes(content: "same", priority: 2, labels: []).isEmpty)
    }

    /// Property: a title with no quick-add characters or date words parses to itself
    /// (whitespace collapsed), with no tokens.
    func testPlainWordsRoundTrip() {
        let words = ["review", "draft", "Acme", "budget", "notes", "call", "team", "plan", "x", "42", "v2", "(draft)"]
        for _ in 0..<500 {
            let title = (0..<Int.random(in: 1...8)).map { _ in words.randomElement()! }.joined(separator: " ")
            let q = parse(title)
            XCTAssertEqual(q.content, title, title)
            XCTAssertTrue(q.tokens.isEmpty, title)
        }
    }

    /// Property: whatever tokens are appended to a plain title, the title comes back
    /// out and each token is reported exactly once.
    func testAppendedTokensComeBackOut() {
        let pool: [(String, QuickAdd.Kind)] = [
            ("p2", .priority), ("@focus", .label), ("#Home", .project), ("{dec 3}", .deadline), ("tomorrow", .due),
        ]
        for _ in 0..<300 {
            let picked = pool.shuffled().prefix(Int.random(in: 0...pool.count))
            let text = (["Write report"] + picked.map(\.0)).joined(separator: " ")
            let q = parse(text)
            XCTAssertEqual(q.content, "Write report", text)
            XCTAssertEqual(Set(q.tokens.map(\.kind)), Set(picked.map(\.1)), text)
            XCTAssertEqual(q.tokens.count, picked.count, text)
        }
    }
}

func makeTask(_ id: String, priority: Int = 4, labels: [String] = [], subtasks: [Subtask] = []) -> TaskItem {
    TaskItem(id: id, content: "Task \(id)", priority: priority, dueDate: nil, originalDueDate: nil, moved: false,
             dueTime: nil, deadline: nil, labels: labels, project: nil, minutes: 30, sizeLabel: "30m",
             pinned: false, fixed: false, recurring: false, movable: true, overdue: false, atRisk: false,
             spansUntil: nil, running: false, url: "u", description: "", commentCount: 0, subtasks: subtasks)
}

func makeSub(_ id: String, _ children: [Subtask] = []) -> Subtask {
    Subtask(id: id, content: "Sub \(id)", priority: 4, dueDate: nil, labels: [], commentCount: 0, url: "u",
            subtasks: children)
}

func makeSnapshot(overdue: [TaskItem] = [], today: [TaskItem] = [], columns: [[TaskItem]] = [],
                  tray: [TaskItem] = [], inbox: [TaskItem] = []) -> Snapshot {
    let column = { (tasks: [TaskItem]?, day: Int) in
        Column(date: "2026-11-\(16 + day)", capacityMinutes: 60, capacityOverridden: false, plannedMinutes: 0,
               taskCount: tasks?.count ?? 0, tasks: tasks)
    }
    return Snapshot(now: "", today: "2026-11-16", planning: false, calendarError: nil, todayColumn: column(nil, 0),
                    overdue: overdue, todayTasks: today,
                    columns: columns.enumerated().map { column($1, $0 + 1) }, tray: tray, inbox: inbox,
                    timers: [], result: nil, projects: [])
}

final class PendingEditsTests: XCTestCase {
    func testEmptyLeavesSnapshotAlone() {
        let snap = makeSnapshot(today: [makeTask("a")])
        XCTAssertEqual(PendingEdits().apply(to: snap), snap)
    }

    func testHiddenGoneEverywhereIncludingSubtasks() {
        let parent = makeTask("p", subtasks: [makeSub("s1", [makeSub("s2")]), makeSub("s3")])
        let snap = makeSnapshot(overdue: [makeTask("h")], today: [parent, makeTask("h")], columns: [[makeTask("h")]],
                                tray: [makeTask("h")], inbox: [makeTask("h")])
        var pending = PendingEdits()
        pending.hidden = ["h", "s2", "s3"]
        let shown = pending.apply(to: snap)
        XCTAssertEqual(Navigation.order(shown, planner: true), ["p"])
        XCTAssertEqual(shown.todayTasks[0].subtasks.map(\.id), ["s1"])
        XCTAssertTrue(shown.todayTasks[0].subtasks[0].subtasks.isEmpty)
    }

    func testEditsApplyToTasksAndSubtasks() {
        let snap = makeSnapshot(today: [makeTask("a", labels: ["x"], subtasks: [makeSub("s")])])
        var pending = PendingEdits()
        pending.edits["a"] = TaskEdit(content: "New", priority: 1, labels: ["x", "fixed"])
        pending.edits["s"] = TaskEdit(content: "Sub new", priority: 2, labels: ["y"])
        let task = pending.apply(to: snap).todayTasks[0]
        XCTAssertEqual(task.content, "New")
        XCTAssertEqual(task.priority, 1)
        XCTAssertTrue(task.fixed)
        XCTAssertEqual(task.subtasks[0].content, "Sub new")
        XCTAssertEqual(task.subtasks[0].priority, 2)
        XCTAssertEqual(task.subtasks[0].labels, ["y"])
        pending.edits["a"] = TaskEdit(priority: 2)
        pending.edits["s"] = TaskEdit(priority: 1)
        XCTAssertEqual(pending.apply(to: snap).todayTasks[0].subtasks[0].content, "Sub s")
        let partial = pending.apply(to: snap).todayTasks[0]
        XCTAssertEqual(partial.content, "Task a")
        XCTAssertEqual(partial.labels, ["x"])
        XCTAssertFalse(partial.fixed)
    }

    func testSettleClearsEverything() {
        var pending = PendingEdits()
        pending.completing.insert("a")
        pending.hidden.insert("a")
        pending.syncing.insert("a")
        pending.edits["a"] = TaskEdit(priority: 1)
        pending.syncing.insert("b")
        pending.settle("a")
        XCTAssertFalse(pending.isEmpty)
        pending.settle("b")
        XCTAssertTrue(pending.isEmpty)
    }

    func testDescendantCount() {
        let task = makeTask("p", subtasks: [makeSub("a", [makeSub("b", [makeSub("c")])]), makeSub("d")])
        XCTAssertEqual(task.descendantCount, 4)
        XCTAssertEqual(makeTask("x").descendantCount, 0)
    }
}

final class NavigationTests: XCTestCase {
    func testOrder() {
        let snap = makeSnapshot(overdue: [makeTask("o")], today: [makeTask("t")], columns: [[makeTask("c1")], [makeTask("i")]],
                                tray: [makeTask("r")], inbox: [makeTask("i")])
        XCTAssertEqual(Navigation.order(snap, planner: false), ["o", "t"])
        XCTAssertEqual(Navigation.order(snap, planner: true), ["i", "o", "t", "c1", "r"])
    }

    func testStep() {
        let ids = ["a", "b", "c"]
        XCTAssertNil(Navigation.step([], from: "a", by: 1))
        XCTAssertEqual(Navigation.step(ids, from: nil, by: 1), "a")
        XCTAssertEqual(Navigation.step(ids, from: nil, by: -1), "c")
        XCTAssertEqual(Navigation.step(ids, from: "gone", by: 1), "a")
        XCTAssertEqual(Navigation.step(ids, from: "a", by: 1), "b")
        XCTAssertEqual(Navigation.step(ids, from: "c", by: 1), "c")
        XCTAssertEqual(Navigation.step(ids, from: "a", by: -1), "a")
    }

    /// Property: a step always lands on a listed id, at most |delta| away.
    func testStepStaysInBounds() {
        for _ in 0..<1000 {
            let ids = (0..<Int.random(in: 1...12)).map(String.init)
            let current = Bool.random() ? ids.randomElement() : nil
            let delta = Int.random(in: -3...3)
            let next = Navigation.step(ids, from: current, by: delta)!
            XCTAssertTrue(ids.contains(next))
            if let current {
                XCTAssertLessThanOrEqual(abs(ids.firstIndex(of: next)! - ids.firstIndex(of: current)!), abs(delta))
            }
        }
    }

    func testKeys() {
        XCTAssertEqual(KeyCommand.from(keyCode: 38, characters: "j", modified: false), .next)
        XCTAssertEqual(KeyCommand.from(keyCode: 40, characters: "K", modified: false), .previous)
        XCTAssertEqual(KeyCommand.from(keyCode: 14, characters: "e", modified: false), .edit)
        XCTAssertEqual(KeyCommand.from(keyCode: 19, characters: "2", modified: false), .priority(2))
        XCTAssertEqual(KeyCommand.from(keyCode: KeyCommand.returnKey, characters: "\r", modified: false), .toggle)
        XCTAssertEqual(KeyCommand.from(keyCode: KeyCommand.enterKey, characters: "\u{3}", modified: false), .toggle)
        XCTAssertEqual(KeyCommand.from(keyCode: KeyCommand.escapeKey, characters: nil, modified: false), .collapse)
        XCTAssertEqual(KeyCommand.from(keyCode: KeyCommand.downKey, characters: nil, modified: false), .next)
        XCTAssertEqual(KeyCommand.from(keyCode: KeyCommand.upKey, characters: nil, modified: false), .previous)
        XCTAssertNil(KeyCommand.from(keyCode: 38, characters: "j", modified: true))
        XCTAssertNil(KeyCommand.from(keyCode: 23, characters: "5", modified: false))
        XCTAssertNil(KeyCommand.from(keyCode: 0, characters: nil, modified: false))
    }
}

final class SubtaskFlattenTests: XCTestCase {
    func testDepthFirstWithDepths() {
        let tree = [makeSub("a", [makeSub("b", [makeSub("c")]), makeSub("d")]), makeSub("e")]
        let rows = Subtask.flatten(tree)
        XCTAssertEqual(rows.map(\.subtask.id), ["a", "b", "c", "d", "e"])
        XCTAssertEqual(rows.map(\.depth), [0, 1, 2, 1, 0])
        XCTAssertEqual(rows.count, makeTask("p", subtasks: tree).descendantCount)
    }
}

final class SnapshotLookupTests: XCTestCase {
    func testAllTasksAndTitles() {
        let snap = makeSnapshot(overdue: [makeTask("o")], today: [makeTask("t", subtasks: [makeSub("s", [makeSub("d")])])],
                                columns: [[makeTask("c")]], tray: [makeTask("r")], inbox: [makeTask("i")])
        XCTAssertEqual(snap.allTasks.map(\.id), ["i", "o", "t", "c", "r"])
        XCTAssertEqual(snap.title(of: "c"), "Task c")
        XCTAssertEqual(snap.title(of: "d"), "Sub d")
        XCTAssertNil(snap.title(of: "zz"))
    }
}

final class BoardStateTests: XCTestCase {
    private let order = ["a", "b", "c"]

    func testMoving() {
        var state = BoardState()
        XCTAssertEqual(state.handle(.next, order: order), .handled)
        XCTAssertEqual(state.selected, "a")
        XCTAssertEqual(state.handle(.previous, order: order), .handled)
        XCTAssertEqual(state.selected, "a")
    }

    func testToggleEditAndCollapse() {
        var state = BoardState(selected: "b")
        XCTAssertEqual(state.handle(.toggle, order: order), .unfolded("b"))
        XCTAssertEqual(state.handle(.edit, order: order), .handled)
        XCTAssertEqual(state.editing, "b")
        XCTAssertEqual(state.handle(.collapse, order: order), .handled)
        XCTAssertEqual(state.expanded, [])
        XCTAssertNil(state.editing)
        XCTAssertEqual(state.selected, "b")
        XCTAssertEqual(state.handle(.collapse, order: order), .handled)
        XCTAssertNil(state.selected)
    }

    func testEditUnfoldsAFoldedCard() {
        var state = BoardState(selected: "c")
        XCTAssertEqual(state.handle(.edit, order: order), .unfolded("c"))
        XCTAssertEqual(state.expanded, ["c"])
        XCTAssertEqual(state.handle(.toggle, order: order), .handled)
        XCTAssertTrue(state.expanded.isEmpty)
    }

    func testFoldingAnotherCardKeepsEditing() {
        var state = BoardState(expanded: ["a", "b"], editing: "a")
        XCTAssertFalse(state.toggle("b"))
        XCTAssertEqual(state.editing, "a")
    }

    func testPriorityAndIgnored() {
        var state = BoardState(selected: "a")
        XCTAssertEqual(state.handle(.priority(1), order: order), .priority("a", 1))
        var empty = BoardState()
        XCTAssertEqual(empty.handle(.toggle, order: order), .ignored)
        var gone = BoardState(selected: "zz")
        XCTAssertEqual(gone.handle(.toggle, order: order), .ignored)
    }

    func testAfterRemoving() {
        XCTAssertEqual(Navigation.afterRemoving("b", from: order, selected: "b"), "c")
        XCTAssertEqual(Navigation.afterRemoving("c", from: order, selected: "c"), "b")
        XCTAssertEqual(Navigation.afterRemoving("b", from: order, selected: "a"), "a")
        XCTAssertNil(Navigation.afterRemoving("a", from: ["a"], selected: "a"))
        XCTAssertNil(Navigation.afterRemoving("x", from: order, selected: "x"))
    }

    func testColumnContaining() {
        let snap = makeSnapshot(overdue: [makeTask("o")], today: [makeTask("t")], columns: [[makeTask("c")]],
                                tray: [makeTask("r")], inbox: [makeTask("i")])
        XCTAssertEqual(BoardColumn.containing("o", in: snap), .today)
        XCTAssertEqual(BoardColumn.containing("t", in: snap), .today)
        XCTAssertEqual(BoardColumn.containing("i", in: snap), .inbox)
        XCTAssertEqual(BoardColumn.containing("c", in: snap), .day("2026-11-17"))
        XCTAssertEqual(BoardColumn.containing("r", in: snap), .tray)
        XCTAssertNil(BoardColumn.containing("zz", in: snap))
    }
}

/// Gaps the slow-pass mutation run (tools/swift_mutate.py) found.
final class MutationGapTests: XCTestCase {
    func testOutcomeFlags() {
        XCTAssertTrue(BoardState.Outcome.handled.handled)
        XCTAssertTrue(BoardState.Outcome.unfolded("x").handled)
        XCTAssertTrue(BoardState.Outcome.priority("x", 1).handled)
        XCTAssertFalse(BoardState.Outcome.ignored.handled)
    }

    func testJAndKMoveOneCard() {
        var state = BoardState(selected: "a")
        _ = state.handle(.next, order: ["a", "b", "c"])
        XCTAssertEqual(state.selected, "b")
        _ = state.handle(.next, order: ["a", "b", "c"])
        _ = state.handle(.previous, order: ["a", "b", "c"])
        XCTAssertEqual(state.selected, "b")
    }

    func testStepByZeroWithNothingSelectedPicksTheFirst() {
        XCTAssertEqual(Navigation.step(["a", "b"], from: nil, by: 0), "a")
    }

    func testDatedInboxTaskScrollsToTheInbox() {
        let snap = makeSnapshot(columns: [[makeTask("i")]], inbox: [makeTask("i")])
        XCTAssertEqual(BoardColumn.containing("i", in: snap), .inbox)
    }

    func testDeadlineWords() {
        let now = Format.date("2026-11-16")!
        XCTAssertEqual(QuickAdd.deadline("tod", now: now), .set("2026-11-16"))
        XCTAssertEqual(QuickAdd.deadline("tomorrow", now: now), .set("2026-11-17"))
        XCTAssertEqual(QuickAdd.deadline("abcdefghij", now: now), .invalid("abcdefghij"))
        XCTAssertEqual(QuickAdd.deadline("2026-12-01T09:00", now: now), .invalid("2026-12-01T09:00"))
    }

    func testTheLastDatePhraseWins() {
        let q = QuickAdd.parse("Call Bob mon about friday")
        XCTAssertEqual(q.dueString, "friday")
        XCTAssertEqual(q.content, "Call Bob mon about")
    }

    func testTheFirstDeadlineWins() {
        let q = QuickAdd.parse("Ship {2026-12-01} {2026-12-05}")
        XCTAssertEqual(q.deadline, .set("2026-12-01"))
        XCTAssertEqual(q.content, "Ship {2026-12-05}")
    }

    func testOneMinute() {
        XCTAssertEqual(Format.minutes(1), "1m")
    }

    func testFlowEmptyAndExactFit() {
        XCTAssertEqual(Flow.arrange(sizes: [], width: 100, spacing: 4).size, .zero)
        let exact = Flow.arrange(sizes: [CGSize(width: 50, height: 10), CGSize(width: 50, height: 10)],
                                 width: 100, spacing: 0)
        XCTAssertEqual(exact.origins.map(\.y), [0, 0])
        XCTAssertEqual(exact.size, CGSize(width: 100, height: 10))
    }
}
