import AppKit
import Foundation

// Render the project's vector TV and remote artwork as a macOS icon set.
let destination = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
try FileManager.default.createDirectory(at: destination, withIntermediateDirectories: false)
func color(_ red: CGFloat, _ green: CGFloat, _ blue: CGFloat) -> NSColor {
    NSColor(srgbRed: red / 255, green: green / 255, blue: blue / 255, alpha: 1)
}
func rectangle(_ rect: NSRect, radius: CGFloat, fill: NSColor) {
    fill.setFill(); NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius).fill()
}
for points in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
        let pixels = points * scale
        let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels,
            bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
            colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
        let transform = NSAffineTransform()
        transform.scale(by: CGFloat(pixels) / 128)
        transform.concat()
        rectangle(NSRect(x: 8, y: 32, width: 108, height: 76), radius: 14, fill: color(28, 113, 216))
        rectangle(NSRect(x: 16, y: 40, width: 92, height: 60), radius: 8, fill: color(98, 160, 234))
        rectangle(NSRect(x: 24, y: 86, width: 28, height: 6), radius: 3, fill: color(153, 193, 241))
        rectangle(NSRect(x: 46, y: 19, width: 32, height: 17), radius: 3, fill: color(28, 113, 216))
        rectangle(NSRect(x: 35, y: 15, width: 54, height: 8), radius: 4, fill: color(28, 113, 216))
        rectangle(NSRect(x: 86, y: 11, width: 31, height: 63), radius: 12, fill: color(36, 31, 49))
        color(119, 118, 123).setFill(); NSBezierPath(ovalIn: NSRect(x: 92, y: 35, width: 20, height: 20)).fill()
        color(222, 221, 218).setFill(); NSBezierPath(ovalIn: NSRect(x: 98, y: 41, width: 8, height: 8)).fill()
        color(246, 97, 81).setFill(); NSBezierPath(ovalIn: NSRect(x: 99, y: 61, width: 6, height: 6)).fill()
        rectangle(NSRect(x: 97, y: 24, width: 10, height: 3), radius: 1.5, fill: color(222, 221, 218))
        NSGraphicsContext.restoreGraphicsState()
        let filename = "icon_\(points)x\(points)\(scale == 2 ? "@2x" : "").png"
        try bitmap.representation(using: .png, properties: [:])!.write(to: destination.appendingPathComponent(filename), options: .withoutOverwriting)
    }
}
