import SwiftUI
import TodoneCore

enum RowLayout { case wide, compact }

struct TaskRow: View {
    @EnvironmentObject var model: AppModel
    let task: TaskItem
    let layout: RowLayout

    private var expanded: Bool { model.expanded.contains(task.id) }
    private var selected: Bool { model.selectedId == task.id }
    private var completing: Bool { model.pending.completing.contains(task.id) }

    var body: some View {
        let card = VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .top, spacing: 8) {
                RoundedRectangle(cornerRadius: 2).fill(Theme.priority(task.priority)).frame(width: 4)
                DoneButton(id: task.id, priority: task.priority, running: task.running)
                VStack(alignment: .leading, spacing: 4) {
                    TitleLine(task: task, expanded: expanded)
                    MetaLine(task: task)
                    if layout == .compact { RowButtons(task: task) }
                }
                Spacer(minLength: 0)
                if layout == .wide { RowButtons(task: task) }
                Chevron(expanded: expanded)
            }
            .contentShape(Rectangle())
            .onTapGesture { select(toggle: true) }
            if expanded {
                TaskExpansion(task: task)
                    .padding(.leading, 12)
                    .transition(.opacity.combined(with: .move(edge: .top)))
            }
        }
        .padding(8)
        .background(RoundedRectangle(cornerRadius: 6).fill(background))
        .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(border, lineWidth: borderWidth))
        .shadow(color: .black.opacity(selected ? 0.12 : 0), radius: 4, y: 1)
        .opacity(completing ? 0.5 : 1)
        .scaleEffect(completing ? 0.98 : 1)
        .id(task.id)
        .contextMenu {
            Button(expanded ? "Fold" : "Unfold") { model.toggleExpanded(task.id) }
            Button("Details…") { model.detailsTaskId = task.id }
            if let url = URL(string: task.url) { Link("Open in Todoist", destination: url) }
        }
        if task.movable && !expanded {
            card.draggable(task.id) { DragPreview(task: task) }
        } else {
            card
        }
    }

    private func select(toggle: Bool) {
        model.selectedId = task.id
        if toggle { model.toggleExpanded(task.id) }
    }

    private var background: Color {
        task.priority == 1 ? Color.red.opacity(0.07) : Color(nsColor: .textBackgroundColor)
    }

    private var border: Color {
        if selected { return .accentColor }
        if task.running { return .green }
        if task.moved { return Theme.planning }
        return .secondary.opacity(0.25)
    }

    private var borderWidth: CGFloat { selected || task.running ? 2 : 1 }
}

/// Points right when folded, down when unfolded.
private struct Chevron: View {
    let expanded: Bool

    var body: some View {
        Image(systemName: "chevron.right")
            .rotationEffect(.degrees(expanded ? 90 : 0))
            .foregroundStyle(.tertiary)
            .appFont(.caption, weight: .semibold)
            .padding(.top, 3)
            .help(expanded ? "Fold (Return)" : "Unfold: description, subtasks, comments (Return)")
    }
}

private struct TitleLine: View {
    @EnvironmentObject var model: AppModel
    let task: TaskItem
    let expanded: Bool

    var body: some View {
        if model.editingTitleId == task.id {
            TitleEditor(id: task.id, content: task.content, priority: task.priority, labels: task.labels)
        } else {
            HStack(alignment: .firstTextBaseline, spacing: 4) {
                StatusIcons(task: task)
                Text(task.content)
                    .appFont(.body, weight: task.priority <= 2 ? .semibold : .regular)
                    .strikethrough(model.pending.completing.contains(task.id))
                    .foregroundStyle(model.pending.completing.contains(task.id) ? .secondary : .primary)
                    .lineLimit(expanded ? nil : 3)
                    .onTapGesture {
                        // Unfolded, the title is for editing; folded, it unfolds like the rest of the card.
                        model.selectedId = task.id
                        if expanded { model.editingTitleId = task.id } else { model.toggleExpanded(task.id) }
                    }
                    .help(expanded ? "Click to edit the title" : "")
                if model.pending.syncing.contains(task.id) { SyncSpinner() }
            }
        }
    }
}

/// Shown on a card while its change is on the way to Todoist.
struct SyncSpinner: View {
    var body: some View {
        ProgressView().controlSize(.mini).help("Saving to Todoist…")
    }
}

/// Overdue, at risk, pinned, recurring and can't-move markers.
private struct StatusIcons: View {
    let task: TaskItem

    var body: some View {
        if task.overdue { icon("clock.badge.exclamationmark", .red, "Overdue") }
        if task.atRisk { icon("exclamationmark.triangle.fill", .orange, "At risk: the planner can't fit it before its date") }
        if task.pinned {
            if task.dueTime != nil {
                icon("clock.fill", .secondary, "Pinned: has a time of day")
            } else {
                icon("pin.fill", .secondary, "Pinned: @fixed")
            }
        }
        if task.recurring { icon("repeat", .secondary, "Recurring") }
        if !task.movable && !task.pinned {
            icon("lock.fill", .secondary, "Can't be moved: this recurrence would shift the whole series")
        }
    }

