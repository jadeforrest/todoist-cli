import AppKit
import SwiftUI
import TodoneCore

@main
struct TodoneApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var model = AppModel()

    var body: some Scene {
        WindowGroup("Todone") {
            ContentView()
                .environmentObject(model)
                .textSize(model.textSize)
                .frame(minWidth: 460, minHeight: 500)
                .onReceive(NotificationCenter.default.publisher(for: NSApplication.willTerminateNotification)) { _ in
                    model.shutdown()
                }
        }
        .commands {
            CommandGroup(after: .toolbar) {
                Picker("Text Size", selection: $model.textSize) {
                    ForEach(TextSize.allCases, id: \.self) { Text($0.label).tag($0) }
                }
                Button("Bigger Text") { model.textSize = model.textSize.bigger }
                    .keyboardShortcut("+")
                    .disabled(model.textSize == model.textSize.bigger)
                Button("Smaller Text") { model.textSize = model.textSize.smaller }
                    .keyboardShortcut("-")
                    .disabled(model.textSize == model.textSize.smaller)
                Button("Default Text Size") { model.textSize = .standard }
                    .keyboardShortcut("0")
                Divider()
            }
            CommandGroup(after: .toolbar) {
                Button("Refresh") { Task { await model.refresh(forceCalendar: true) } }
                    .keyboardShortcut("r")
                Button(model.showPlanner ? "Hide Planner" : "Show Planner") { model.togglePlanner() }
                    .keyboardShortcut("p")
                Button("Plan") { model.showPlanner = true; Task { await model.plan() } }
                    .keyboardShortcut("p", modifiers: [.command, .shift])
                    .disabled(model.snapshot?.planning == true)
                Button("Confirm Plan") { Task { await model.confirmPlan() } }
                    .keyboardShortcut(.return, modifiers: .command)
                    .disabled(model.snapshot?.planning != true)
                Button("Cancel Plan") { Task { await model.cancelPlan() } }
                    .disabled(model.snapshot?.planning != true)
            }
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        // A write to the helper after it has exited must fail, not kill the app.
        signal(SIGPIPE, SIG_IGN)
        // Run as a regular app with a Dock icon and menu bar even when started
        // as a bare executable (swift run).
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}
