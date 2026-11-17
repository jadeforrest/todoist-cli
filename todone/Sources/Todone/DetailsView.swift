import SwiftUI
import TodoneCore

/// Edit a task's fields and comments. Loads fresh from Todoist when opened.
/// Laid out as cards on the window background so fields stand out: a header
/// band tinted by priority with the title, then Description, Schedule,
/// Organize and Comments.
struct DetailsView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @Environment(\.textScale) private var scale
    let taskId: String

    @State private var details: Details?
    @State private var form: DetailChanges?
    @State private var sizeText = ""
    @State private var newLabel = ""
    @State private var newComment = ""
    @State private var error: String?
    @State private var saving = false

    var body: some View {
        VStack(spacing: 0) {
            if let details, let form {
                header(details, form: form)
                ScrollView { content(details).padding(20) }
                    .background(Color(nsColor: .windowBackgroundColor))
                footer
            } else if let error {
                Text(error).foregroundStyle(.red).padding()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .frame(width: 600 * scale, height: 740 * scale)
        .task { await load() }
    }

    private var binding: Binding<DetailChanges> { Binding(get: { form! }, set: { form = $0 }) }

    private func header(_ details: Details, form: DetailChanges) -> some View {
        let color = Theme.priority(form.priority)
        return HStack(alignment: .top, spacing: 12) {
            RoundedRectangle(cornerRadius: 2).fill(color).frame(width: 5)
            VStack(alignment: .leading, spacing: 6) {
                Text(breadcrumb(details, form: form))
                    .appFont(.caption, weight: .semibold)
                    .foregroundStyle(.secondary)
                TextField("Task name", text: binding.content, axis: .vertical)
                    .textFieldStyle(.plain)
                    .appFont(.title2, weight: .semibold)
            }
            Spacer()
            if let url = URL(string: details.task.url) {
                Link(destination: url) {
                    Label("Todoist", systemImage: "arrow.up.right.square")
                }
                .help("Open in Todoist")
            }
        }
        .padding(20)
        .fixedSize(horizontal: false, vertical: true)
        .background(color.opacity(0.10))
        .overlay(alignment: .bottom) { Divider() }
    }

    private func breadcrumb(_ details: Details, form: DetailChanges) -> String {
        let project = details.projects.first { $0.id == form.projectId }?.name ?? "No project"
        let section = details.sections.first { $0.id == form.sectionId }?.name
        return [project, section].compactMap { $0 }.joined(separator: "  ›  ").uppercased()
    }

    @ViewBuilder
    private func content(_ details: Details) -> some View {
        VStack(alignment: .leading, spacing: 16) {
            FormCard(title: "Description", icon: "text.alignleft") {
                TextEditor(text: binding.description)
                    .scrollContentBackground(.hidden)
                    .frame(minHeight: 90)
                    .fieldBox()
            }
            FormCard(title: "Schedule", icon: "calendar") {
                Grid(alignment: .leading, horizontalSpacing: 14, verticalSpacing: 12) {
                    FieldRow("Due", icon: "calendar") {
                        TextField("e.g. tomorrow, every mon, none", text: binding.dueString)
                            .textFieldStyle(.plain).fieldBox()
                    }
                    FieldRow("Deadline", icon: "flag.checkered") { DeadlinePicker(deadline: binding.deadline) }
                    FieldRow("Size", icon: "hourglass") {
                        TextField("30m", text: $sizeText)
                            .textFieldStyle(.plain).fieldBox().scaledWidth(110)
                            .help(details.task.hasNativeDuration ? "Writes the task's duration" : "Writes a size hint into the title")
                    }
                }
            }
            FormCard(title: "Organize", icon: "folder") {
                Grid(alignment: .leading, horizontalSpacing: 14, verticalSpacing: 12) {
                    FieldRow("Priority", icon: "flag") { PriorityPicker(priority: binding.priority) }
                    FieldRow("Project", icon: "number") { ProjectPicker(details: details, form: binding) }
                    FieldRow("Labels", icon: "tag") { labelEditor(details) }
                }
            }
            FormCard(title: "Comments", icon: "text.bubble", count: details.comments.count) {
                CommentsView(taskId: taskId, comments: details.comments, newComment: $newComment) { updated in
                    self.details?.comments = updated.comments
                }
            }
        }
    }

    private func labelEditor(_ details: Details) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            FlowRow {
                ForEach(Array(Set(details.labels + binding.wrappedValue.labels)).sorted(), id: \.self) { label in
                    LabelToggle(name: label, on: binding.wrappedValue.labels.contains(label)) {
                        binding.wrappedValue.toggleLabel(label)
                    }
                }
            }
            TextField("Add a label and press Return", text: $newLabel)
                .textFieldStyle(.plain).fieldBox().scaledWidth(220)
                .onSubmit {
                    binding.wrappedValue.addLabel(newLabel)
                    newLabel = ""
                }
        }
    }

    private var footer: some View {
        HStack {
            if let error {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.red).textSelection(.enabled).lineLimit(2)
            }
            Spacer()
            if saving { ProgressView().controlSize(.small) }
            Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
            Button("Save") { Task { await save() } }
                .keyboardShortcut(.defaultAction)
                .buttonStyle(.borderedProminent)
        }
        .padding(14)
        .background(.bar)
        .overlay(alignment: .top) { Divider() }
    }

    private func load() async {
        do {
            let loaded = try await model.call("details", ["task_id": taskId], as: Details.self)
            details = loaded
            form = DetailChanges(from: loaded.task)
            sizeText = Format.minutes(loaded.task.sizeMinutes).replacingOccurrences(of: " ", with: "")
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func save() async {
        guard let details, let form else { return }
        do {
            try await submit(try form.payload(sizeText: sizeText, original: details.task))
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func submit(_ diff: [String: Any]) async throws {
        guard !diff.isEmpty else { return }
        saving = true
        defer { saving = false }
        _ = try await model.call("update_details", ["task_id": taskId, "changes": diff], as: Details.self)
        await model.run("snapshot")
    }
}

/// A label in the details form: filled when the task has it, outlined when not.
private struct LabelToggle: View {
    let name: String
    let on: Bool
    let action: () -> Void

    private struct Style {
        let text: Color
        let fill: Color
        let edge: Color
    }

    private static let onStyle = Style(text: .white, fill: .accentColor, edge: .clear)
    private static let offStyle = Style(text: .primary, fill: .primary.opacity(0.06), edge: .primary.opacity(0.2))

    var body: some View {
        let style = on ? Self.onStyle : Self.offStyle
        Button(action: action) {
            Text("@\(name)")
                .padding(.horizontal, 8).padding(.vertical, 3)
                .foregroundStyle(style.text)
                .background(Capsule().fill(style.fill))
                .overlay(Capsule().strokeBorder(style.edge))
        }
        .buttonStyle(.plain)
    }
}

/// A titled group of fields on a raised card.
private struct FormCard<Content: View>: View {
    let title: String
    let icon: String
    var count: Int?
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 6) {
                Image(systemName: icon)
                Text(title.uppercased())
                if let count { Text("\(count)").foregroundStyle(.tertiary) }
            }
            .appFont(.caption, weight: .bold)
            .foregroundStyle(.secondary)
            content
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color(nsColor: .controlBackgroundColor)))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color.primary.opacity(0.1)))
        .shadow(color: .black.opacity(0.05), radius: 2, y: 1)
    }
}

