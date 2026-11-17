import Foundation

/// A change shown on a card before Todoist confirms it.
public struct TaskEdit: Equatable, Sendable {
    public var content: String?
    public var priority: Int?
    public var labels: [String]?

    public init(content: String? = nil, priority: Int? = nil, labels: [String]? = nil) {
        self.content = content
        self.priority = priority
        self.labels = labels
    }
}

/// What the app is waiting on Todoist for. Cards show these at once (optimistic
/// updates); each is dropped when its call returns, so a failed call puts the
/// task back as Todoist has it.
public struct PendingEdits: Equatable, Sendable {
    /// Checked off and fading out.
    public var completing: Set<String> = []
    /// Faded out and removed from the lists.
    public var hidden: Set<String> = []
    /// Any call in flight for the task (shows a spinner on its card).
    public var syncing: Set<String> = []
    public var edits: [String: TaskEdit] = [:]

    public init() {}

    public var isEmpty: Bool { completing.isEmpty && hidden.isEmpty && syncing.isEmpty && edits.isEmpty }

    /// Drop everything recorded for `id`.
    public mutating func settle(_ id: String) {
        completing.remove(id)
        hidden.remove(id)
        syncing.remove(id)
        edits[id] = nil
    }

    /// The snapshot as the cards should show it: hidden tasks (and subtasks)
    /// gone, pending edits applied.
    public func apply(to snapshot: Snapshot) -> Snapshot {
        if isEmpty { return snapshot }
        var snap = snapshot
        snap.overdue = tasks(snap.overdue)
        snap.todayTasks = tasks(snap.todayTasks)
        snap.tray = tasks(snap.tray)
        snap.inbox = tasks(snap.inbox)
        snap.columns = snap.columns.map { column in
            var column = column
            column.tasks = column.tasks.map(tasks)
            return column
        }
        return snap
    }

    private func tasks(_ list: [TaskItem]) -> [TaskItem] {
        list.filter { !hidden.contains($0.id) }.map { task in
            var task = task
            if let edit = edits[task.id] {
                task.content = edit.content ?? task.content
                task.priority = edit.priority ?? task.priority
                task.labels = edit.labels ?? task.labels
                task.fixed = task.labels.contains { $0.lowercased() == "fixed" }
            }
            task.subtasks = subtasks(task.subtasks)
            return task
        }
    }

    private func subtasks(_ list: [Subtask]) -> [Subtask] {
        list.filter { !hidden.contains($0.id) }.map { sub in
            var sub = sub
            if let edit = edits[sub.id] {
                sub.content = edit.content ?? sub.content
                sub.priority = edit.priority ?? sub.priority
                sub.labels = edit.labels ?? sub.labels
            }
            sub.subtasks = subtasks(sub.subtasks)
            return sub
        }
    }
}

/// Keyboard selection over the cards on screen.
public enum Navigation {
    /// Task ids in reading order: the Inbox panel, overdue, today, each day
    /// column, then the tray (the planner parts only when it's open). A task
    /// shown twice (a dated Inbox task) counts once, where it first appears.
    public static func order(_ snapshot: Snapshot, planner: Bool) -> [String] {
        var lists = [snapshot.overdue, snapshot.todayTasks]
        if planner {
            lists.insert(snapshot.inbox, at: 0)
            lists += snapshot.columns.map { $0.tasks ?? [] }
            lists.append(snapshot.tray)
        }
        var seen = Set<String>()
        return lists.joined().map(\.id).filter { seen.insert($0).inserted }
    }

    /// The id `delta` steps from `current`, stopping at either end. With nothing
    /// selected (or the selection gone), down picks the first and up the last.
    public static func step(_ ids: [String], from current: String?, by delta: Int) -> String? {
        guard !ids.isEmpty else { return nil }
        guard let current, let index = ids.firstIndex(of: current) else {
            return delta >= 0 ? ids.first : ids.last
        }
        return ids[min(max(index + delta, 0), ids.count - 1)]
    }
}

/// What a key press on the board means.
public enum KeyCommand: Equatable, Sendable {
    case next, previous, toggle, edit, collapse
    case priority(Int)

    public static let returnKey: UInt16 = 36
    public static let enterKey: UInt16 = 76
    public static let escapeKey: UInt16 = 53
    public static let downKey: UInt16 = 125
    public static let upKey: UInt16 = 126

    /// nil for keys the board leaves alone, and for any ⌘, ⌃ or ⌥ combination
    /// (those belong to the menus).
    public static func from(keyCode: UInt16, characters: String?, modified: Bool) -> KeyCommand? {
        if modified { return nil }
        switch keyCode {
        case returnKey, enterKey: return .toggle
        case escapeKey: return .collapse
        case downKey: return .next
        case upKey: return .previous
        default: break
        }
        switch characters?.lowercased() {
        case "j": return .next
        case "k": return .previous
        case "e": return .edit
        case let digit? where ["1", "2", "3", "4"].contains(digit): return .priority(Int(digit)!)
        default: return nil
        }
    }
}

