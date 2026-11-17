import CoreGraphics

/// The app-wide text size, chosen from the View menu and remembered between launches.
/// macOS has no Dynamic Type, so the app scales its own fonts and column widths.
public enum TextSize: String, CaseIterable, Sendable {
    case small, standard, large, larger, largest

    public var scale: CGFloat {
        switch self {
        case .small: return 0.9
        case .standard: return 1
        case .large: return 1.15
        case .larger: return 1.3
        case .largest: return 1.5
        }
    }

    public var label: String {
        switch self {
        case .small: return "Small"
        case .standard: return "Default"
        case .large: return "Large"
        case .larger: return "Larger"
        case .largest: return "Largest"
        }
    }

    /// One step up, or this size if it is already the largest.
    public var bigger: TextSize { step(1) }
    /// One step down, or this size if it is already the smallest.
    public var smaller: TextSize { step(-1) }

    private func step(_ delta: Int) -> TextSize {
        let all = TextSize.allCases
        let index = all.firstIndex(of: self)! + delta
        return all[max(0, min(all.count - 1, index))]
    }

    /// A stored value read back; anything unknown falls back to the default.
    public init(stored: String?) {
        self = stored.flatMap(TextSize.init(rawValue:)) ?? .standard
    }
}

/// The macOS point sizes of the text styles the app uses, so they can be scaled.
public enum TextStyleSize {
    public enum Style: Sendable { case title2, title3, headline, body, callout, caption }

    public static func base(_ style: Style) -> CGFloat {
        switch style {
        case .title2: return 17
        case .title3: return 15
        case .headline, .body: return 13
        case .callout: return 12
        case .caption: return 10
        }
    }

    public static func points(_ style: Style, scale: CGFloat) -> CGFloat {
        (base(style) * scale).rounded()
    }
}
