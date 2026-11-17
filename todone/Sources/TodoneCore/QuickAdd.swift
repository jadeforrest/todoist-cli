import Foundation

/// Todoist's quick-add syntax, read out of an edited title: `p1`–`p4` priority,
/// `@label`, `#Project`, `{deadline}`, and a due date phrase ("tomorrow 3pm",
/// "next fri", "nov 20", "every mon"). The date phrase is only found here; it is
/// sent to Todoist as `due_string`, so Todoist itself decides what it means.
///
/// Tokens that were already in the original title are left as text, so editing
/// "Call Tom" doesn't suddenly read "Tom" as tomorrow.
public struct QuickAdd: Equatable {
    public enum Kind: String, Equatable, Sendable { case priority, label, project, deadline, due }

    public struct Token: Equatable, Sendable {
        public var kind: Kind
        /// The text as typed, e.g. "p1", "@deep", "{fri}", "next week".
        public var text: String
    }

    public enum Deadline: Equatable, Sendable {
        case set(String)  // ISO date
        case clear
        case invalid(String)
    }

    public var content: String
    public var priority: Int?
    public var labels: [String] = []
    public var project: Project?
    public var dueString: String?
    public var deadline: Deadline?
    public var tokens: [Token] = []

    public enum ParseError: Error, LocalizedError, Equatable {
        case deadline(String)
        case emptyTitle

        public var errorDescription: String? {
            switch self {
            case .deadline(let text): return "Can't read the deadline {\(text)}; try a date like {fri} or {2026-12-01}."
            case .emptyTitle: return "The title can't be empty."
            }
        }
    }

    /// Parse `text`. `keepDateAsText` leaves a due phrase in the title (clicking
    /// the date in the preview does this, like un-highlighting it in Todoist).
    public static func parse(_ text: String, original: String = "", projects: [Project] = [],
                             keepDateAsText: Bool = false, now: Date = Date()) -> QuickAdd {
        let stale = original.isEmpty ? [] : Set(parse(original, projects: projects, now: now).tokens.map(Key.init))
        var parser = Parser(text: text, stale: stale)
        var result = QuickAdd(content: "")
        if let (inner, token) = parser.take(Patterns.deadline, kind: .deadline) {
            result.deadline = deadline(inner, now: now)
            result.tokens.append(token)
        }
        for (name, token) in parser.takeAll(Patterns.label, kind: .label) {
            if !result.labels.contains(where: { $0.caseInsensitiveCompare(name) == .orderedSame }) {
                result.labels.append(name)
            }
            result.tokens.append(token)
        }
        if let (project, token) = parser.takeProject(projects) {
            result.project = project
            result.tokens.append(token)
        }
        if let (digit, token) = parser.take(Patterns.priority, kind: .priority, last: true) {
            result.priority = Int(digit)
            result.tokens.append(token)
        }
        if !keepDateAsText, let (phrase, token) = parser.takeDue() {
            result.dueString = phrase
            result.tokens.append(token)
        }
        result.content = parser.remaining
        return result
    }

    /// The fields to send to the helper's `quick_update` for a task that has
    /// `content`, `priority` and `labels` now. Empty when nothing changed.
    public func changes(content current: String, priority currentPriority: Int,
                        labels currentLabels: [String]) throws -> [String: Any] {
        if content.isEmpty { throw ParseError.emptyTitle }
        var out: [String: Any] = [:]
        if content != current { out["content"] = content }
        if let priority, priority != currentPriority { out["priority"] = priority }
        let added = labels.filter { new in !currentLabels.contains { $0.caseInsensitiveCompare(new) == .orderedSame } }
        if !added.isEmpty { out["labels"] = currentLabels + added }
        if let project {
            out["project_id"] = project.id
            out["section_id"] = NSNull()  // into the project itself, as Todoist's #Project does
        }
        if let dueString { out["due_string"] = dueString }
        switch deadline {
        case .set(let iso): out["deadline"] = iso
        case .clear: out["deadline"] = NSNull()
        case .invalid(let text): throw ParseError.deadline(text)
        case nil: break
        }
        return out
    }

