import SwiftUI
import TodoneCore

/// What an unfolded card shows under its title: description, subtasks (all the
/// way down) and comments.
struct TaskExpansion: View {
    @EnvironmentObject var model: AppModel
    let task: TaskItem
    @State private var newComment = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Divider().padding(.top, 8)
            if !task.description.isEmpty {
                MarkdownText(text: task.description)
                    .appFont(.callout)
                    .foregroundStyle(.primary.opacity(0.85))
            }
            if !task.subtasks.isEmpty {
                VStack(alignment: .leading, spacing: 2) {
                    ExpansionHeading(title: "Subtasks", count: task.descendantCount, icon: "list.bullet.indent")
                    ForEach(Subtask.flatten(task.subtasks), id: \.subtask.id) { row in
                        SubtaskRow(subtask: row.subtask, depth: row.depth)
                            .transition(.opacity.combined(with: .move(edge: .leading)))
                    }
                }
            }
            comments
            Text("Click the title to edit · p1–p4 · @label · #Project · {deadline} · dates like “tomorrow 3pm”")
                .appFont(.caption)
                .foregroundStyle(.tertiary)
        }
    }

    @ViewBuilder
    private var comments: some View {
        let loaded = model.comments[task.id]
        VStack(alignment: .leading, spacing: 6) {
            ExpansionHeading(title: "Comments", count: loaded?.count ?? task.commentCount, icon: "text.bubble")
            if let loaded {
                ForEach(loaded) { CommentBubble(comment: $0) }
            } else if task.commentCount > 0 {
                ProgressView().controlSize(.small)
            }
            HStack {
                TextField("Add a comment", text: $newComment)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit(send)
                Button("Add", action: send).disabled(newComment.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            .appFont(.callout)
        }
    }

    private func send() {
        let text = newComment.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        newComment = ""
        Task { await model.addComment(task.id, text) }
    }
}

private struct ExpansionHeading: View {
    let title: String
    let count: Int
    let icon: String

    var body: some View {
        HStack(spacing: 4) {
            Image(systemName: icon)
            Text(title.uppercased()).appFont(.caption, weight: .bold)
            Text("\(count)").appFont(.caption)
        }
        .foregroundStyle(.secondary)
    }
}

/// One subtask inside an unfolded card, indented by its depth.
private struct SubtaskRow: View {
    @EnvironmentObject var model: AppModel
    let subtask: Subtask
    let depth: Int

    private var completing: Bool { model.pending.completing.contains(subtask.id) }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            DoneButton(id: subtask.id, priority: subtask.priority)
                .appFont(.body)
            if model.editingTitleId == subtask.id {
                TitleEditor(id: subtask.id, content: subtask.content, priority: subtask.priority, labels: subtask.labels)
            } else {
                VStack(alignment: .leading, spacing: 2) {
                    Text(subtask.content)
                        .strikethrough(completing)
                        .foregroundStyle(completing ? .secondary : .primary)
                        .onTapGesture { model.editingTitleId = subtask.id }
                        .help("Click to edit the title")
                    meta
                }
                if model.pending.syncing.contains(subtask.id) { SyncSpinner() }
                Spacer(minLength: 0)
                if let url = URL(string: subtask.url) {
                    Link(destination: url) { Image(systemName: "link") }
                        .buttonStyle(.borderless)
                        .foregroundStyle(.secondary)
                        .help("Open in Todoist")
                }
            }
        }
        .appFont(.callout)
        .padding(.leading, CGFloat(depth) * 18)
        .padding(.vertical, 3)
        .opacity(completing ? 0.5 : 1)
    }

    @ViewBuilder
    private var meta: some View {
        let hasMeta = subtask.priority < 4 || subtask.dueDate != nil || !subtask.labels.isEmpty || subtask.commentCount > 0
        if hasMeta {
            FlowRow {
                if subtask.priority < 4 {
                    Text("P\(subtask.priority)").foregroundStyle(Theme.priority(subtask.priority)).bold()
                }
                if let due = subtask.dueDate { Label(Format.shortDate(due), systemImage: "calendar") }
                if subtask.commentCount > 0 { Label("\(subtask.commentCount)", systemImage: "text.bubble") }
                ForEach(subtask.labels, id: \.self) { LabelChip(name: $0) }
            }
            .appFont(.caption)
            .foregroundStyle(.secondary)
        }
    }
}

/// Edit a title in place with Todoist's quick-add syntax. The tokens it finds
/// show underneath as they're typed; Return saves, Esc cancels.
struct TitleEditor: View {
    @EnvironmentObject var model: AppModel
    let id: String
    let content: String
    let priority: Int
    let labels: [String]
    @State private var text = ""
    @State private var keepDate = false
    @State private var error: String?
    @FocusState private var focused: Bool

