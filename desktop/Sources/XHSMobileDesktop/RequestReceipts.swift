import Foundation

// Receipt bookkeeping does not resend messages. Timed-out actions retain their
// identity until their own acknowledgement arrives; a heartbeat cannot confirm them.
struct RequestReceiptTracker {
    struct Request: Equatable {
        let id: String
        let method: String
        let sentAt: Date
    }
    struct Receipt {
        let request: Request?
        let wasLate: Bool
        let resolvedTimeoutIDs: Set<String>
    }
    private(set) var pending: [String: Request] = [:]
    private(set) var timedOut: [String: Request] = [:]
    var hasUnconfirmedActions: Bool { timedOut.values.contains { ["start", "resume"].contains($0.method) } }
    mutating func register(id: String, method: String, now: Date = Date()) {
        pending[id] = Request(id: id, method: method, sentAt: now)
    }
    mutating func discard(id: String) { pending.removeValue(forKey: id); timedOut.removeValue(forKey: id) }
    mutating func removeAll() { pending.removeAll(); timedOut.removeAll() }
    mutating func expire(now: Date = Date(), after seconds: TimeInterval = 45) -> [Request] {
        let expired = pending.values.filter { now.timeIntervalSince($0.sentAt) > seconds }.sorted { $0.sentAt < $1.sentAt }
        for request in expired {
            pending.removeValue(forKey: request.id)
            timedOut[request.id] = request
        }
        return expired
    }
    mutating func acknowledge(id: String, success: Bool) -> Receipt {
        let late = timedOut.removeValue(forKey: id)
        let request = pending.removeValue(forKey: id) ?? late
        var resolved: Set<String> = late == nil ? [] : [id]
        if success, let request, request.method == "status" {
            for (oldID, old) in timedOut where old.method == "status" && old.sentAt <= request.sentAt {
                resolved.insert(oldID); timedOut.removeValue(forKey: oldID)
            }
        }
        return Receipt(request: request, wasLate: late != nil, resolvedTimeoutIDs: resolved)
    }
}
