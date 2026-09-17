import Foundation

// Headless tests for the exact production models and parser. No App, bridge, device,
// database or network is started. This works with macOS Command Line Tools alone.
@MainActor enum DesktopSelfTests {
    enum Failure: Error { case assertion(String) }
    private static func require(_ value: @autoclosure () -> Bool, _ message: String) throws {
        if !value() { throw Failure.assertion(message) }
    }
    static func run() -> Int32 {
        let cases: [(String, () throws -> Void)] = [
            ("fragmented_chinese_jsonl", {
                let line = #"{"event":"state","data":{"message":"中文"}}\#n{"id":"2","ok":true,"result":{}}\#n"#
                var decoder = JSONLDecoder(), messages: [JSONObject] = []
                for byte in line.utf8 { messages.append(contentsOf: try decoder.feed(Data([byte]))) }
                try require(messages.count == 2, "coalesced response count")
                try require(messages[0].object("data").string("message") == "中文", "split UTF-8 preserved")
                try require(messages[1].string("id") == "2" && messages[1].bool("ok"), "request identity preserved")
            }),
            ("protocol_fail_closed", {
                var malformed = JSONLDecoder(), oversized = JSONLDecoder()
                do { _ = try malformed.feed(Data("ordinary log text\n".utf8)); throw Failure.assertion("malformed accepted") }
                catch DesktopProtocolError.malformed {}
                do { _ = try oversized.feed(Data(repeating: 65, count: 8_000_001)); throw Failure.assertion("oversized accepted") }
                catch DesktopProtocolError.oversized {}
            }),
            ("partial_frames_and_empty_lines", {
                var decoder = JSONLDecoder()
                let initial = try decoder.feed(Data("\n{\"id\":\"1\"".utf8))
                try require(initial.isEmpty, "partial frame emitted")
                let result = try decoder.feed(Data("}\n\n".utf8))
                try require(result.count == 1 && result[0].string("id") == "1", "empty lines handled")
            }),
            ("collected_is_not_manually_accepted", {
                let item = TaskItem(["id": "synthetic", "status": "collected_awaiting_review", "eligible": 30, "observations": 62, "reliable_identities": 12, "confirmed_identities": 0, "target": 30])
                try require(item.label == "采集完成 · 待核验", "unreviewed task mislabeled")
                try require(item.observations == 62 && item.eligible == 30 && item.confirmedIdentities == 0, "counts conflated")
                try require(item.reliableIdentities == 12, "reliable ID count conflated with readable or reviewed")
                try require(item.progress == 1 && !item.canResume, "completion action defaults")
            }),
            ("stale_running_task", {
                try require(TaskItem(["status": "running", "active": false]).label == "待检查运行状态", "stale state claims live process")
                try require(TaskItem(["status": "running", "active": true]).label == "采集中", "live state incorrect")
            }),
            ("safe_pause_retains_its_task_after_activity_finishes", {
                let activity = Activity(["id": "SYNTHETIC-batch", "kind": "batch", "phase": "exporting"])
                let finished = AppState(["busy": false, "activity": NSNull(), "tasks": [["id": "SYNTHETIC-batch", "kind": "batch", "status": "paused", "stop_reason": "operator_pause", "active": false]]])
                try require(completedPauseTask(previousActivity: activity, state: finished)?.id == "SYNTHETIC-batch", "paused result selection lost")
                try require(completedPauseTask(previousActivity: nil, state: finished) == nil, "startup auto-selected historical pause")
                let working = AppState(["busy": true, "tasks": [["id": "SYNTHETIC-batch", "kind": "batch", "status": "paused", "stop_reason": "operator_pause"]]])
                try require(completedPauseTask(previousActivity: activity, state: working) == nil, "pause shown before process ended")
                let wrong = Activity(["id": "SYNTHETIC-other", "kind": "batch"])
                try require(completedPauseTask(previousActivity: wrong, state: finished) == nil, "unrelated task selected")
                try require(stopReasonLabel("operator_pause").contains("安全暂停"), "normal pause exposed internal code")
            }),
            ("consecutive_read_check_is_explicit_and_does_not_mean_offline", {
                let item = TaskItem(["id": "SYNTHETIC", "status": "paused", "requires_read_check": true,
                                     "read_anomaly": ["consecutive_failures": 10, "consecutive_no_progress": 0],
                                     "can_resume": true, "target": 100, "eligible": 1])
                try require(item.requiresReadCheck && item.canResume && !item.requiresAck, "read recovery action lost")
                try require(item.consecutiveReadFailures == 10 && item.consecutiveNoProgress == 0, "separate streaks lost")
                try require(!TaskItem(["requires_read_retry": true]).requiresReadCheck, "legacy credit requirement accepted")
                let label = stopReasonLabel("DeviceError: capture: persistent retry budget exhausted")
                try require(label.contains("检查并继续") && !label.contains("未连接"), "read error shown as offline")
                try require(!label.contains("追加 3 次"), "legacy credit request displayed")
                try require(stopReasonLabel("consecutive_read_failures:10").contains("连续 10 次页面读取失败"), "read threshold lost")
                try require(stopReasonLabel("consecutive_no_progress:10").contains("未取得进展"), "progress threshold lost")
            }),
            ("raw_note_text_and_missing_values", {
                let record = NoteRecord(["id": "synthetic", "title": NSNull(), "author": "原作者", "body": "我不喜欢这个\n但这是原文。",
                                         "quality": "标题未能读取；正文完整性未自动判断", "eligible": false, "evidence_paths": [["label": "UI 树", "path": "/synthetic/note.xml"]]])
                try require(record.title == "" && record.body == "我不喜欢这个\n但这是原文。", "raw fields altered")
                try require(record.quality == "标题未能读取；正文完整性未自动判断" && !record.eligible, "quality lost")
                try require(record.evidence.first?.path == "/synthetic/note.xml", "evidence lost")
                try require(record.titlePlaceholder == "标题未能读取" && record.topicsLabel == "平台话题未能识别", "missing fields misstated")
            }),
            ("page_time_topics_and_identity_are_independent", {
                let record = NoteRecord(["id": "synthetic", "title_status": "not_displayed", "page_time": "编辑于 昨天 香港",
                                         "time_label": "编辑时间", "time_status_label": "已读取", "topics_status": "confirmed",
                                         "topics": ["SYNTHETIC 平台话题"], "identity_status": "verified", "note_id": "synthetic-note-123"])
                try require(record.pageTime == "编辑于 昨天 香港" && record.timeLabel == "编辑时间", "time raw or kind changed")
                try require(record.topicsLabel == "SYNTHETIC 平台话题", "confirmed topic missing")
                try require(record.identityLabel.contains("synthetic-note-123"), "verified identity lost")
                try require(record.titlePlaceholder == "页面未展示标题", "explicit absence conflated with failure")
                let unknown = NoteRecord(["topics": ["#SYNTHETIC 普通正文"], "topics_status": "unrecognized"])
                try require(unknown.topics.isEmpty && unknown.identityLabel.contains("身份未核验"), "unverified data promoted")
            }),
            ("nullable_state_and_batch_children", {
                let state = AppState(["initialized": true, "activity": NSNull(), "issue": NSNull(), "tasks": [["id": "synthetic-batch", "kind": "batch", "tasks": [["id": "synthetic-child"]]]]])
                try require(state.activity == nil && state.issue == nil, "null handling")
                try require(state.tasks.count == 1 && state.tasks[0].children[0].id == "synthetic-child", "nested items not preserved")
            }),
            ("persisted_cooldown_dates", {
                try require(parsedDate("2026-09-14T10:20:00+08:00") != nil, "offset date")
                try require(parsedDate("2026-09-14T02:20:00.123456Z") != nil, "microsecond date")
                try require(parsedDate(nil) == nil && parsedDate("not-a-date") == nil, "invalid date")
            }),
            ("keyword_and_target_validation", {
                let store = AppStore()
                store.keywords = " 香港城市大学 \n\n校园生活\r\n香港城市大学\n"
                try require(store.uniqueKeywords == ["香港城市大学", "校园生活"] && store.duplicateCount == 1, "keyword normalization")
                try require(store.target == 10 && store.validTarget, "default target")
                for invalid in ["0", "501", "", "十", "10.5", "-1"] {
                    store.targetText = invalid; try require(!store.validTarget, "invalid target accepted: " + invalid)
                }
                for valid in [100, 101, 500] {
                    store.targetText = " \(valid) "; try require(store.validTarget && store.target == valid, "valid numeric edit")
                }
            }),
            ("idle_checking_is_not_active_work", {
                var state = AppState(["initialized": true, "busy": false, "activity": NSNull(),
                                      "device": ["status": "checking", "checks": [["id": "automation", "status": "checking", "message": "正在连接"]]]])
                state.normalizeDeviceChecking(bridgeAlive: true)
                try require(!state.deviceCheckInProgress(bridgeAlive: true), "idle check stays active")
                try require(state.device.label == "检查未完成", "idle status not explained")
                try require(state.device.checks[0].status == "incomplete", "stale card preserved")
                try require(state.device.message.contains("重新检查"), "missing retry guidance")
                try require(!state.busy, "normalization invented a worker")
            }),
            ("active_check_survives_connection_ready", {
                var state = AppState(["initialized": true, "busy": true, "activity": ["phase": "preparing"],
                                      "device": ["status": "ready", "checks": [["id": "automation", "status": "checking", "message": "正在检查自动化服务"]]]])
                state.normalizeDeviceChecking(bridgeAlive: true)
                try require(state.deviceCheckInProgress(bridgeAlive: true), "service check hidden after ADB ready")
                try require(state.device.status == "ready", "known connection overwritten")
                try require(state.device.checks[0].status == "checking", "active check marked incomplete")
            }),
            ("cancel_and_disconnect_end_checking_display", {
                let json: JSONObject = ["initialized": true, "busy": true, "activity": ["phase": "pausing"],
                                        "device": ["status": "checking", "checks": [["id": "connection", "status": "checking"]]]]
                var pausing = AppState(json)
                pausing.normalizeDeviceChecking(bridgeAlive: true)
                try require(!pausing.deviceCheckInProgress(bridgeAlive: true), "cancel still claims active check")
                try require(pausing.device.message.contains("安全停止") && pausing.busy, "cancel invented completion")
                var disconnected = AppState(json)
                disconnected.normalizeDeviceChecking(bridgeAlive: false)
                try require(!disconnected.deviceCheckInProgress(bridgeAlive: false), "EOF left active check")
                try require(disconnected.device.checks[0].status == "incomplete", "EOF left checking card")
                try require(disconnected.device.checks[0].message.contains("控制连接已中断"), "EOF reason missing")
            }),
        ] + ConnectionSelfTests.cases + RequestReceiptSelfTests.cases
        var failures = 0
        for (name, test) in cases {
            do { try test(); print("PASS \(name)") }
            catch { failures += 1; print("FAIL \(name): \(error)") }
        }
        print("Native desktop self-test: \(cases.count - failures) passed, \(failures) failed (synthetic only).")
        return failures == 0 ? 0 : 1
    }
}