extension Subtask {
    /// A subtask tree as rows in display order, each with its nesting depth (0 for direct children).
    public static func flatten(_ list: [Subtask], depth: Int = 0) -> [(depth: Int, subtask: Subtask)] {
        list.flatMap { [(depth, $0)] + flatten($0.subtasks, depth: depth + 1) }
    }
}

extension Snapshot {
    /// Every card in the snapshot, in reading order (a task shown twice appears twice).
    public var allTasks: [TaskItem] {
        var out: [TaskItem] = inbox + overdue
        out += todayTasks
        for column in columns { out += column.tasks ?? [] }
        return out + tray
    }

    /// The title of a task or subtask anywhere in the snapshot.
    public func title(of id: String) -> String? {
        for task in allTasks {
            if task.id == id { return task.content }
            if let sub = Subtask.flatten(task.subtasks).first(where: { $0.subtask.id == id }) { return sub.subtask.content }
        }
        return nil
    }
}

extension Navigation {
    /// Where the selection goes when `removed` leaves the list: the next card,
    /// else the previous one, else nothing. Any other selection stays.
    public static func afterRemoving(_ removed: String, from ids: [String], selected: String?) -> String? {
        guard selected == removed else { return selected }
        let rest = ids.filter { $0 != removed }
        guard let index = ids.firstIndex(of: removed), !rest.isEmpty else { return nil }
        return rest[min(index, rest.count - 1)]
    }
}

/// Which card is selected, unfolded and being edited, and what each key does to that.
public struct BoardState: Equatable, Sendable {
    public var selected: String?
    public var expanded: Set<String>
    public var editing: String?

    /// What a key did, beyond changing the state.
    public struct Outcome: Equatable, Sendable {
        /// False when it isn't the board's key, so it should go on to the rest of the app.
        public var handled = true
        /// A card it unfolded (its comments want loading).
        public var unfolded: String?
        /// A priority to write.
        public var priority: PriorityChange?

        public static let ignored = Outcome(handled: false)
        public static let handled = Outcome()
        public static func unfolded(_ id: String) -> Outcome { Outcome(unfolded: id) }
        public static func priority(_ id: String, _ p: Int) -> Outcome { Outcome(priority: PriorityChange(id: id, priority: p)) }
    }

    public struct PriorityChange: Equatable, Sendable {
        public var id: String
        public var priority: Int
    }

    public init(selected: String? = nil, expanded: Set<String> = [], editing: String? = nil) {
        self.selected = selected
        self.expanded = expanded
        self.editing = editing
    }

    /// Fold or unfold `id`; folding also ends editing its title. True when it unfolded.
    @discardableResult
    public mutating func toggle(_ id: String) -> Bool {
        if expanded.remove(id) != nil {
            if editing == id { editing = nil }
            return false
        }
        expanded.insert(id)
        return true
    }

    public mutating func handle(_ command: KeyCommand, order: [String]) -> Outcome {
        switch command {
        case .next: selected = Navigation.step(order, from: selected, by: 1)
        case .previous: selected = Navigation.step(order, from: selected, by: -1)
        case .collapse: collapse()
        case .toggle, .edit, .priority: return act(command, order: order)
        }
        return .handled
    }

    /// Esc: fold the selected card if it's open, else drop the selection.
    private mutating func collapse() {
        if let id = selected, expanded.contains(id) { toggle(id) } else { selected = nil }
    }

    private mutating func act(_ command: KeyCommand, order: [String]) -> Outcome {
        guard let id = selected, order.contains(id) else { return .ignored }
        switch command {
        case .priority(let p): return .priority(id, p)
        case .edit:
            editing = id
            return expanded.insert(id).inserted ? .unfolded(id) : .handled
        default:
            return toggle(id) ? .unfolded(id) : .handled
        }
    }
}

/// The board's columns, for scrolling the selected card's column into view.
public enum BoardColumn: Hashable, Sendable {
    case inbox, today, tray
    case day(String)

    public static func containing(_ id: String, in snapshot: Snapshot) -> BoardColumn? {
        let lists: [(BoardColumn, [TaskItem])] = [(.today, snapshot.overdue + snapshot.todayTasks), (.inbox, snapshot.inbox)]
            + snapshot.columns.map { (.day($0.date), $0.tasks ?? []) } + [(.tray, snapshot.tray)]
        return lists.first { $0.1.contains { $0.id == id } }?.0
    }
}