/// A label column and a field column in a FormCard's grid.
private struct FieldRow<Content: View>: View {
    let title: String
    let icon: String
    let content: Content

    init(_ title: String, icon: String, @ViewBuilder content: () -> Content) {
        self.title = title
        self.icon = icon
        self.content = content()
    }

    var body: some View {
        GridRow(alignment: .firstTextBaseline) {
            Label(title, systemImage: icon)
                .appFont(.callout, weight: .medium)
                .foregroundStyle(.primary.opacity(0.8))
                .gridColumnAlignment(.leading)
            content
        }
    }
}

extension View {
    /// A text field's box: the text background with a clear edge, readable on a card.
    func fieldBox() -> some View {
        padding(.horizontal, 8).padding(.vertical, 6)
            .background(RoundedRectangle(cornerRadius: 6).fill(Color(nsColor: .textBackgroundColor)))
            .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Color.primary.opacity(0.22)))
    }
}

/// Four flags in Todoist's colors; the chosen one is filled.
private struct PriorityPicker: View {
    @Binding var priority: Int

    var body: some View {
        HStack(spacing: 6) {
            ForEach(1...4, id: \.self) { p in
                let on = p == priority
                let color = Theme.priority(p)
                Button { priority = p } label: {
                    Label("P\(p)", systemImage: on ? "flag.fill" : "flag")
                        .padding(.horizontal, 10).padding(.vertical, 4)
                        .foregroundStyle(on ? Color.white : color)
                        .background(Capsule().fill(on ? color : color.opacity(0.1)))
                        .overlay(Capsule().strokeBorder(color.opacity(on ? 0 : 0.5)))
                }
                .buttonStyle(.plain)
                .help("Priority \(p)")
            }
        }
        .animation(.snappy(duration: 0.15), value: priority)
    }
}