    /// The labels the task has once these changes are written.
    public func mergedLabels(_ currentLabels: [String]) -> [String] {
        currentLabels + labels.filter { new in
            !currentLabels.contains { $0.caseInsensitiveCompare(new) == .orderedSame }
        }
    }

    // MARK: - Internals

    private struct Key: Hashable {
        let kind: Kind
        let text: String
        init(_ token: Token) { kind = token.kind; text = token.text.lowercased() }
    }

    static func deadline(_ inner: String, now: Date) -> Deadline {
        let text = inner.trimmingCharacters(in: .whitespaces).lowercased()
        if ["", "none", "no deadline", "no date"].contains(text) { return .clear }
        if Format.date(text) != nil, text.count == 10 { return .set(text) }
        let calendar = Calendar.current
        let offsets = ["today": 0, "tod": 0, "tomorrow": 1, "tom": 1]
        if let days = offsets[text], let day = calendar.date(byAdding: .day, value: days, to: now) {
            return .set(Format.iso(day))
        }
        guard let detector = try? NSDataDetector(types: NSTextCheckingResult.CheckingType.date.rawValue),
              let match = detector.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)),
              match.range.length == (text as NSString).length,
              let date = match.date
        else { return .invalid(inner) }
        return .set(Format.iso(date))
    }

    /// Removes matched tokens from the text as it goes, so later patterns can't
    /// see text an earlier one claimed (a date inside `@tomorrow`, say).
    private struct Parser {
        var text: String
        let stale: Set<Key>

        var remaining: String {
            text.split(whereSeparator: \.isWhitespace).joined(separator: " ")
        }

        func fresh(_ kind: Kind, _ value: String) -> Bool {
            !stale.contains(Key(Token(kind: kind, text: value)))
        }

        func matches(_ regex: NSRegularExpression) -> [NSTextCheckingResult] {
            regex.matches(in: text, range: NSRange(text.startIndex..., in: text))
        }

        func string(_ range: NSRange) -> String {
            Range(range, in: text).map { String(text[$0]) } ?? ""
        }

        mutating func remove(_ range: NSRange) {
            if let r = Range(range, in: text) { text.replaceSubrange(r, with: " ") }
        }

        /// The first (or last) fresh match: its first capture group and its token.
        mutating func take(_ regex: NSRegularExpression, kind: Kind, last: Bool = false) -> (String, Token)? {
            let found = matches(regex).filter { fresh(kind, string($0.range)) }
            guard let match = last ? found.last : found.first else { return nil }
            let token = Token(kind: kind, text: string(match.range))
            let value = string(match.range(at: 1))
            // Taking the last (priority) clears the earlier ones too: the last one typed wins.
            for removed in (last ? found : [match]).reversed() { remove(removed.range) }
            return (value, token)
        }

        mutating func takeAll(_ regex: NSRegularExpression, kind: Kind) -> [(String, Token)] {
            let found = matches(regex).filter { fresh(kind, string($0.range)) }
            let out = found.map { (string($0.range(at: 1)), Token(kind: kind, text: string($0.range))) }
            for match in found.reversed() { remove(match.range) }
            return out
        }

        /// `#Name`, matching the longest project name (spaces allowed, or typed without them).
        mutating func takeProject(_ projects: [Project]) -> (Project, Token)? {
            let byLength = projects.sorted { $0.name.count > $1.name.count }
            for match in matches(Patterns.hash) {
                let start = match.range.location + 1
                let rest = (text as NSString).substring(from: start)
                for project in byLength {
                    guard let length = Patterns.prefixLength(of: project.name, in: rest) else { continue }
                    let range = NSRange(location: match.range.location, length: length + 1)
                    let typed = string(range)
                    guard fresh(.project, typed) else { continue }
                    remove(range)
                    return (project, Token(kind: .project, text: typed))
                }
            }
            return nil
        }

        /// A recurrence wins over a one-off date; otherwise the last date phrase.
        mutating func takeDue() -> (String, Token)? {
            for regex in [Patterns.recurring, Patterns.date] {
                if let match = matches(regex).last(where: { fresh(.due, string($0.range)) }) {
                    let phrase = string(match.range)
                    remove(match.range)
                    return (phrase, Token(kind: .due, text: phrase))
                }
            }
            return nil
        }
    }
}

