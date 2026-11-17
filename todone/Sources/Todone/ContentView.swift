import AppKit
import SwiftUI
import TodoneCore

struct ContentView: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        VStack(spacing: 0) {
            HeaderBar()
            if model.snapshot?.planning == true { PlanningBanner() }
            MessageBanners()
            Divider()
            if let snapshot = model.shown {
                Board(snapshot: snapshot)
            } else {
                ProgressView("Loading tasks…").frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .background(model.snapshot?.planning == true ? Theme.planningTint : Color(nsColor: .windowBackgroundColor))
        .overlay {
            if model.snapshot?.planning == true {
                RoundedRectangle(cornerRadius: 6).strokeBorder(Theme.planning, lineWidth: 3).allowsHitTesting(false)
            }
        }
        .onAppear { model.installKeyMonitor() }
        .onChange(of: model.showPlanner) { _, planner in
            resizeWindow(planner: planner)
        }
        .sheet(item: Binding(
            get: { model.detailsTaskId.map(DetailsTarget.init) },
            set: { model.detailsTaskId = $0?.id }
        )) { target in
            DetailsView(taskId: target.id).environmentObject(model).textSize(model.textSize)
        }
        .onChange(of: model.textSize) { _, _ in
            resizeWindow(planner: model.showPlanner)
        }
    }
}

private struct DetailsTarget: Identifiable { let id: String }

extension ContentView {
    /// Narrow for the today view alone, wide enough for the whole planner when it opens.
    func resizeWindow(planner: Bool) {
        guard let window = NSApp.keyWindow, let screen = window.screen else { return }
        let width = WindowLayout.width(planner: planner, dayColumns: model.snapshot?.columns.count ?? 5,
                                       scale: model.textSize.scale)
        let frame = WindowLayout.frame(current: window.frame, screen: screen.visibleFrame, width: width)
        window.setFrame(frame, display: true, animate: true)
    }
}

struct Board: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.textScale) private var scale
    let snapshot: Snapshot

    var body: some View {
        if model.showPlanner {
            // Everything side by side, scrolling sideways when it's wider than the window.
            ScrollViewReader { proxy in
                ScrollView(.horizontal) {
                    HStack(alignment: .top, spacing: 0) {
                        TaskPanel(title: "Inbox", note: "Inbox tasks outside its sections. Drag onto a day.",
                                  tasks: snapshot.inbox)
                            .padding(10)
                            .id(BoardColumn.inbox)
                        Divider()
                        TodayColumnView(snapshot: snapshot)
                            .frame(width: WindowLayout.todayMaxWidth * scale)
                            .id(BoardColumn.today)
                        Divider()
                        HStack(alignment: .top, spacing: 10) {
                            ForEach(snapshot.columns) { DayColumnView(column: $0).id(BoardColumn.day($0.date)) }
                            TaskPanel(title: "Unplanned", note: "No date, a weekend, or deadline only. Drag onto a day.",
                                      tasks: snapshot.tray, dashed: true)
                                .id(BoardColumn.tray)
                        }
                        .padding(10)
                    }
                }
                .onChange(of: model.selectedId) { _, id in
                    guard let id, let column = BoardColumn.containing(id, in: snapshot) else { return }
                    withAnimation(.snappy) { proxy.scrollTo(column) }
                }
            }
        } else {
            TodayColumnView(snapshot: snapshot)
        }
    }
}

struct HeaderBar: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        HStack(spacing: 14) {
            if let today = model.snapshot?.todayColumn {
                Stat(label: "left today", value: Format.minutes(today.capacityMinutes))
                Stat(label: "scheduled", value: Format.minutes(today.plannedMinutes),
                     warn: (today.capacityMinutes ?? .max) < today.plannedMinutes)
            }
            TimersStrip()
            Spacer()
            LogButton()
            if model.fakeMode { Text("SAMPLE DATA").appFont(.caption, weight: .bold).foregroundStyle(.orange) }
            if model.busy { ProgressView().controlSize(.small) }
            PlanControls()
            Button { Task { await model.refresh(forceCalendar: true) } } label: {
                Image(systemName: "arrow.clockwise")
            }
            .help("Refresh tasks and calendar (⌘R)")
            Toggle(isOn: $model.showPlanner) {
                Label("Planner", systemImage: "calendar.day.timeline.left")
            }
            .toggleStyle(.button)
            .help("Show or hide the planner (⌘P)")
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
    }
}

struct Stat: View {
    let label: String
    let value: String
    var warn = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(value).appFont(.title2, weight: .bold).monospacedDigit().foregroundStyle(warn ? .red : .primary)
            Text(label).appFont(.caption).foregroundStyle(.secondary)
        }
    }
}

struct PlanControls: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        if model.snapshot?.planning == true {
            Button("Cancel") { Task { await model.cancelPlan() } }
            Button("Confirm Plan") { Task { await model.confirmPlan() } }
                .buttonStyle(.borderedProminent)
                .tint(Theme.planning)
        } else if model.showPlanner {
            Button { Task { await model.plan() } } label: { Label("Plan", systemImage: "wand.and.stars") }
                .disabled(model.snapshot?.calendarError != nil)
                .help("Propose a plan for the coming days, like `todoist plan`")
        }
    }
}

struct PlanningBanner: View {
    var body: some View {
        HStack {
            Image(systemName: "pencil.and.ruler")
            Text("Planning — drag tasks to adjust, then Confirm Plan to write the changes.")
            Spacer()
        }
        .appFont(.callout, weight: .bold)
        .foregroundStyle(.white)
        .padding(.horizontal, 14)
        .padding(.vertical, 6)
        .background(Theme.planning)
    }
}

struct MessageBanners: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        VStack(spacing: 0) {
            if let error = model.errorMessage {
                Banner(text: error, color: .red) { model.errorMessage = nil }
            }
            if let calendar = model.snapshot?.calendarError {
                Banner(text: Format.calendarBanner(error: calendar, reauth: model.snapshot?.calendarReauth),
                       color: .orange, dismiss: nil)
            }
            if let notice = model.notice {
                Banner(text: notice, color: .green) { model.notice = nil }
            }
        }
    }
}

struct Banner: View {
    let text: String
    let color: Color
    let dismiss: (() -> Void)?

    var body: some View {
        HStack(alignment: .top) {
            Text(text).textSelection(.enabled)
            Spacer()
            if let dismiss { Button(action: dismiss) { Image(systemName: "xmark") }.buttonStyle(.plain) }
        }
        .appFont(.callout)
        .padding(.horizontal, 14)
        .padding(.vertical, 6)
        .background(color.opacity(0.15))
    }
}

enum Theme {
    static let planning = Color.purple
    static let planningTint = Color.purple.opacity(0.06)

    private static let priorityColors: [Color] = [.red, .orange, .blue]

    /// P1 red, P2 orange, P3 blue, anything else gray (Todoist's own colors).
    static func priority(_ p: Int) -> Color {
        priorityColors.indices.contains(p - 1) ? priorityColors[p - 1] : .gray
    }
}
