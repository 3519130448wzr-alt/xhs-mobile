import Foundation

typealias JSONObject = [String: Any]

enum DesktopProtocolError: Error { case oversized, malformed }

struct JSONLDecoder {
    private var buffer = Data()
    mutating func feed(_ chunk: Data) throws -> [JSONObject] {
        buffer.append(chunk)
        var messages: [JSONObject] = []
        while let end = buffer.firstIndex(of: 10) {
            if buffer.distance(from: buffer.startIndex, to: end) > 8_000_000 { throw DesktopProtocolError.oversized }
            let line = Data(buffer[..<end]); buffer.removeSubrange(...end)
            if line.isEmpty { continue }
            guard let value = (try? JSONSerialization.jsonObject(with: line)) as? JSONObject else { throw DesktopProtocolError.malformed }
            messages.append(value)
        }
        if buffer.count > 8_000_000 { throw DesktopProtocolError.oversized }
        return messages
    }
}

extension Dictionary where Key == String, Value == Any {
    func string(_ key: String, _ fallback: String = "") -> String { self[key] as? String ?? fallback }
    func integer(_ key: String) -> Int { (self[key] as? NSNumber)?.intValue ?? 0 }
    func bool(_ key: String) -> Bool { self[key] as? Bool ?? false }
    func object(_ key: String) -> JSONObject { self[key] as? JSONObject ?? [:] }
    func objects(_ key: String) -> [JSONObject] { self[key] as? [JSONObject] ?? [] }
}

struct DeviceCheck: Identifiable {
    var id: String
    var label: String
    var status: String
    var message: String
    init(_ json: JSONObject) {
        id = json.string("id"); label = json.string("label")
        status = json.string("status", "unknown"); message = json.string("message")
    }
}

struct DeviceState {
    var id = "lab01"
    var status = "unknown"
    var message = "打开后不会自动开始采集。"
    var consoleURL = "https://wya.wuying.aliyun.com/instanceLayouts"
    var checks: [DeviceCheck] = []
    var connectionPhase = ""
    var connectionDeadline: String?
    init(_ json: JSONObject = [:]) {
        id = json.string("id", "lab01"); status = json.string("status", "unknown")
        message = json.string("message", "请检查设备，或直接开始新采集。")
        consoleURL = json.string("console_url", "https://wya.wuying.aliyun.com/instanceLayouts")
        checks = json.objects("checks").map(DeviceCheck.init)
        connectionPhase = json.string("connection_phase")
        connectionDeadline = json["connection_deadline"] as? String
    }
    var label: String {
        ["unknown": "尚未检查", "checking": "正在检查", "incomplete": "检查未完成", "ready": "手机已连接", "offline": "连接待处理",
         "busy": "设备正在使用", "attention": "需要处理"][status] ?? "需要检查"
    }
}

struct Activity {
    let phase: String
    let kind: String
    let id: String
    let keyword: String
    let message: String
    init(_ json: JSONObject) {
        phase = json.string("phase"); kind = json.string("kind"); id = json.string("id")
        keyword = json.string("keyword"); message = json.string("message")
    }
    var label: String {
        ["preparing": "正在准备", "running": "正在采集", "pausing": "正在安全暂停", "exporting": "正在导出结果"][phase] ?? "正在处理"
    }
}

struct TaskItem: Identifiable {
    let id: String
    let kind: String
    let title: String
    let status: String
    let stopReason: String
    let observations: Int
    let eligible: Int
    let reliableIdentities: Int
    let confirmedIdentities: Int
    let target: Int
    let createdAt: String
    let updatedAt: String
    let active: Bool
    let canResume: Bool
    let requiresAck: Bool
    let requiresReadCheck: Bool
    let consecutiveReadFailures: Int
    let consecutiveNoProgress: Int
    let children: [TaskItem]
    let exportPath: String?
    let cooldownUntil: String?
    init(_ json: JSONObject) {
        id = json.string("id"); kind = json.string("kind", "task"); title = json.string("title", "采集任务")
        status = json.string("status"); stopReason = json.string("stop_reason")
        observations = json.integer("observations"); eligible = json.integer("eligible")
        reliableIdentities = json.integer("reliable_identities")
        confirmedIdentities = json.integer("confirmed_identities"); target = json.integer("target")
        createdAt = json.string("created_at"); updatedAt = json.string("updated_at")
        active = json.bool("active"); canResume = json.bool("can_resume"); requiresAck = json.bool("requires_ack")
        requiresReadCheck = json.bool("requires_read_check")
        consecutiveReadFailures = json.object("read_anomaly").integer("consecutive_failures")
        consecutiveNoProgress = json.object("read_anomaly").integer("consecutive_no_progress")
        children = json.objects("tasks").map(TaskItem.init)
        exportPath = json["export_path"] as? String; cooldownUntil = json["cooldown_until"] as? String
    }
    var progress: Double { target > 0 ? min(Double(eligible) / Double(target), 1) : 0 }
    var label: String {
        if active && status == "running" { return "采集中" }
        return ["pending": "等待开始", "queued": "等待开始", "running": "待检查运行状态", "paused": "已暂停",
                "pausing": "正在暂停", "cooldown": "冷却中", "cooling_down": "冷却中", "needs_attention": "需要处理",
                "partial": "部分完成", "collected_awaiting_review": "采集完成 · 待核验", "completed": "采集结束",
                "failed": "已停止", "cancelled": "已停止"][status] ?? "状态待确认"
    }
    var parameters: JSONObject { ["kind": kind, "id": id] }
}

