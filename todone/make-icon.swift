// Draws Todone's app icon (a white check on a green rounded square) into an
// .iconset folder; build-app.sh turns that into AppIcon.icns with iconutil.
//
//   swift make-icon.swift <out.iconset>
import AppKit

guard CommandLine.arguments.count == 2 else {
    FileHandle.standardError.write("usage: swift make-icon.swift <out.iconset>\n".data(using: .utf8)!)
    exit(2)
}
let out = URL(fileURLWithPath: CommandLine.arguments[1])
try FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)

func draw(pixels: Int) -> Data {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels,
                               bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                               colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    let s = CGFloat(pixels)

    // macOS icon grid: the shape fills ~80% of the canvas, corners ~22% of the shape.
    let inset = s * 0.1
    let tile = NSRect(x: inset, y: inset, width: s - 2 * inset, height: s - 2 * inset)
    let shape = NSBezierPath(roundedRect: tile, xRadius: tile.width * 0.225, yRadius: tile.width * 0.225)

    NSGraphicsContext.saveGraphicsState()
    let shadow = NSShadow()
    shadow.shadowColor = NSColor.black.withAlphaComponent(0.3)
    shadow.shadowOffset = NSSize(width: 0, height: -s * 0.01)
    shadow.shadowBlurRadius = s * 0.02
    shadow.set()
    NSColor(red: 0.16, green: 0.70, blue: 0.33, alpha: 1).setFill()
    shape.fill()
    NSGraphicsContext.restoreGraphicsState()

    let gradient = NSGradient(starting: NSColor(red: 0.30, green: 0.84, blue: 0.45, alpha: 1),
                              ending: NSColor(red: 0.10, green: 0.58, blue: 0.27, alpha: 1))!
    gradient.draw(in: shape, angle: -90)

    let check = NSBezierPath()
    check.move(to: NSPoint(x: tile.minX + tile.width * 0.24, y: tile.minY + tile.height * 0.50))
    check.line(to: NSPoint(x: tile.minX + tile.width * 0.43, y: tile.minY + tile.height * 0.30))
    check.line(to: NSPoint(x: tile.minX + tile.width * 0.77, y: tile.minY + tile.height * 0.70))
    check.lineWidth = tile.width * 0.12
    check.lineCapStyle = .round
    check.lineJoinStyle = .round
    NSColor.white.setStroke()
    check.stroke()

    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

for points in [16, 32, 128, 256, 512] {
    for (scale, suffix) in [(1, ""), (2, "@2x")] {
        let name = "icon_\(points)x\(points)\(suffix).png"
        try draw(pixels: points * scale).write(to: out.appendingPathComponent(name))
    }
}
