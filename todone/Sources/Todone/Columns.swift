import SwiftUI
import TodoneCore

struct TodayColumnView: View {
    @EnvironmentObject var model: AppModel
    let snapshot: Snapshot

    var body: some View {
        SelectionScrollView {
            VStack(alignment: .leading, spacing: 8) {
                CapacityHeader(column: snapshot.todayColumn, title: "Today")
                if !snapshot.overdue.isEmpty {
                    SectionTitle(text: "Overdue", count: snapshot.overdue.count, color: .red)
                    TaskList(tasks: snapshot.overdue, layout: .wide)
                }
                SectionTitle(text: "Today", count: snapshot.todayTasks.count, color: .primary)
                if snapshot.todayTasks.isEmpty {
                    Text("Nothing due today.").foregroundStyle(.secondary).padding(.vertical, 6)
                }
                TaskList(tasks: snapshot.todayTasks, layout: .wide)
            }
            .padding(12)
        }
        .dropTarget(day: snapshot.today)
    }
}

struct DayColumnView: View {
    let column: Column

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            CapacityHeader(column: column, title: Format.dayLabel(column.date))
            SelectionScrollView {
                VStack(alignment: .leading, spacing: 8) {
                    TaskList(tasks: column.tasks ?? [], layout: .compact)
                    Color.clear.frame(height: 40)  // somewhere to drop below the last card
                }
            }
        }
        .scaledWidth(WindowLayout.panelWidth)
        .padding(8)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color(nsColor: .controlBackgroundColor)))
        .dropTarget(day: column.date)
    }
}

/// A side list of tasks (the Inbox, the Unplanned tray). Not a drop target.
struct TaskPanel: View {
    let title: String
    let note: String
    let tasks: [TaskItem]
    var dashed = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionTitle(text: title, count: tasks.count, color: .secondary)
            Text(note).appFont(.caption).foregroundStyle(.secondary)
            SelectionScrollView {
                VStack(alignment: .leading, spacing: 8) {
                    TaskList(tasks: tasks, layout: .compact)
                }
            }
        }
        .scaledWidth(WindowLayout.panelWidth)
        .padding(8)
        .background(RoundedRectangle(cornerRadius: 8).strokeBorder(
            .secondary.opacity(0.4), style: StrokeStyle(lineWidth: 1, dash: dashed ? [4] : [])))
    }
}

/// Cards that fade and slide away when completed, and fade in when they arrive.
struct TaskList: View {
    let tasks: [TaskItem]
    let layout: RowLayout

    var body: some View {
        ForEach(tasks) { task in
            TaskRow(task: task, layout: layout)
                .transition(.asymmetric(insertion: .opacity,
                                        removal: .opacity.combined(with: .move(edge: .leading))))
        }
    }
}

/// A vertical scroll view that brings the keyboard-selected card into view.
struct SelectionScrollView<Content: View>: View {
    @EnvironmentObject var model: AppModel
    @ViewBuilder let content: Content

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView { content }
                .onChange(of: model.selectedId) { _, id in
                    guard let id else { return }
                    withAnimation(.snappy) { proxy.scrollTo(id) }
                }
        }
    }
}

struct SectionTitle: View {
    let text: String
    let count: Int
    let color: Color

    var body: some View {
        HStack {
            Text(text.uppercased()).appFont(.caption, weight: .bold).foregroundStyle(color)
            Text("\(count)").appFont(.caption).foregroundStyle(.secondary)
        }
        .padding(.top, 4)
    }
}

/// Free and planned time for a day. Clicking it edits that day's capacity.
struct CapacityHeader: View {
    @EnvironmentObject var model: AppModel
    let column: Column
    let title: String
    @State private var editing = false
    @State private var entry = ""

    private var over: Bool { (column.capacityMinutes ?? .max) < column.plannedMinutes }

    var body: some View {
        Button { entry = ""; editing = true } label: {
            VStack(alignment: .leading, spacing: 3) {
                HStack {
                    Text(title).appFont(.headline)
                    if column.capacityOverridden {
                        Image(systemName: "slider.horizontal.3").foregroundStyle(.secondary).help("Capacity edited")
                    }
                    Spacer()
                }
                HStack(spacing: 4) {
                    Text("\(Format.minutes(column.capacityMinutes)) free")
                    Text("·")
                    Text("\(Format.minutes(column.plannedMinutes)) planned").foregroundStyle(over ? .red : .secondary)
                }
                .appFont(.caption).monospacedDigit()
                LoadBar(free: column.capacityMinutes, planned: column.plannedMinutes)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help("Click to change this day's available time")
        .popover(isPresented: $editing) {
            VStack(alignment: .leading, spacing: 8) {
                Text("Available time on \(Format.dayLabel(column.date))").appFont(.headline)
                TextField("e.g. 1h30m, 0", text: $entry).scaledWidth(180).onSubmit(save)
                HStack {
                    Button("Reset to calendar") { submit("") }
                    Spacer()
                    Button("Set", action: save).keyboardShortcut(.defaultAction)
                }
            }
            .padding()
            .appFont(.body)
        }
    }

    private func save() { submit(entry) }

    private func submit(_ value: String) {
        editing = false
        Task { await model.setCapacity(column.date, value) }
    }
}

struct LoadBar: View {
    let free: Int?
    let planned: Int

    var body: some View {
        GeometryReader { geo in
            let total = max(free ?? 0, planned, 1)
            let fraction = CGFloat(planned) / CGFloat(total)
            ZStack(alignment: .leading) {
                Capsule().fill(.secondary.opacity(0.2))
                Capsule().fill(planned > (free ?? .max) ? Color.red : Color.accentColor)
                    .frame(width: geo.size.width * min(fraction, 1))
            }
        }
        .frame(height: 4)
    }
}

extension View {
    /// Accept a dragged task id and move it to `day`.
    func dropTarget(day: String) -> some View {
        modifier(DropTarget(day: day))
    }
}

private struct DropTarget: ViewModifier {
    @EnvironmentObject var model: AppModel
    let day: String
    @State private var hovering = false

    func body(content: Content) -> some View {
        content
            .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Color.accentColor, lineWidth: hovering ? 2 : 0))
            .dropDestination(for: String.self) { ids, _ in
                guard let id = ids.first else { return false }
                Task { await model.move(id, to: day) }
                return true
            } isTargeted: { hovering = $0 }
    }
}
