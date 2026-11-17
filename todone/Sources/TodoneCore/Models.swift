import Foundation

/// What the helper's `snapshot` (and every mutating call) returns. Field names
/// mirror todone_core.Todone.snapshot, decoded from snake_case.
public struct Snapshot: Decodable, Sendable, Equatable {
    public var now: String
    public var today: String
    public var planning: Bool
    public var calendarError: String?
    /// "running" while the helper's Google re-authorization waits on the
    /// browser consent screen, "failed" once it gave up; nil otherwise.
    public var calendarReauth: String?
    public var todayColumn: Column
    public var overdue: [TaskItem]
    public var todayTasks: [TaskItem]
    public var columns: [Column]
    public var tray: [TaskItem]
    /// Open tasks in the Inbox itself (not in one of its sections).
    public var inbox: [TaskItem]
    public var timers: [RunningTimer]
    public var result: ApplyResult?
    /// Every project, for `#Project` in the title editor.
    public var projects: [Project]
}

public struct Column: Decodable, Sendable, Equatable, Identifiable {
    public var date: String
    public var capacityMinutes: Int?
    public var capacityOverridden: Bool
    public var plannedMinutes: Int
    public var taskCount: Int
    /// Absent on the today column, whose tasks come split into overdue and today.
    public var tasks: [TaskItem]?

    public var id: String { date }
}

public struct TaskItem: Decodable, Sendable, Equatable, Identifiable {
    public var id: String
    public var content: String
    public var priority: Int
    public var dueDate: String?
    public var originalDueDate: String?
    public var moved: Bool
    public var dueTime: String?
    public var deadline: String?
    public var labels: [String]
    public var project: String?
    public var minutes: Int
    public var sizeLabel: String
    public var pinned: Bool
    public var fixed: Bool
    public var recurring: Bool
    public var movable: Bool
    public var overdue: Bool
    public var atRisk: Bool
    /// The last day a task too big for its day spills into; nil when it fits.
    public var spansUntil: String?
    public var running: Bool
    public var url: String
    public var description: String
    public var commentCount: Int
    /// Open subtasks, nested all the way down.
    public var subtasks: [Subtask]
}

/// An open subtask shown inside its parent's unfolded card.
public struct Subtask: Decodable, Sendable, Equatable, Identifiable {
    public var id: String
    public var content: String
    public var priority: Int
    public var dueDate: String?
    public var labels: [String]
    public var commentCount: Int
    public var url: String
    public var subtasks: [Subtask]

    /// This subtask and everything under it.
    public var descendantCount: Int { subtasks.reduce(subtasks.count) { $0 + $1.descendantCount } }
}

extension TaskItem {
    public var descendantCount: Int { subtasks.reduce(subtasks.count) { $0 + $1.descendantCount } }
}

public struct RunningTimer: Decodable, Sendable, Equatable, Identifiable {
    public var taskId: String
    public var title: String
    public var startedAt: String
    /// A `todoist log` style timer with no task behind it.
    public var isLog: Bool

    public var id: String { taskId }
}

public struct ApplyResult: Decodable, Sendable, Equatable {
    public var updated: Int
    public var skipped: Int
    public var labelsChanged: Int
    public var failed: Int
}

public struct EventColor: Decodable, Sendable, Equatable, Identifiable, Hashable {
    /// nil is the calendar's default color.
    public var colorId: Int?
    public var name: String
    public var hex: String?

    public var id: String { name }

    enum CodingKeys: String, CodingKey { case colorId = "id", name, hex }
}

public struct Details: Decodable, Sendable, Equatable {
    public var task: TaskDetail
    public var comments: [Comment]
    public var projects: [Project]
    public var sections: [Section]
    public var labels: [String]
}

public struct TaskDetail: Decodable, Sendable, Equatable {
    public var id: String
    public var content: String
    public var description: String
    public var priority: Int
    public var dueString: String
    public var deadline: String?
    public var labels: [String]
    public var sizeMinutes: Int
    public var hasNativeDuration: Bool
    public var projectId: String?
    public var sectionId: String?
    public var url: String
}

public struct Comment: Decodable, Sendable, Equatable, Identifiable {
    public var id: String
    public var content: String
    public var postedAt: String?
    public var attachment: Attachment?
}

public struct Attachment: Decodable, Sendable, Equatable {
    public var fileName: String
    public var fileUrl: String?
    public var fileType: String?
}

public struct Project: Decodable, Sendable, Equatable, Identifiable, Hashable {
    public var id: String
    public var name: String
}

public struct Section: Decodable, Sendable, Equatable, Identifiable, Hashable {
    public var id: String
    public var name: String
    public var projectId: String
}

public enum Decoding {
    public static func decode<T: Decodable>(_ type: T.Type, from data: Data) throws -> T {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(type, from: data)
    }
}