enum Patterns {
    private static func regex(_ pattern: String) -> NSRegularExpression {
        // The patterns are constants; a typo should fail every test, loudly.
        try! NSRegularExpression(pattern: pattern, options: [.caseInsensitive])
    }

    static let weekday = "mon(?:day)?|tue(?:s|sday)?|wed(?:nesday)?|thu(?:rs?|rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?"
    static let month = "jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
        + "|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
    static let time = #"(?:\d{1,2}(?::\d{2})?\s?(?:am|pm)|\d{1,2}:\d{2}|noon|midnight)"#
    private static let ordinal = #"\d{1,2}(?:st|nd|rd|th)?"#
    private static let day = [
        "today", "tod", "tonight", "tomorrow", "tom",
        #"no\s+(?:due\s+)?date"#,
        #"next\s+(?:week|month|year|weekend)"#, #"this\s+weekend"#,
        #"(?:next\s+|this\s+)?(?:\#(weekday))"#,
        #"in\s+\d+\s+(?:days?|weeks?|months?|years?)"#,
        #"(?:\#(month))\.?\s+\#(ordinal)(?:,?\s+\d{4})?"#,
        #"\#(ordinal)\s+(?:\#(month))(?:\s+\d{4})?"#,
        #"\d{4}-\d{2}-\d{2}"#, #"\d{1,2}/\d{1,2}(?:/\d{2,4})?"#,
    ].joined(separator: "|")

    static let date = regex(#"(?<![\w@#/{])(?:(?:\#(day))(?:\s+(?:at\s+)?\#(time))?|(?:at\s+)?\#(time))(?![\w}]|\.\w)"#)

    private static let repeatWord = [
        #"days?"#, #"weekdays?"#, #"workdays?"#, #"weeks?"#, #"months?"#, #"years?"#,
        "other", "last", "first", "second", "third", "fourth", "morning", "afternoon", "evening", "night",
        weekday, month, time, #"\d+(?:st|nd|rd|th)?"#, "at", "and",
    ].joined(separator: "|")
    static let recurring = regex(#"(?<![\w@#/{])every!?(?:[\s,]+(?:\#(repeatWord))(?![\w:]))+"#)

    static let priority = regex(#"(?<![\w@#/])p([1-4])(?!\w)"#)
    static let label = regex(#"(?<![\w@])@([\p{L}\p{N}_\-/]+(?:\.[\p{L}\p{N}_\-/]+)*)"#)
    static let hash = regex(#"(?<![\w#])#(?=\S)"#)
    static let deadline = regex(#"\{([^{}]*)\}"#)

    /// How much of `text` the project `name` takes up at its start, comparing
    /// case-insensitively and also with the name's spaces removed; nil if it
    /// doesn't start there or runs on into a longer word.
    static func prefixLength(of name: String, in text: String) -> Int? {
        let lower = text.lowercased()
        for candidate in [name.lowercased(), name.lowercased().replacingOccurrences(of: " ", with: "")]
        where !candidate.isEmpty && lower.hasPrefix(candidate) {
            let end = lower.index(lower.startIndex, offsetBy: candidate.count)
            if end == lower.endIndex || !(lower[end].isLetter || lower[end].isNumber) {
                return (String(text.prefix(candidate.count)) as NSString).length
            }
        }
        return nil
    }
}
