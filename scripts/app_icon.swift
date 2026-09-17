import AppKit

// Draw our own local launcher icon; no platform artwork or collected content.
let output = CommandLine.arguments[1]
let size = 1024
let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size,
                             bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                             isPlanar: false, colorSpaceName: .deviceRGB,
                             bytesPerRow: 0, bitsPerPixel: 0)!
let context = NSGraphicsContext(bitmapImageRep: bitmap)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = context
let backdrop = NSBezierPath(roundedRect: NSRect(x: 42, y: 42, width: 940, height: 940),
                            xRadius: 210, yRadius: 210)
NSGradient(starting: NSColor(calibratedRed: 1.0, green: 0.34, blue: 0.37, alpha: 1),
           ending: NSColor(calibratedRed: 0.79, green: 0.12, blue: 0.28, alpha: 1))!
    .draw(in: backdrop, angle: -60)
let phone = NSBezierPath(roundedRect: NSRect(x: 282, y: 185, width: 460, height: 664),
                         xRadius: 76, yRadius: 76)
NSColor.white.withAlphaComponent(0.97).setFill()
phone.fill()
let screen = NSBezierPath(roundedRect: NSRect(x: 310, y: 215, width: 404, height: 603),
                          xRadius: 52, yRadius: 52)
NSColor(calibratedRed: 1, green: 0.94, blue: 0.94, alpha: 1).setFill()
screen.fill()
NSColor(calibratedRed: 0.88, green: 0.23, blue: 0.31, alpha: 1).setFill()
NSBezierPath(roundedRect: NSRect(x: 440, y: 770, width: 144, height: 22),
             xRadius: 11, yRadius: 11).fill()
let heights: [CGFloat] = [155, 250, 345]
for (index, height) in heights.enumerated() {
    let bar = NSBezierPath(roundedRect: NSRect(x: 361 + CGFloat(index) * 107, y: 312,
                                             width: 76, height: height),
                           xRadius: 25, yRadius: 25)
    NSColor(calibratedRed: 0.95, green: 0.27 + CGFloat(index) * 0.06,
            blue: 0.35 + CGFloat(index) * 0.05, alpha: 1).setFill()
    bar.fill()
}
NSColor(calibratedRed: 0.86, green: 0.18, blue: 0.29, alpha: 1).setFill()
NSBezierPath(roundedRect: NSRect(x: 450, y: 250, width: 124, height: 13),
             xRadius: 6, yRadius: 6).fill()
NSGraphicsContext.restoreGraphicsState()
try bitmap.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: output))
