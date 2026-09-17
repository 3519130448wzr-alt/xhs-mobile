import Foundation

// Synthetic acknowledgements only: no bridge, process, device or UI is started.
enum RequestReceiptSelfTests {
    enum Failure: Error { case assertion(String) }
    static func require(_ value: @autoclosure () -> Bool, _ message: String) throws {
        if !value() { throw Failure.assertion(message) }
    }
    static let epoch = Date(timeIntervalSince1970: 1_000)
    static var cases: [(String, () throws -> Void)] { [
        ("late_matching_ack_resolves_only_its_timeout", {
            var tracker = RequestReceiptTracker()
            tracker.register(id: "synthetic-check", method: "check", now: epoch)
            tracker.register(id: "synthetic-detail", method: "detail", now: epoch)
            _ = tracker.expire(now: epoch.addingTimeInterval(46))
            let receipt = tracker.acknowledge(id: "synthetic-check", success: true)
            try require(receipt.wasLate && receipt.request?.method == "check", "late response lost identity")
            try require(receipt.resolvedTimeoutIDs == ["synthetic-check"], "unrelated timeout cleared")
            try require(tracker.timedOut["synthetic-detail"] != nil, "unrelated pending request discarded")
            try require(tracker.pending.isEmpty, "late receipt resent a request")
        }),
        ("new_successful_heartbeat_resolves_old_heartbeats_only", {
            var tracker = RequestReceiptTracker()
            tracker.register(id: "status-1", method: "status", now: epoch)
            tracker.register(id: "start-1", method: "start", now: epoch)
            _ = tracker.expire(now: epoch.addingTimeInterval(46))
            tracker.register(id: "status-2", method: "status", now: epoch.addingTimeInterval(47))
            let receipt = tracker.acknowledge(id: "status-2", success: true)
            try require(receipt.resolvedTimeoutIDs == ["status-1"], "heartbeat confirmed an action")
            try require(tracker.timedOut["start-1"] != nil && tracker.hasUnconfirmedActions, "start uncertainty lost")
            let own = tracker.acknowledge(id: "start-1", success: true)
            try require(own.resolvedTimeoutIDs == ["start-1"] && !tracker.hasUnconfirmedActions, "own start ACK did not release uncertainty")
            try require(tracker.pending.isEmpty, "start was replayed")
        }),
        ("late_failed_action_is_a_received_failure_not_success", {
            var tracker = RequestReceiptTracker()
            tracker.register(id: "resume-1", method: "resume", now: epoch)
            _ = tracker.expire(now: epoch.addingTimeInterval(46))
            try require(tracker.hasUnconfirmedActions, "resume timeout permits another resume")
            let receipt = tracker.acknowledge(id: "resume-1", success: false)
            try require(receipt.wasLate && receipt.request?.method == "resume" && receipt.resolvedTimeoutIDs == ["resume-1"], "late failure lost action attribution")
            try require(!tracker.hasUnconfirmedActions && tracker.pending.isEmpty, "failure was replayed or left unconfirmed")
            let incident = Incident(code: "bridge_timeout", title: "synthetic", message: "synthetic", requestID: "resume-1")
            try require(incident.requestID == "resume-1", "UI issue lost request ID")
        }),
        ("failed_or_old_heartbeat_cannot_clear_newer_timeout", {
            var tracker = RequestReceiptTracker()
            tracker.register(id: "old", method: "status", now: epoch)
            tracker.register(id: "new", method: "status", now: epoch.addingTimeInterval(2))
            _ = tracker.expire(now: epoch.addingTimeInterval(48))
            let old = tracker.acknowledge(id: "old", success: true)
            try require(old.resolvedTimeoutIDs == ["old"] && tracker.timedOut["new"] != nil, "old receipt clears newer timeout")
            tracker.register(id: "failure", method: "status", now: epoch.addingTimeInterval(49))
            let failed = tracker.acknowledge(id: "failure", success: false)
            try require(failed.resolvedTimeoutIDs.isEmpty && tracker.timedOut["new"] != nil, "failed heartbeat claimed recovery")
            let unknown = tracker.acknowledge(id: "unknown", success: true)
            try require(unknown.request == nil && unknown.resolvedTimeoutIDs.isEmpty, "unknown response clears errors")
        }),
        ("receipt_retains_dismissed_heartbeat_identity", {
            var tracker = RequestReceiptTracker()
            tracker.register(id: "visible-alert", method: "status", now: epoch)
            _ = tracker.expire(now: epoch.addingTimeInterval(46))
            tracker.register(id: "new-timeout", method: "status", now: epoch.addingTimeInterval(47))
            _ = tracker.expire(now: epoch.addingTimeInterval(93))
            tracker.register(id: "recovered", method: "status", now: epoch.addingTimeInterval(94))
            let resolved = tracker.acknowledge(id: "recovered", success: true).resolvedTimeoutIDs
            try require(resolved == ["visible-alert", "new-timeout"], "older alert can remain stale after heartbeat recovers")
            tracker.removeAll()
            try require(tracker.pending.isEmpty && tracker.timedOut.isEmpty, "disconnect left receipt state")
        })
    ] }
}
