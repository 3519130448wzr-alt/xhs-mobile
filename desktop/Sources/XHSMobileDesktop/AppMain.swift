import AppKit
import SwiftUI

@main enum DesktopMain {
    @MainActor static func main() {
        if CommandLine.arguments.contains("--self-test") {
            exit(DesktopSelfTests.run())
        }
        if CommandLine.arguments.contains("--connection-helper") { exit(ConnectionHelper.run()) }
        XHSMobileDesktopApp.main()
    }
}

struct XHSMobileDesktopApp: App {
    @NSApplicationDelegateAdaptor(DesktopAppDelegate.self) var delegate
    @StateObject private var store = AppStore.shared
    var body: some Scene {
        Window("小红书采集助手", id: "main") {
            ContentView().environmentObject(store)
                .frame(minWidth: 960, minHeight: 680)
                .background(WindowLifecycleAccessor())
                .onAppear { store.launch() }
        }
        .defaultSize(width: 1160, height: 810)
        .windowStyle(.hiddenTitleBar)
        .commands {
            CommandGroup(replacing: .newItem) {}
            CommandMenu("采集") {
                Button("开始采集") { store.page = .collect }.keyboardShortcut("1", modifiers: .command)
                Button("任务与结果") { store.page = .results }.keyboardShortcut("2", modifiers: .command)
                Button("设备连接") { store.page = .device }.keyboardShortcut("3", modifiers: .command)
                Divider()
                Button("打开云手机") { store.openCloud() }
                Button("诊断详情") { store.openLogs() }
            }
        }
    }
}

private struct WindowLifecycleAccessor: NSViewRepresentable {
    func makeNSView(context: Context) -> WindowLifecycleView { WindowLifecycleView() }
    func updateNSView(_ nsView: WindowLifecycleView, context: Context) {}
}

private final class WindowLifecycleView: NSView {
    private let keeper = WindowKeeper()
    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        guard let window, window.delegate !== keeper else { return }
        keeper.previous = window.delegate
        window.delegate = keeper
        window.isReleasedWhenClosed = false
    }
}

private final class WindowKeeper: NSObject, NSWindowDelegate {
    weak var previous: NSWindowDelegate?
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        sender.orderOut(nil)
        return false
    }
    override func responds(to aSelector: Selector!) -> Bool {
        super.responds(to: aSelector) || previous?.responds(to: aSelector) == true
    }
    override func forwardingTarget(for aSelector: Selector!) -> Any? {
        previous?.responds(to: aSelector) == true ? previous : super.forwardingTarget(for: aSelector)
    }
}

@MainActor final class DesktopAppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        signal(SIGPIPE, SIG_IGN)
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag {
            if let window = sender.windows.first(where: { $0.identifier?.rawValue == "main" || $0.title == "小红书采集助手" }) {
                window.makeKeyAndOrderFront(nil)
            } else { sender.windows.first?.makeKeyAndOrderFront(nil) }
        }
        AppStore.shared.activated()
        return true
    }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply { AppStore.shared.beginTermination() }
    func applicationDidBecomeActive(_ notification: Notification) { AppStore.shared.activated() }
}
