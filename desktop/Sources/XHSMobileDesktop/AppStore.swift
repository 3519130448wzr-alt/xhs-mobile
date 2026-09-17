import AppKit
import Combine
import Foundation

@MainActor final class AppStore: ObservableObject {
    static let shared = AppStore()
    @Published var state = AppState()
    @Published var page: NavigationPage = .collect
    @Published var keywords = ""
    @Published var targetText = "10"
    @Published var selectedTaskID: String?
    @Published var records: [NoteRecord] = []
    @Published var failureEvidence: [EvidenceLink] = []
    @Published var recordTotal = 0
    @Published var nextOffset: Int?
    @Published var detailLoading = false
    @Published var alertIssue: Incident?
    @Published var showingIssue = false
    @Published var localIssue: Incident?
    @Published var notice: String?
    @Published var pendingAction = false
    @Published var terminating = false
    @Published var cancellingTermination = false
    @Published var bridgeAlive = false
    @Published var preview = false
    @Published var showingConnectionSettings = false
    private(set) var connectionRecordPath = ""
    private var networkMonitor: ConnectionNetworkMonitor?
    private var process: Process?
    private var input: FileHandle?
    private var output: FileHandle?
    private var errorLog: FileHandle?
    private var timer: Timer?
    private var callbacks: [String: (JSONObject?, Incident?) -> Void] = [:]
    private var receipts = RequestReceiptTracker()
    private var statusID: String?
    private var shownIncidents: Set<String> = []
    private var exitWasRequested = false
    private var started = false
    private var connectionLost = false
    private var projectURL: URL?
    private let nativeLogDirectory = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Logs/XHSMobileDesktop", isDirectory: true)
    private var detailGeneration = 0
    private let readerQueue = DispatchQueue(label: "com.xhs.mobile.desktop.protocol")

    var issue: Incident? { localIssue ?? state.issue }
    var canStart: Bool { bridgeAlive && state.initialized && !state.busy && !pendingAction && !terminating && !receipts.hasUnconfirmedActions }
    var deviceCheckInProgress: Bool { state.deviceCheckInProgress(bridgeAlive: bridgeAlive) }
    var target: Int {
        get { Int(targetText.trimmingCharacters(in: .whitespacesAndNewlines)) ?? 0 }
        set { targetText = String(newValue) }
    }
    var validTarget: Bool { (1...500).contains(target) }
    var selectedTask: TaskItem? { state.tasks.first { $0.id == selectedTaskID } }
    var activeTask: TaskItem? { state.tasks.first(where: \.active) }
    var keywordLines: [String] {
        keywords.components(separatedBy: .newlines).map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
    }
    var uniqueKeywords: [String] {
        var seen: Set<String> = []
        return keywordLines.filter { seen.insert($0).inserted }
    }
    var duplicateCount: Int { keywordLines.count - uniqueKeywords.count }