    private func icon(_ name: String, _ color: Color, _ help: String) -> some View {
        Image(systemName: name).foregroundStyle(color).appFont(.caption).help(help)
    }
}

private struct MetaLine: View {
    let task: TaskItem

    var body: some View {
        FlowRow {
            Text("P\(task.priority)").foregroundStyle(Theme.priority(task.priority)).bold()
            Text(task.sizeLabel).foregroundStyle(.secondary)
            if task.descendantCount > 0 {
                Label("\(task.descendantCount)", systemImage: "list.bullet.indent")
                    .foregroundStyle(Color.accentColor)
                    .padding(.horizontal, 5).padding(.vertical, 1)
                    .background(Capsule().fill(Color.accentColor.opacity(0.12)))
                    .help("\(task.descendantCount) open subtask\(task.descendantCount == 1 ? "" : "s"); click to unfold")
            }
            if task.commentCount > 0 {
                Label("\(task.commentCount)", systemImage: "text.bubble").foregroundStyle(.secondary)
                    .help("\(task.commentCount) comment\(task.commentCount == 1 ? "" : "s")")
            }
            if !task.description.isEmpty {
                Image(systemName: "text.alignleft").foregroundStyle(.secondary).help("Has a description")
            }
            if let deadline = task.deadline {
                Label(Format.shortDate(deadline), systemImage: "flag.checkered")
                    .foregroundStyle(task.atRisk ? .orange : .secondary)
            }
            if let time = task.dueTime {
                Label(time, systemImage: "clock").foregroundStyle(.secondary)
            }
            if let until = task.spansUntil {
                Label("through \(Format.dayLabel(until))", systemImage: "arrow.right.to.line")
                    .foregroundStyle(.secondary)
                    .help("Too big for one day: the rest is counted on the following days")
            }
            if task.moved {
                Text("\(Format.shortDate(task.originalDueDate)) → \(Format.shortDate(task.dueDate))")
                    .foregroundStyle(Theme.planning)
                if !task.movable { Text("plan only").foregroundStyle(.secondary).italic() }
            }
            ForEach(task.labels, id: \.self) { LabelChip(name: $0) }
        }
        .appFont(.caption)
    }
}

struct LabelChip: View {
    let name: String

    var body: some View {
        Text("@\(name)")
            .padding(.horizontal, 5).padding(.vertical, 1)
            .background(Capsule().fill(.secondary.opacity(0.15)))
    }
}

private struct RowButtons: View {
    @EnvironmentObject var model: AppModel
    let task: TaskItem
    @State private var pickingColor = false

    var body: some View {
        HStack(spacing: 6) {
            Button {
                if task.running { pickingColor = true } else {
                    Task { await model.startTimer(task.id) }
                }
            } label: {
                Image(systemName: task.running ? "stop.circle.fill" : "play.circle")
                    .foregroundStyle(task.running ? .red : .green)
            }
            .help(task.running ? "Stop and log to the calendar" : "Start a timer")
            Button { Task { await model.toggleFixed(task.id) } } label: {
                Image(systemName: task.fixed ? "pin.fill" : "pin")
                    .contentTransition(.symbolEffect(.replace))
            }
            .help(task.fixed ? "Remove @fixed" : "Add @fixed so the planner leaves it on its day")
            Button { model.detailsTaskId = task.id } label: { Image(systemName: "info.circle") }
                .help("Details")
            if let url = URL(string: task.url) {
                Link(destination: url) { Image(systemName: "link") }
                    .help("Open in Todoist")
            }
        }
        .buttonStyle(.borderless)
        .appFont(.title3)
        .popover(isPresented: $pickingColor) {
            ColorPicker(title: "Log to calendar") { color in
                pickingColor = false
                Task { await model.stopTimer(task.id, color: color, thenComplete: false) }
            }
        }
    }
}

/// Complete, at the card's left edge, away from the other buttons so it isn't hit by mistake.
/// Fills with a check and bounces as soon as it's clicked; the card then fades out
/// while Todoist is told.
struct DoneButton: View {
    @EnvironmentObject var model: AppModel
    let id: String
    let priority: Int
    var running = false
    @State private var pickingColor = false
    @State private var hovering = false

    private var done: Bool { model.pending.completing.contains(id) }

    var body: some View {
        Button {
            if running { pickingColor = true } else {
                Task { await model.complete(id) }
            }
        } label: {
            Image(systemName: done ? "checkmark.circle.fill" : (hovering ? "checkmark.circle" : "circle"))
                .foregroundStyle(done ? Color.green : Theme.priority(priority))
                .contentTransition(.symbolEffect(.replace))
                .symbolEffect(.bounce, value: done)
                .scaleEffect(done ? 1.15 : 1)
        }
        .buttonStyle(.borderless)
        .appFont(.title3)
        .disabled(done)
        .onHover { hovering = $0 }
        .help(running ? "Stop the timer, log it, and complete" : "Complete")
        .popover(isPresented: $pickingColor) {
            ColorPicker(title: "Log and complete") { color in
                pickingColor = false
                Task { await model.stopTimer(id, color: color, thenComplete: true) }
            }
        }
    }
}

