import Foundation

public enum Format {
    /// "2h 3m", "45m", "0m"; "—" when unknown.
    public static func minutes(_ value: Int?) -> String {
        guard let value else { return "—" }
        if value <= 0 { return "0m" }
        let (hours, mins) = value.quotientAndRemainder(dividingBy: 60)
        switch (hours, mins) {
        case (0, _): return "\(mins)m"
        case (_, 0): return "\(hours)h"
        default: return "\(hours)h \(mins)m"
        }
    }

    /// Minutes from "1h30m", "1h 30m", "90m", "2h" or a bare number of minutes; nil if unreadable.
    public static func parseMinutes(_ text: String) -> Int? {
        let compact = text.lowercased().filter { !$0.isWhitespace }
        if compact.isEmpty { return nil }
        if let plain = Int(compact) { return plain >= 0 ? plain : nil }
        let pattern = #/^(?:(\d+)h)?(?:(\d+)m)?$/#
        guard let match = compact.wholeMatch(of: pattern), match.1 != nil || match.2 != nil else { return nil }
        let hours = match.1.flatMap { Int($0) } ?? 0
        let mins = match.2.flatMap { Int($0) } ?? 0
        return hours * 60 + mins
    }

    private static let isoDay: DateFormatter = {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()

    public static func date(_ iso: String?) -> Date? {
        iso.flatMap { isoDay.date(from: String($0.prefix(10))) }
    }

    public static func iso(_ date: Date) -> String { isoDay.string(from: date) }

    /// "Tue Nov 17".
    public static func dayLabel(_ iso: String) -> String {
        guard let d = date(iso) else { return iso }
        return d.formatted(.dateTime.weekday(.abbreviated).month(.abbreviated).day())
    }

    /// "Nov 17".
    public static func shortDate(_ iso: String?) -> String {
        guard let d = date(iso) else { return "—" }
        return d.formatted(.dateTime.month(.abbreviated).day())
    }

    /// "1:05:09" or "4:09" elapsed since an ISO-8601 timestamp.
    public static func elapsed(since iso: String, now: Date = Date()) -> String {
        guard let start = parseTimestamp(iso) else { return "" }
        let seconds = max(0, Int(now.timeIntervalSince(start)))
        let (h, rest) = seconds.quotientAndRemainder(dividingBy: 3600)
        let (m, s) = rest.quotientAndRemainder(dividingBy: 60)
        return h > 0 ? String(format: "%d:%02d:%02d", h, m, s) : String(format: "%d:%02d", m, s)
    }

    public static func parseTimestamp(_ iso: String) -> Date? {
        let withFraction = ISO8601DateFormatter()
        withFraction.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return withFraction.date(from: iso) ?? ISO8601DateFormatter().date(from: iso)
    }
}

/// The fields the details form changed, as `update_details` expects them.
public struct DetailChanges {
    public var content: String
    public var description: String
    public var priority: Int
    public var dueString: String
    public var deadline: String?
    public var labels: [String]
    public var sizeMinutes: Int
    public var projectId: String?
    public var sectionId: String?

    public init(from task: TaskDetail) {
        content = task.content
        description = task.description
        priority = task.priority
        dueString = task.dueString
        deadline = task.deadline
        labels = task.labels
        sizeMinutes = task.sizeMinutes
        projectId = task.projectId
        sectionId = task.sectionId
    }

    /// Only what differs from `original`; nil values become JSON null.
    public func diff(from original: TaskDetail) -> [String: Any] {
        let base = DetailChanges(from: original)
        var out: [String: Any] = [:]
        if content != base.content { out["content"] = content }
        if description != base.description { out["description"] = description }
        if priority != base.priority { out["priority"] = priority }
        if dueString != base.dueString { out["due_string"] = dueString }
        if deadline != base.deadline { out["deadline"] = deadline ?? NSNull() }
        if labels != base.labels { out["labels"] = labels }
        if sizeMinutes != base.sizeMinutes { out["size_minutes"] = sizeMinutes }
        if projectId != base.projectId || sectionId != base.sectionId {
            out["project_id"] = projectId ?? NSNull()
            out["section_id"] = sectionId ?? NSNull()
        }
        return out
    }
}

public enum DetailError: Error, LocalizedError, Equatable {
    case badSize(String)

    public var errorDescription: String? {
        switch self {
        case .badSize(let text): return "Size \"\(text)\" should look like 30m, 1h30m or 2h."
        }
    }
}

extension DetailChanges {
    /// The update to send: the changed fields, with the size read from the form's text.
    public func payload(sizeText: String, original: TaskDetail) throws -> [String: Any] {
        guard let minutes = Format.parseMinutes(sizeText) else { throw DetailError.badSize(sizeText) }
        var copy = self
        copy.sizeMinutes = minutes
        return copy.diff(from: original)
    }

    public mutating func toggleLabel(_ name: String) {
        if labels.contains(name) { labels.removeAll { $0 == name } } else { labels.append(name) }
    }

    /// Add a typed label; blank or already-present names are ignored.
    public mutating func addLabel(_ text: String) {
        let name = text.trimmingCharacters(in: .whitespaces)
        if !name.isEmpty && !labels.contains(name) { labels.append(name) }
    }
}

/// Left-to-right wrapping layout math for FlowRow.
public enum Flow {
    public static func arrange(sizes: [CGSize], width: CGFloat, spacing: CGFloat) -> (size: CGSize, origins: [CGPoint]) {
        var origins: [CGPoint] = []
        var x: CGFloat = 0, y: CGFloat = 0, lineHeight: CGFloat = 0, widest: CGFloat = 0
        for size in sizes {
            if x > 0 && x + size.width > width {
                x = 0
                y += lineHeight + spacing
                lineHeight = 0
            }
            origins.append(CGPoint(x: x, y: y))
            x += size.width + spacing
            lineHeight = max(lineHeight, size.height)
            widest = max(widest, x - spacing)
        }
        return (CGSize(width: widest, height: y + lineHeight), origins)
    }
}
