import SwiftUI
import TodoneCore

private struct TextScaleKey: EnvironmentKey {
    static let defaultValue: CGFloat = 1
}

extension EnvironmentValues {
    /// The chosen text size's scale factor (see TextSize), set once at the root.
    var textScale: CGFloat {
        get { self[TextScaleKey.self] }
        set { self[TextScaleKey.self] = newValue }
    }
}

/// A text style's font at the app's text size. Use this instead of `.font(.caption)`.
private struct AppFont: ViewModifier {
    @Environment(\.textScale) private var scale
    let style: TextStyleSize.Style
    let weight: Font.Weight?

    func body(content: Content) -> some View {
        let defaultWeight: Font.Weight = style == .headline ? .bold : .regular
        content.font(.system(size: TextStyleSize.points(style, scale: scale), weight: weight ?? defaultWeight))
    }
}

/// A fixed width that grows with the text size.
private struct ScaledWidth: ViewModifier {
    @Environment(\.textScale) private var scale
    let width: CGFloat

    func body(content: Content) -> some View { content.frame(width: width * scale) }
}

extension View {
    func appFont(_ style: TextStyleSize.Style, weight: Font.Weight? = nil) -> some View {
        modifier(AppFont(style: style, weight: weight))
    }

    func scaledWidth(_ width: CGFloat) -> some View { modifier(ScaledWidth(width: width)) }

    /// Root setup: every unstyled Text, field and button label gets body text at `size`,
    /// and controls grow a step at the larger sizes.
    func textSize(_ size: TextSize) -> some View {
        appFont(.body)
            .controlSize(size.scale >= 1.3 ? .large : .regular)
            .environment(\.textScale, size.scale)
    }
}