    private var parsed: QuickAdd {
        QuickAdd.parse(text, original: content, projects: model.snapshot?.projects ?? [], keepDateAsText: keepDate)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            TextField("Task name", text: $text, axis: .vertical)
                .textFieldStyle(.plain)
                .appFont(.body, weight: .semibold)
                .padding(6)
                .background(RoundedRectangle(cornerRadius: 6).fill(Color(nsColor: .textBackgroundColor)))
                .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Color.accentColor, lineWidth: 1.5))
                .focused($focused)
                .onSubmit(save)
                .onExitCommand { model.editingTitleId = nil }
            HStack(spacing: 6) {
                ForEach(Array(parsed.tokens.enumerated()), id: \.offset) { _, token in TokenChip(token: token) }
                if keepDate {
                    Button("Read dates again") { keepDate = false }.buttonStyle(.link)
                }
                Spacer()
                Button("Cancel") { model.editingTitleId = nil }
                Button("Save", action: save).buttonStyle(.borderedProminent).controlSize(.small)
            }
            .appFont(.caption)
            if let error { Text(error).foregroundStyle(.red).appFont(.caption) }
        }
        .onAppear {
            text = content
            focused = true
        }
        .onChange(of: text) { _, _ in error = nil }
        .environment(\.keepDate, $keepDate)
    }

    private func save() {
        let result = parsed
        do {
            let changes = try result.changes(content: content, priority: priority, labels: labels)
            Task { await model.saveTitle(id, result, changes: changes, labels: labels) }
        } catch {
            self.error = error.localizedDescription
        }
    }
}

private struct KeepDateKey: EnvironmentKey {
    static let defaultValue: Binding<Bool> = .constant(false)
}

extension EnvironmentValues {
    /// Lets the due-date chip tell its editor to leave the phrase as text.
    fileprivate var keepDate: Binding<Bool> {
        get { self[KeepDateKey.self] }
        set { self[KeepDateKey.self] = newValue }
    }
}

/// A parsed token, colored by kind like Todoist's highlights. The date chip can
/// be clicked to keep the phrase as plain title text.
private struct TokenChip: View {
    @Environment(\.keepDate) private var keepDate
    let token: QuickAdd.Token

    var body: some View {
        let chip = Label(token.text, systemImage: icon)
            .padding(.horizontal, 6).padding(.vertical, 2)
            .foregroundStyle(color)
            .background(Capsule().fill(color.opacity(0.15)))
        if token.kind == .due {
            Button { keepDate.wrappedValue = true } label: { chip }
                .buttonStyle(.plain)
                .help("Due “\(token.text)”, as Todoist reads it. Click to keep it in the title instead.")
        } else {
            chip
        }
    }

    private var icon: String {
        switch token.kind {
        case .priority: return "flag.fill"
        case .label: return "tag"
        case .project: return "number"
        case .deadline: return "flag.checkered"
        case .due: return "calendar"
        }
    }

    private var color: Color {
        switch token.kind {
        case .priority: return .red
        case .label: return .brown
        case .project: return .blue
        case .deadline: return .purple
        case .due: return .green
        }
    }
}

/// Inline Markdown (links, bold, code) as Todoist descriptions use it.
struct MarkdownText: View {
    let text: String

    var body: some View {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        let attributed = (try? AttributedString(markdown: text, options: options)) ?? AttributedString(text)
        Text(attributed).textSelection(.enabled)
    }
}

/// A comment: who/when on top, the text below. `edit` adds an Edit button.
struct CommentBubble: View {
    let comment: Comment
    var edit: (() -> Void)?

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 4) {
                Image(systemName: "person.crop.circle.fill").foregroundStyle(.secondary)
                if let posted = comment.postedAt { Text(Format.shortDate(posted)).foregroundStyle(.secondary) }
                Spacer()
                if let edit { Button("Edit", action: edit).buttonStyle(.link) }
            }
            .appFont(.caption)
            MarkdownText(text: comment.content)
            if let attachment = comment.attachment { AttachmentLink(attachment: attachment) }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.primary.opacity(0.05)))
        .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Color.primary.opacity(0.08)))
    }
}

struct AttachmentLink: View {
    let attachment: Attachment

    var body: some View {
        HStack(spacing: 4) {
            Image(systemName: "paperclip")
            if let url = attachment.fileUrl.flatMap(URL.init(string:)) {
                Link(attachment.fileName, destination: url)
            } else {
                Text(attachment.fileName)
            }
        }
        .appFont(.caption)
    }
}