    func launch() {
        guard !started else { return }; started = true
        if CommandLine.arguments.contains("--ui-preview") {
            preview = true; bridgeAlive = true; state = PreviewData.state
            if let index = CommandLine.arguments.firstIndex(of: "--page"), CommandLine.arguments.count > index + 1 {
                page = NavigationPage(rawValue: CommandLine.arguments[index + 1]) ?? .collect
            }
            return
        }
        do {
            guard let configURL = Bundle.main.url(forResource: "desktop-config", withExtension: "json") else {
                throw DesktopError.message("找不到桌面配置，请重新运行项目中的桌面 App 构建程序。")
            }
            let config = try JSONSerialization.jsonObject(with: Data(contentsOf: configURL)) as? JSONObject ?? [:]
            let project = config.string("project_path"), python = config.string("python_path"), bridge = config.string("bridge_path")
            guard !project.isEmpty, !python.isEmpty, !bridge.isEmpty, FileManager.default.isExecutableFile(atPath: python), FileManager.default.fileExists(atPath: bridge) else {
                throw DesktopError.message("项目环境或控制程序不可用，请检查项目文件是否被移动。")
            }
            projectURL = URL(fileURLWithPath: project, isDirectory: true)
            connectionRecordPath = config.string("connection_record_path")
            let logDir = nativeLogDirectory
            try FileManager.default.createDirectory(at: logDir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: logDir.path)
            let logURL = logDir.appendingPathComponent("desktop-stderr.log")
            if !FileManager.default.fileExists(atPath: logURL.path) {
                FileManager.default.createFile(atPath: logURL.path, contents: nil, attributes: [.posixPermissions: 0o600])
            }
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: logURL.path)
            errorLog = try FileHandle(forWritingTo: logURL); try errorLog?.seekToEnd()
            let worker = Process(), sendPipe = Pipe(), receivePipe = Pipe()
            worker.executableURL = URL(fileURLWithPath: python)
            worker.arguments = [bridge, "--project", project, "--device", config.string("device_id", "lab01")]
            worker.currentDirectoryURL = projectURL
            var environment = ProcessInfo.processInfo.environment
            environment["PYTHONUTF8"] = "1"; environment["PYTHONUNBUFFERED"] = "1"
            worker.environment = environment
            worker.standardInput = sendPipe; worker.standardOutput = receivePipe; worker.standardError = errorLog
            worker.terminationHandler = { [weak self] child in
                let code = child.terminationStatus
                Task { @MainActor in self?.bridgeEnded(code: code) }
            }
            process = worker; input = sendPipe.fileHandleForWriting; output = receivePipe.fileHandleForReading
            try worker.run(); bridgeAlive = true
            let reader = receivePipe.fileHandleForReading
            readerQueue.async { [weak self] in
                var decoder = JSONLDecoder()
                while true {
                    let chunk = reader.availableData
                    if chunk.isEmpty {
                        Task { @MainActor in self?.stdoutEnded() }
                        break
                    }
                    do {
                        for json in try decoder.feed(chunk) {
                            Task { @MainActor in self?.receive(json) }
                        }
                    } catch {
                        Task { @MainActor in self?.protocolFailure() }; break
                    }
                }
            }
            request("initialize") { [weak self] _, error in
                guard let self, let error else { return }
                if error.code == "bridge_timeout" {
                    self.report(Incident(code: "startup_wait", title: "等待本机环境就绪", message: "首次运行时，macOS 可能正在请求“文稿”文件夹访问权限。请检查系统提示并选择允许，然后等待加载；若仍未连接，请退出并重新打开采集助手。", requestID: error.requestID))
                } else { self.report(error) }
            }
            let monitor = ConnectionNetworkMonitor()
            networkMonitor = monitor
            monitor.start { [weak self] in Task { @MainActor in self?.networkChanged() } }
            timer = Timer(timeInterval: 3, repeats: true) { [weak self] _ in Task { @MainActor in self?.poll() } }
            RunLoop.main.add(timer!, forMode: .common)
        } catch { report(Incident(code: "desktop_start", title: "采集助手暂时无法启动", message: error.localizedDescription)) }
    }

    private func receive(_ json: JSONObject) {
        guard !connectionLost else { return }
        if json.string("event") == "state", let data = json["data"] as? JSONObject { update(data); return }
        guard let id = json["id"] as? String else { return }
        let receipt = receipts.acknowledge(id: id, success: json.bool("ok"))
        let resolvesVisibleTimeout = localIssue?.requestID.map { receipt.resolvedTimeoutIDs.contains($0) } ?? false
        clearResolvedTimeout(receipt.resolvedTimeoutIDs)
        if id == statusID { statusID = nil }
        let completion = callbacks.removeValue(forKey: id)
        if json.bool("ok") {
            let result = json["result"] as? JSONObject ?? [:]
            if result["initialized"] != nil { update(result) }
            else if let data = result["state"] as? JSONObject { update(data) }
            completion?(result, nil)
        } else {
            let data = json.object("error")
            let error = Incident(code: data.string("code", "operation_failed"), title: "操作未完成", message: data.string("message", "请查看诊断详情后重试。"))
            completion?(nil, error)
            if receipt.wasLate && resolvesVisibleTimeout { report(error) }
        }
    }

    private func clearResolvedTimeout(_ requestIDs: Set<String>) {
        if let id = localIssue?.requestID, requestIDs.contains(id) { localIssue = nil }
        if let id = alertIssue?.requestID, requestIDs.contains(id) { alertIssue = nil; showingIssue = false }
        if localIssue == nil && state.issue == nil { NSApp.dockTile.badgeLabel = nil }
    }

    private func update(_ data: JSONObject) {
        let previousActivity = state.activity
        state = AppState(data)
        state.normalizeDeviceChecking(bridgeAlive: bridgeAlive)
        if let id = selectedTaskID, !state.tasks.contains(where: { $0.id == id }) { selectedTaskID = nil; records = [] }
        if let paused = completedPauseTask(previousActivity: previousActivity, state: state), page != .device {
            page = .results
            select(paused)
            notice = "已安全暂停，已保存成果保留。可以查看结果或继续采集。"
        }
        if let issue = state.issue { present(issue) }
        else if localIssue == nil { NSApp.dockTile.badgeLabel = nil }
        if terminating && state.shuttingDown && !state.busy && state.activity == nil { finishTermination() }
    }

    @discardableResult private func request(_ method: String, _ params: JSONObject = [:], completion: @escaping (JSONObject?, Incident?) -> Void = { _, _ in }) -> String? {
        guard bridgeAlive, let input else {
            completion(nil, Incident(code: "bridge_unavailable", title: "控制程序未连接", message: "请退出并重新打开采集助手。已经保存的结果不会丢失。")); return nil
        }
        let id = UUID().uuidString
        do {
            var data = try JSONSerialization.data(withJSONObject: ["id": id, "method": method, "params": params], options: [.sortedKeys])
            data.append(10); callbacks[id] = completion; receipts.register(id: id, method: method)
            try input.write(contentsOf: data)
            return id
        } catch {
            callbacks.removeValue(forKey: id); receipts.discard(id: id)
            completion(nil, Incident(code: "bridge_write", title: "控制连接已中断", message: "未自动重发操作。请重新打开 App 核对任务状态后再继续。"))
            return nil
        }
    }

    private func poll() {
        for request in receipts.expire() {
            if statusID == request.id { statusID = nil }
            callbacks.removeValue(forKey: request.id)?(nil, Incident(code: "bridge_timeout", title: "控制程序响应较慢", message: "尚未收到操作确认。请查看任务状态，采集操作不会自动重发。", requestID: request.id))
        }
        guard bridgeAlive, statusID == nil, !preview else { return }
        statusID = request("status") { [weak self] _, error in
            guard let self, let error else { return }
            if self.localIssue?.code != "startup_wait" && !self.receipts.hasUnconfirmedActions { self.report(error) }
        }
    }

    func refresh() { if preview { return }; poll() }

    private func networkChanged() {
        guard bridgeAlive, state.initialized, !terminating, !preview else { return }
        request("network_changed") { _, _ in }
    }

    func openConnectionGuide() {
        guard let projectURL else { return }
        let guide = projectURL.appendingPathComponent("docs/AUTO_CONNECTION.md")
        if FileManager.default.fileExists(atPath: guide.path) { openPath(guide.path) }
        else { notice = "请查看项目中的云手机接入说明，准备专用名单与 RAM 凭据后完成设置。" }
    }

    func perform(_ method: String, params: JSONObject = [:]) {
        guard !preview else { notice = "这是 synthetic 界面预览，不会操作设备或写入采集数据。"; return }
        guard !pendingAction else { return }
        pendingAction = true
        if localIssue?.requestID.flatMap({ receipts.timedOut[$0] }) == nil { localIssue = nil }
        notice = nil
        request(method, params) { [weak self] _, error in
            guard let self else { return }; self.pendingAction = false
            if let error { self.report(error) }
            else { self.refresh() }
        }
    }

    func start(current: Bool = false) {
        guard canStart else { return }
        if !current && (uniqueKeywords.isEmpty || uniqueKeywords.count > 20 || !validTarget) { return }
        perform("start", params: ["mode": current ? "current" : "keywords", "keywords": current ? [] : uniqueKeywords, "limit": current ? 1 : target])
    }

    func resume(_ item: TaskItem) {
        guard canStart else { return }
        var params = item.parameters; params["acknowledge"] = item.requiresAck
        perform("resume", params: params)
    }

    func select(_ item: TaskItem) {
        detailGeneration += 1
        selectedTaskID = item.id; records = []; failureEvidence = []; nextOffset = nil; recordTotal = 0
        detailLoading = false
        loadRecords()
    }

    func loadRecords(more: Bool = false) {
        guard let item = selectedTask, !detailLoading else { return }
        if preview { records = PreviewData.records; recordTotal = records.count; return }
        detailLoading = true
        let generation = detailGeneration
        var params = item.parameters; params["offset"] = more ? (nextOffset ?? 0) : 0; params["limit"] = 30
        request("detail", params) { [weak self] result, error in
            guard let self, self.detailGeneration == generation, self.selectedTaskID == item.id else { return }
            self.detailLoading = false
            if let error { self.report(error); return }
            guard let result else { return }
            let fresh = result.objects("records").map(NoteRecord.init)
            self.records = more ? self.records + fresh : fresh
            self.failureEvidence = result.objects("failure_evidence_paths").map(EvidenceLink.init)
            self.recordTotal = result.integer("total"); self.nextOffset = result["next_offset"] as? Int
        }
    }

    func openCloud() {
        guard let url = URL(string: state.device.consoleURL), url.scheme == "https", url.host != nil, url.user == nil, url.password == nil else {
            notice = "云手机链接配置无效。请在设备连接页复制并检查地址。"; return
        }
        if !NSWorkspace.shared.open(url) { notice = "浏览器未能打开，请点击“复制链接”，在浏览器中粘贴。" }
    }

    func copyCloud() {
        NSPasteboard.general.clearContents(); NSPasteboard.general.setString(state.device.consoleURL, forType: .string)
        notice = "已复制云手机链接。"
    }

    func openPath(_ path: String) {
        guard path.hasPrefix("/"), FileManager.default.fileExists(atPath: path) else { notice = "文件暂不可用。请重新导出，或查看诊断详情。"; return }
        if !NSWorkspace.shared.open(URL(fileURLWithPath: path)) { notice = "系统未能打开文件，可在诊断详情中核对路径。" }
    }

    func openLogs() {
        if !state.logDirectory.isEmpty { openPath(state.logDirectory) }
        else { openPath(nativeLogDirectory.path) }
    }

    func report(_ issue: Incident) { localIssue = issue; present(issue) }

    private func present(_ issue: Incident) {
        guard !shownIncidents.contains(issue.id) else { return }
        NSApp.dockTile.badgeLabel = "!"
        guard NSApp.isActive, NSApp.windows.contains(where: { $0.isVisible }) else { return }
        shownIncidents.insert(issue.id); alertIssue = issue; showingIssue = true
    }

    func activated() { if let issue { present(issue) } }

    private func protocolFailure() {
        disconnect(Incident(code: "invalid_protocol", title: "收到无法识别的控制消息", message: "采集操作不会自动重发。请查看诊断详情，并重新打开 App 核对任务状态。"))
    }

    private func stdoutEnded() {
        guard !terminating && !exitWasRequested else {
            try? input?.close(); input = nil
            return
        }
        disconnect(Incident(code: "bridge_eof", title: "控制连接已中断", message: "已关闭采集控制管道并请求安全暂停，当前运行状态待核对。请重新打开 App；已保存记录不会丢失。"))
    }

    private func disconnect(_ issue: Incident) {
        guard !connectionLost else { return }
        connectionLost = true
        networkMonitor?.cancel(); networkMonitor = nil
        bridgeAlive = false; pendingAction = false; detailLoading = false
        callbacks.removeAll(); receipts.removeAll(); statusID = nil
        timer?.invalidate(); timer = nil
        state.normalizeDeviceChecking(bridgeAlive: false)
        state.device.status = "unknown"; state.device.message = "控制连接已中断，请重新打开 App 核对设备与任务状态。"
        try? input?.close(); input = nil
        report(issue)
    }

    private func bridgeEnded(code: Int32) {
        bridgeAlive = false; pendingAction = false; detailLoading = false; timer?.invalidate(); timer = nil
        input = nil; callbacks.removeAll(); receipts.removeAll(); statusID = nil
        if cancellingTermination {
            cancellingTermination = false; terminating = false; exitWasRequested = false
            NSApp.reply(toApplicationShouldTerminate: false)
        } else if terminating { finishTermination(); return }
        guard !exitWasRequested else { return }
        disconnect(Incident(code: "bridge_exit_\(code)", title: "控制程序已停止", message: "采集控制管道已关闭，程序会请求安全暂停。请重新打开 App 核对任务状态；已保存结果不会丢失。"))
    }

    func beginTermination() -> NSApplication.TerminateReply {
        if preview || !bridgeAlive { return .terminateNow }
        if terminating { return .terminateLater }
        if let window = NSApp.windows.first(where: { $0.title == "小红书采集助手" }) {
            window.makeKeyAndOrderFront(nil)
        }
        terminating = true; exitWasRequested = true
        request("shutdown") { [weak self] _, error in
            guard let self else { return }
            if let error {
                self.terminating = false; self.exitWasRequested = false
                NSApp.reply(toApplicationShouldTerminate: false); self.report(error)
            }
        }
        return .terminateLater
    }

    func cancelTermination() {
        guard !cancellingTermination else { return }
        cancellingTermination = true
        request("cancel_shutdown") { [weak self] _, error in
            guard let self else { return }
            self.terminating = false; self.exitWasRequested = false; self.cancellingTermination = false
            NSApp.reply(toApplicationShouldTerminate: false)
            if let error { self.report(error); return }
            self.notice = "已取消退出。已请求的暂停会正常完成，可稍后继续。"
        }
    }

    private func finishTermination() {
        guard terminating && !cancellingTermination else { return }
        networkMonitor?.cancel(); networkMonitor = nil
        timer?.invalidate(); try? input?.close(); input = nil
        NSApp.reply(toApplicationShouldTerminate: true)
    }
}

enum DesktopError: LocalizedError {
    case message(String)
    var errorDescription: String? { if case let .message(value) = self { return value }; return nil }
}