private struct DeadlinePicker: View {
    @Binding var deadline: String?

    var body: some View {
        HStack {
            Toggle("", isOn: Binding(
                get: { deadline != nil },
                set: { deadline = $0 ? Format.iso(Date()) : nil }
            ))
            .toggleStyle(.switch)
            .labelsHidden()
            if deadline != nil {
                DatePicker("", selection: Binding(
                    get: { Format.date(deadline) ?? Date() },
                    set: { deadline = Format.iso($0) }
                ), displayedComponents: .date)
                .labelsHidden()
            } else {
                Text("None").foregroundStyle(.secondary)
            }
        }
    }
}

private struct ProjectPicker: View {
    let details: Details
    @Binding var form: DetailChanges

    var body: some View {
        HStack {
            Picker("", selection: Binding(
                get: { form.projectId ?? "" },
                set: { form.projectId = $0; form.sectionId = nil }
            )) {
                ForEach(details.projects) { Text($0.name).tag($0.id) }
            }
            .labelsHidden().scaledWidth(190)
            Picker("", selection: Binding(get: { form.sectionId ?? "" }, set: { form.sectionId = $0.isEmpty ? nil : $0 })) {
                Text("No section").tag("")
                ForEach(details.sections.filter { $0.projectId == form.projectId }) { Text($0.name).tag($0.id) }
            }
            .labelsHidden().scaledWidth(170)
        }
    }
}

private struct CommentsView: View {
    @EnvironmentObject var model: AppModel
    let taskId: String
    let comments: [Comment]
    @Binding var newComment: String
    let updated: (Details) -> Void
    @State private var editingId: String?
    @State private var editText = ""
    @State private var error: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if comments.isEmpty {
                Text("No comments yet.").foregroundStyle(.secondary)
            }
            ForEach(comments) { comment in
                if editingId == comment.id {
                    VStack(alignment: .trailing, spacing: 6) {
                        TextEditor(text: $editText)
                            .scrollContentBackground(.hidden)
                            .frame(minHeight: 60).fieldBox()
                        HStack {
                            Button("Cancel") { editingId = nil }
                            Button("Save comment") { send("edit_comment", ["comment_id": comment.id, "content": editText]) }
                                .buttonStyle(.borderedProminent)
                        }
                    }
                } else {
                    CommentBubble(comment: comment) {
                        editingId = comment.id
                        editText = comment.content
                    }
                }
            }
            ZStack(alignment: .topLeading) {
                TextEditor(text: $newComment)
                    .scrollContentBackground(.hidden)
                    .frame(minHeight: 54)
                if newComment.isEmpty {
                    Text("Write a comment…").foregroundStyle(.tertiary).padding(.leading, 5).allowsHitTesting(false)
                }
            }
            .fieldBox()
            HStack {
                if let error { Text(error).foregroundStyle(.red).appFont(.caption) }
                Spacer()
                Button("Add comment") { send("add_comment", ["content": newComment]) }
                    .disabled(newComment.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
    }

    private func send(_ method: String, _ params: [String: Any]) {
        Task {
            do {
                var all = params
                all["task_id"] = taskId
                let details = try await model.call(method, all, as: Details.self)
                editingId = nil
                if method == "add_comment" { newComment = "" }
                updated(details)
                error = nil
            } catch {
                self.error = error.localizedDescription
            }
        }
    }
}
