import AppKit
import Foundation
import SwiftUI
import TodoneCore

/// Owns the helper process and the latest snapshot; every view action goes through here.
@MainActor
final class AppModel: ObservableObject {
    @Published var snapshot: Snapshot?
    @Published var errorMessage: String?
    @Published var notice: String?
    @Published var busy = false
    @Published var showPlanner = false
    @Published var colors: [EventColor] = []
    @Published var detailsTaskId: String?
    /// Changes shown before Todoist confirms them; see `shown`.
    @Published var pending = PendingEdits()
    /// The card picked with j/k (or a click).
    @Published var selectedId: String?
    /// Unfolded cards: description, subtasks and comments showing.
    @Published var expanded: Set<String> = []
    /// The task (or subtask) whose title is being edited in place.
    @Published var editingTitleId: String?
    /// Comments fetched when a card unfolds.
    @Published var comments: [String: [Comment]] = [:]
    /// Chosen from the View menu; remembered between launches.
    @Published var textSize = TextSize(stored: UserDefaults.standard.string(forKey: "textSize")) {
        didSet { UserDefaults.standard.set(textSize.rawValue, forKey: "textSize") }
    }

    let fakeMode: Bool
    private var helper: Helper?
    private var refreshTimer: Timer?
    private var keyMonitor: Any?

    init() {
        let settings = HelperSettings.current()
        fakeMode = settings.fake
        showPlanner = ProcessInfo.processInfo.arguments.contains("--planner")
        do {
            helper = try Helper(python: settings.python, module: settings.module,
                                arguments: settings.fake ? ["--fake"] : [], environment: settings.environment)
        } catch {
            errorMessage = "Couldn't start the helper with \(settings.python.path): \(error.localizedDescription)"
        }
        Task { await start() }
        // Capacity counts down through the day and the calendar can change, so
        // reload every 15 minutes (the helper refetches the calendar when stale).
        refreshTimer = Timer.scheduledTimer(withTimeInterval: 15 * 60, repeats: true) { [weak self] _ in
            Task { @MainActor in await self?.refresh() }
        }
    }

    func shutdown() { helper?.stop() }

    /// The snapshot with pending changes applied: what every list shows.
    var shown: Snapshot? { snapshot.map(pending.apply) }

    private func start() async {
        await refresh()
        if let helper, let loaded = try? await helper.call("colors", as: [EventColor].self) {
            colors = loaded
        }
    }

    // MARK: - Calls