struct Incident: Identifiable {
    let id: String
    let code: String
    let title: String
    let message: String
    let cloudAction: Bool
    let canRetry: Bool
    let requestID: String?
    var connectionSettingsAction: Bool {
        ["credentials", "authorization", "cloud_timeout", "connection_timeout", "journal_pending", "configuration", "cloud_error", "connection_recovery_exhausted"].contains(code)
    }
    init(_ json: JSONObject) {
        id = json.string("id", UUID().uuidString); code = json.string("code")
        title = json.string("title", "需要处理"); message = json.string("message")
        cloudAction = json.bool("cloud_action"); canRetry = json.bool("can_retry")
        requestID = json["request_id"] as? String
    }
    init(code: String, title: String, message: String, cloudAction: Bool = false, canRetry: Bool = false, requestID: String? = nil) {
        self.id = code + ":" + message; self.code = code; self.title = title
        self.message = message; self.cloudAction = cloudAction; self.canRetry = canRetry; self.requestID = requestID
    }
}

struct AppState {
    var initialized = false
    var busy = false
    var shuttingDown = false
    var device = DeviceState()
    var activity: Activity?
    var tasks: [TaskItem] = []
    var issue: Incident?
    var logDirectory = ""
    init(_ json: JSONObject = [:]) {
        initialized = json.bool("initialized"); busy = json.bool("busy"); shuttingDown = json.bool("shutting_down")
        device = DeviceState(json.object("device")); tasks = json.objects("tasks").map(TaskItem.init)
        if let value = json["activity"] as? JSONObject { activity = Activity(value) }
        if let value = json["issue"] as? JSONObject { issue = Incident(value) }
        logDirectory = json.string("log_dir")
    }

    func deviceCheckInProgress(bridgeAlive: Bool) -> Bool {
        bridgeAlive && busy && activity?.phase == "preparing"
            && (device.status == "checking" || device.checks.contains { $0.status == "checking" })
    }

    mutating func normalizeDeviceChecking(bridgeAlive: Bool) {
        guard !deviceCheckInProgress(bridgeAlive: bridgeAlive) else { return }
        let message: String
        if !bridgeAlive {
            message = "控制连接已中断，上次检查未完成。请重新打开 App 后检查。"
        } else if busy && activity?.phase == "pausing" {
            message = "检查正在安全停止，等待当前操作结束后可重新检查。"
        } else {
            message = "上次检查未完成，可以重新检查连接。"
        }
        if device.status == "checking" {
            device.status = "incomplete"
            device.message = message
        }
        for index in device.checks.indices where device.checks[index].status == "checking" {
            device.checks[index].status = "incomplete"
            device.checks[index].message = message
        }
    }
}

struct EvidenceLink: Identifiable {
    var id: String { path }
    let label: String
    let path: String
    init(_ json: JSONObject) { label = json.string("label", "来源证据"); path = json.string("path") }
}

func completedPauseTask(previousActivity: Activity?, state: AppState) -> TaskItem? {
    guard let previousActivity, !previousActivity.id.isEmpty,
          !state.busy, state.activity == nil else { return nil }
    return state.tasks.first {
        $0.id == previousActivity.id && $0.kind == previousActivity.kind && !$0.active
            && $0.status == "paused" && $0.stopReason == "operator_pause"
    }
}

struct NoteRecord: Identifiable {
    let id: String
    let title: String
    let author: String
    let body: String
    let eligible: Bool
    let quality: String
    let titleStatus: String
    let pageTime: String
    let timeLabel: String
    let timeStatusLabel: String
    let topics: [String]
    let identityStatus: String
    let noteID: String
    let evidence: [EvidenceLink]
    init(_ json: JSONObject) {
        id = json.string("id"); title = json.string("title"); author = json.string("author")
        body = json.string("body"); eligible = json.bool("eligible"); quality = json.string("quality")
        titleStatus = json.string("title_status", "not_readable")
        pageTime = json.string("page_time")
        timeLabel = json.string("time_label", "页面时间")
        timeStatusLabel = json.string("time_status_label", "未读取")
        topics = json.string("topics_status") == "confirmed" ? (json["topics"] as? [String] ?? []) : []
        identityStatus = json.string("identity_status", "unverified"); noteID = json.string("note_id")
        evidence = json.objects("evidence_paths").map(EvidenceLink.init)
    }
    var titlePlaceholder: String { titleStatus == "not_displayed" ? "页面未展示标题" : "标题未能读取" }
    var topicsLabel: String { topics.isEmpty ? "平台话题未能识别" : topics.joined(separator: " · ") }
    var identityLabel: String {
        identityStatus == "verified" && !noteID.isEmpty ? "笔记身份已核验 · ID \(noteID)" : "身份未核验 · 可能与其他观察记录重复"
    }
}

enum NavigationPage: String, CaseIterable, Identifiable {
    case collect = "开始采集", results = "任务与结果", device = "设备连接"
    var id: String { rawValue }
    var symbol: String {
        switch self { case .collect: return "square.and.pencil"; case .results: return "tray.full"; case .device: return "iphone.gen3.radiowaves.left.and.right" }
    }
    var subtitle: String {
        switch self {
        case .collect: return "写下关键词，其他交给采集助手。"
        case .results: return "每次采集的进度、原文与来源，都在这里。"
        case .device: return "连接状态清楚可见，遇到问题随时处理。"
        }
    }
}

func parsedDate(_ value: String?) -> Date? {
    guard let value else { return nil }
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if let result = formatter.date(from: value) { return result }
    formatter.formatOptions = [.withInternetDateTime]
    return formatter.date(from: value)
}

func shortDate(_ value: String) -> String {
    guard let date = parsedDate(value) else { return value.isEmpty ? "" : String(value.prefix(16)).replacingOccurrences(of: "T", with: " ") }
    let formatter = DateFormatter(); formatter.locale = Locale(identifier: "zh_CN"); formatter.dateFormat = "M 月 d 日 HH:mm"
    return formatter.string(from: date)
}