/// Event color chooser shown when a timer stops.
struct ColorPicker: View {
    @EnvironmentObject var model: AppModel
    let title: String
    let choose: (Int?) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).appFont(.headline).padding(.bottom, 4)
            ForEach(model.colors) { color in
                Button { choose(color.colorId) } label: {
                    HStack {
                        Circle().fill(Color(hex: color.hex) ?? .secondary.opacity(0.3)).frame(width: 14, height: 14)
                        Text(color.name)
                        Spacer()
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
        .padding()
        .scaledWidth(230)
        .appFont(.body)
    }
}

struct TimersStrip: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        if let timers = model.snapshot?.timers, !timers.isEmpty {
            TimelineView(.periodic(from: .now, by: 1)) { context in
                HStack(spacing: 8) {
                    ForEach(timers) { TimerChip(timer: $0, now: context.date) }
                }
            }
        }
    }
}

/// A running timer in the header: elapsed time and a stop button that logs it.
struct TimerChip: View {
    @EnvironmentObject var model: AppModel
    let timer: RunningTimer
    let now: Date
    @State private var pickingColor = false

    var body: some View {
        HStack(spacing: 4) {
            Image(systemName: timer.isLog ? "stopwatch" : "record.circle").foregroundStyle(.red)
            Text(timer.title).lineLimit(1).frame(maxWidth: 160)
            Text(Format.elapsed(since: timer.startedAt, now: now)).monospacedDigit()
            Button { pickingColor = true } label: { Image(systemName: "stop.fill").foregroundStyle(.red) }
                .buttonStyle(.borderless)
                .help("Stop and log to the calendar")
        }
        .appFont(.caption)
        .padding(.horizontal, 6).padding(.vertical, 3)
        .background(Capsule().fill(.green.opacity(0.15)))
        .contextMenu {
            Button("Discard without logging") { Task { await model.discardTimer(timer.taskId) } }
        }
        .popover(isPresented: $pickingColor) {
            ColorPicker(title: "Log to calendar") { color in
                pickingColor = false
                Task { await model.stopTimer(timer.taskId, color: color, thenComplete: false) }
            }
        }
    }
}

/// Start timing work that has no task, like `todoist log`.
struct LogButton: View {
    @EnvironmentObject var model: AppModel
    @State private var open = false
    @State private var title = ""
    @State private var notes = ""

    var body: some View {
        Button { open = true } label: { Label("Log", systemImage: "stopwatch") }
            .help("Time something that isn't a task, then log it to the calendar")
            .popover(isPresented: $open) {
                VStack(alignment: .leading, spacing: 8) {
                    Text("What are you working on?").appFont(.headline)
                    TextField("e.g. Acme call", text: $title).scaledWidth(260).onSubmit(start)
                    TextField("Notes for the calendar event (optional)", text: $notes).scaledWidth(260)
                    HStack {
                        Spacer()
                        Button("Start", action: start).keyboardShortcut(.defaultAction)
                            .disabled(title.trimmingCharacters(in: .whitespaces).isEmpty)
                    }
                }
                .padding()
                .appFont(.body)
            }
    }

    private func start() {
        open = false
        let (what, extra) = (title, notes)
        title = ""
        notes = ""
        Task { await model.startLog(what, extra) }
    }
}

private struct DragPreview: View {
    let task: TaskItem

    var body: some View {
        Text(task.content).padding(6).background(RoundedRectangle(cornerRadius: 6).fill(.background))
    }
}

/// Lays children out left to right, wrapping onto new lines.
struct FlowRow: Layout {
    var spacing: CGFloat = 6

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        arrange(width: proposal.width ?? .infinity, subviews: subviews).size
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let layout = arrange(width: bounds.width, subviews: subviews)
        for (subview, origin) in zip(subviews, layout.origins) {
            subview.place(at: CGPoint(x: bounds.minX + origin.x, y: bounds.minY + origin.y), proposal: .unspecified)
        }
    }

    private func arrange(width: CGFloat, subviews: Subviews) -> (size: CGSize, origins: [CGPoint]) {
        Flow.arrange(sizes: subviews.map { $0.sizeThatFits(.unspecified) }, width: width, spacing: spacing)
    }
}

extension Color {
    init?(hex: String?) {
        guard let hex, hex.hasPrefix("#"), let value = UInt32(hex.dropFirst(), radix: 16) else { return nil }
        self.init(red: Double((value >> 16) & 0xFF) / 255,
                  green: Double((value >> 8) & 0xFF) / 255,
                  blue: Double(value & 0xFF) / 255)
    }
}
