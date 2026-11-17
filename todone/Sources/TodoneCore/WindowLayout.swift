import CoreGraphics

/// Window widths for the today view alone and for the full planner.
/// The numbers mirror the view sizes in the app; text-sized parts (the panels
/// and the today column) grow with the text size `scale`, padding does not.
public enum WindowLayout {
    /// A day column, the tray or the inbox at the default text size.
    public static let panelWidth: CGFloat = 250
    /// The today column's width range at the default text size.
    public static let todayMinWidth: CGFloat = 380
    public static let todayMaxWidth: CGFloat = 460

    /// Just the today column.
    public static func narrowWidth(scale: CGFloat = 1) -> CGFloat { 480 * scale }

    private static let padding: CGFloat = 16
    private static let gap: CGFloat = 10

    /// Inbox panel, today column, every day column and the tray, side by side.
    public static func wideWidth(dayColumns: Int, scale: CGFloat = 1) -> CGFloat {
        let panel = panelWidth * scale + padding
        let inbox = panel + 2 * gap + 1
        let today = todayMaxWidth * scale + 1
        let board = 2 * gap + CGFloat(dayColumns + 1) * panel + CGFloat(dayColumns) * gap
        return inbox + today + board
    }

    public static func width(planner: Bool, dayColumns: Int, scale: CGFloat = 1) -> CGFloat {
        planner ? wideWidth(dayColumns: dayColumns, scale: scale) : narrowWidth(scale: scale)
    }

    /// The window frame at `width`: same top-left corner and height, but pulled
    /// left (and no wider than the screen) so it stays fully visible.
    public static func frame(current: CGRect, screen: CGRect, width: CGFloat) -> CGRect {
        let w = min(width, screen.width)
        let x = max(screen.minX, min(current.minX, screen.maxX - w))
        return CGRect(x: x, y: current.minY, width: w, height: current.height)
    }
}