    /// Run a helper method that returns a snapshot, surfacing any error.
    func run(_ method: String, _ params: [String: Any] = [:]) async {
        busy = true
        defer { busy = false }
        do {
            apply(try await requireHelper().call(method, params, as: Snapshot.self))
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func apply(_ snap: Snapshot) {
        snapshot = snap
        errorMessage = nil
        notice = snap.result.map(Self.describe) ?? notice
    }

    private func requireHelper() throws -> Helper {
        guard let helper else { throw HelperError.exited(-1) }
        return helper
    }

    func call<T: Decodable & Sendable>(_ method: String, _ params: [String: Any], as type: T.Type) async throws -> T {
        try await requireHelper().call(method, params, as: type)
    }

    func refresh(forceCalendar: Bool = false) async {
        await run("refresh", ["force_calendar": forceCalendar])
    }

    func move(_ taskId: String, to day: String) async {
        pending.syncing.insert(taskId)
        await run("move", ["task_id": taskId, "day": day])
        pending.syncing.remove(taskId)
    }

    // MARK: - Optimistic actions

    private func result(_ method: String, _ params: [String: Any]) async -> Result<Snapshot, Error> {
        do {
            return .success(try await requireHelper().call(method, params, as: Snapshot.self))
        } catch {
            return .failure(error)
        }
    }

    /// Check the box at once, fade the card out, then remove it; if Todoist
    /// refuses, the card comes back and the error shows.
    func complete(_ taskId: String) async {
        guard !pending.completing.contains(taskId) else { return }
        let title = taskTitle(taskId)
        withAnimation(.spring(response: 0.3, dampingFraction: 0.55)) { _ = pending.completing.insert(taskId) }
        let work = Task { await result("complete", ["task_id": taskId]) }
        try? await Task.sleep(for: .milliseconds(650))
        withAnimation(.easeInOut(duration: 0.3)) {
            selectedId = Navigation.afterRemoving(taskId, from: visibleOrder, selected: selectedId)
            _ = pending.hidden.insert(taskId)
        }
        let outcome = await work.value
        withAnimation(.easeInOut(duration: 0.3)) {
            finish(outcome, failure: "Couldn't complete “\(title)”")
            pending.settle(taskId)
            expanded.remove(taskId)
        }
    }

    /// Show `edit` on the card now, write `method` in the background, and drop
    /// the edit once Todoist answers (its snapshot then shows the real state).
    func optimistic(_ taskId: String, _ edit: TaskEdit, _ method: String, _ params: [String: Any]) async {
        let title = taskTitle(taskId)
        withAnimation(.snappy) {
            pending.edits[taskId] = edit
            pending.syncing.insert(taskId)
        }
        let outcome = await result(method, params)
        withAnimation(.snappy) {
            finish(outcome, failure: "Couldn't update “\(title)”")
            pending.edits[taskId] = nil
            pending.syncing.remove(taskId)
        }
    }

    private func finish(_ outcome: Result<Snapshot, Error>, failure: String) {
        switch outcome {
        case .success(let snap): apply(snap)
        case .failure(let error): errorMessage = "\(failure): \(error.localizedDescription)"
        }
    }

    func setPriority(_ taskId: String, _ priority: Int) async {
        await optimistic(taskId, TaskEdit(priority: priority), "quick_update",
                         ["task_id": taskId, "changes": ["priority": priority]])
    }

    func toggleFixed(_ taskId: String) async {
        guard let task = findTask(taskId) else { return }
        let labels = task.fixed ? task.labels.filter { $0.lowercased() != "fixed" } : task.labels + ["fixed"]
        await optimistic(taskId, TaskEdit(labels: labels), "toggle_fixed", ["task_id": taskId])
    }

    /// Write a title edited in place, with its quick-add tokens.
    func saveTitle(_ taskId: String, _ parsed: QuickAdd, changes: [String: Any], labels current: [String]) async {
        editingTitleId = nil
        guard !changes.isEmpty else { return }
        let edit = TaskEdit(content: parsed.content, priority: parsed.priority, labels: parsed.mergedLabels(current))
        await optimistic(taskId, edit, "quick_update", ["task_id": taskId, "changes": changes])
    }

    func findTask(_ taskId: String) -> TaskItem? {
        shown?.allTasks.first { $0.id == taskId }
    }

    private func taskTitle(_ taskId: String) -> String {
        shown?.title(of: taskId) ?? "task"
    }

    // MARK: - Unfolding

    private var board: BoardState {
        get { BoardState(selected: selectedId, expanded: expanded, editing: editingTitleId) }
        set {
            selectedId = newValue.selected
            expanded = newValue.expanded
            editingTitleId = newValue.editing
        }
    }

    private var visibleOrder: [String] { shown.map { Navigation.order($0, planner: showPlanner) } ?? [] }

    func toggleExpanded(_ taskId: String) {
        var state = board
        let unfolded = state.toggle(taskId)
        withAnimation(.snappy(duration: 0.25)) { board = state }
        if unfolded { Task { await loadComments(taskId) } }
    }

    func loadComments(_ taskId: String) async {
        if let details = try? await call("details", ["task_id": taskId], as: Details.self) {
            comments[taskId] = details.comments
        }
    }

    func addComment(_ taskId: String, _ text: String) async {
        do {
            let details = try await call("add_comment", ["task_id": taskId, "content": text], as: Details.self)
            withAnimation(.snappy) { comments[taskId] = details.comments }
            await run("snapshot")
        } catch {
            errorMessage = "Couldn't add the comment: \(error.localizedDescription)"
        }
    }

    // MARK: - Keyboard

    /// j/k (or ↓/↑) move the selection, Return unfolds, e edits the title,
    /// 1–4 set the priority, Esc backs out. Ignored while typing in a field, in
    /// a sheet or popover, and for ⌘/⌃/⌥ combinations.
    func installKeyMonitor() {
        guard keyMonitor == nil else { return }
        // The model lives as long as the app, so the monitor can hold it.
        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { event in
            MainActor.assumeIsolated { self.handleKey(event) } ? nil : event
        }
    }

    private func handleKey(_ event: NSEvent) -> Bool {
        guard let window = event.window, window === NSApp.mainWindow, window.attachedSheet == nil,
              !(window.firstResponder is NSText)
        else { return false }
        let modified = !event.modifierFlags.intersection([.command, .control, .option]).isEmpty
        guard let command = KeyCommand.from(keyCode: event.keyCode, characters: event.charactersIgnoringModifiers,
                                            modified: modified)
        else { return false }
        return perform(command)
    }

    func perform(_ command: KeyCommand) -> Bool {
        var state = board
        let outcome = state.handle(command, order: visibleOrder)
        withAnimation(.snappy(duration: 0.2)) { board = state }
        if let id = outcome.unfolded { Task { await loadComments(id) } }
        if let change = outcome.priority { Task { await setPriority(change.id, change.priority) } }
        return outcome.handled
    }

    // MARK: - Timers, capacity, planning

    func startTimer(_ taskId: String) async { await run("start", ["task_id": taskId]) }
    func discardTimer(_ taskId: String) async { await run("discard_timer", ["task_id": taskId]) }

    func startLog(_ title: String, _ description: String) async {
        await run("start_log", ["title": title, "description": description])
    }

    func stopTimer(_ taskId: String, color: Int?, thenComplete: Bool) async {
        await run("stop", ["task_id": taskId, "color": color ?? NSNull()])
        if thenComplete, errorMessage == nil { await complete(taskId) }
    }

    func setCapacity(_ day: String, _ value: String) async {
        await run("set_capacity", ["day": day, "minutes": value])
    }

    func plan() async { await run("propose_plan") }
    func cancelPlan() async { await run("cancel_plan") }
    func confirmPlan() async { await run("confirm_plan") }


    func togglePlanner() {
        showPlanner.toggle()
    }

    static func describe(_ result: ApplyResult) -> String {
        var text = "Plan applied: \(result.updated) date(s) changed, \(result.labelsChanged) label change(s)."
        if result.failed > 0 { text += " \(result.failed) update(s) failed; see the helper log." }
        return text
    }
}

/// Where the helper lives and how to run it. The build script records the repo
/// path in Info.plist; TODONE_REPO overrides it (handy under `swift run`).
struct HelperSettings {
    var python: URL
    var module: String
    var fake: Bool
    var environment: [String: String]

    static func current(processInfo: ProcessInfo = .processInfo, bundle: Bundle = .main) -> HelperSettings {
        let env = processInfo.environment
        let repoPath = env["TODONE_REPO"]
            ?? bundle.object(forInfoDictionaryKey: "TodoneRepoPath") as? String
            ?? FileManager.default.currentDirectoryPath
        let repo = URL(fileURLWithPath: repoPath)
        let fake = env["TODONE_FAKE"] == "1" || processInfo.arguments.contains("--fake")
        var childEnv = env
        // Launched from Finder the PATH is minimal; gog lives in Homebrew.
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        childEnv["PATH"] = ["/opt/homebrew/bin", "/usr/local/bin", "\(home)/.local/bin", env["PATH"] ?? "/usr/bin:/bin"]
            .joined(separator: ":")
        childEnv["PYTHONUNBUFFERED"] = "1"
        return HelperSettings(
            python: repo.appendingPathComponent(".venv/bin/python"),
            module: "todoist_cli.todone_server",
            fake: fake,
            environment: childEnv
        )
    }
}
