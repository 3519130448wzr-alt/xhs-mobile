import SwiftUI

enum Theme {
    static let accent = Color(red: 0.91, green: 0.29, blue: 0.29)
    static let accentSoft = accent.opacity(0.09)
    static let green = Color(red: 0.17, green: 0.57, blue: 0.43)
    static let amber = Color(red: 0.74, green: 0.45, blue: 0.11)
    static let canvas = Color(nsColor: .windowBackgroundColor)
    static let card = Color(nsColor: .controlBackgroundColor)
    static let stroke = Color.primary.opacity(0.07)
    static func statusColor(_ status: String) -> Color {
        if ["ready", "ok", "passed", "healthy", "collected_awaiting_review", "completed"].contains(status) { return green }
        if ["running", "checking", "busy", "pausing"].contains(status) { return .blue }
        if ["offline", "attention", "incomplete", "needs_attention", "failed", "error", "missing", "partial", "cooldown", "cooling_down"].contains(status) { return amber }
        return .secondary
    }
}

struct Panel<Content: View>: View {
    var padding: CGFloat = 24
    @ViewBuilder var content: Content
    var body: some View {
        VStack(alignment: .leading, spacing: 18) { content }
            .padding(padding).frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.card, in: RoundedRectangle(cornerRadius: 18))
            .overlay(RoundedRectangle(cornerRadius: 18).stroke(Theme.stroke, lineWidth: 1))
            .shadow(color: .black.opacity(0.025), radius: 12, y: 5)
    }
}

struct StatusPill: View {
    let text: String
    let status: String
    var body: some View {
        HStack(spacing: 6) {
            Circle().fill(Theme.statusColor(status)).frame(width: 6, height: 6)
            Text(text).font(.system(size: 11, weight: .medium))
        }.foregroundStyle(Theme.statusColor(status))
            .padding(.horizontal, 9).padding(.vertical, 6)
            .background(Theme.statusColor(status).opacity(0.09), in: Capsule())
    }
}

struct PrimaryButton: ButtonStyle {
    @Environment(\.isEnabled) private var enabled
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.font(.system(size: 13, weight: .semibold)).foregroundStyle(.white)
            .padding(.horizontal, 18).padding(.vertical, 12)
            .background(enabled ? Theme.accent.opacity(configuration.isPressed ? 0.82 : 1) : Color.secondary.opacity(0.26), in: RoundedRectangle(cornerRadius: 10))
            .contentShape(RoundedRectangle(cornerRadius: 10))
    }
}

struct SoftButton: ButtonStyle {
    @Environment(\.isEnabled) private var enabled
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.font(.system(size: 12, weight: .medium))
            .foregroundStyle(enabled ? Color.primary : Color.secondary.opacity(0.5))
            .padding(.horizontal, 12).padding(.vertical, 9)
            .background(Color.primary.opacity(configuration.isPressed ? 0.09 : 0.045), in: RoundedRectangle(cornerRadius: 8))
            .contentShape(RoundedRectangle(cornerRadius: 8))
    }
}

struct SectionLabel: View {
    let title: String
    let subtitle: String
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.system(size: 17, weight: .semibold))
            if !subtitle.isEmpty { Text(subtitle).font(.system(size: 12)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true).lineSpacing(3) }
        }
    }
}

struct MetricView: View {
    let value: String
    let label: String
    var accent = false
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(value).font(.system(size: 27, weight: .semibold, design: .rounded)).monospacedDigit().foregroundStyle(accent ? Theme.accent : .primary)
            Text(label).font(.system(size: 11)).foregroundStyle(.secondary)
        }.frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct EmptyState: View {
    let icon: String
    let title: String
    let message: String
    var body: some View {
        VStack(spacing: 14) {
            Image(systemName: icon).font(.system(size: 31, weight: .light)).foregroundStyle(Theme.accent)
                .frame(width: 76, height: 76).background(Theme.accentSoft, in: RoundedRectangle(cornerRadius: 22))
            Text(title).font(.system(size: 18, weight: .semibold))
            Text(message).font(.system(size: 13)).foregroundStyle(.secondary).multilineTextAlignment(.center).lineSpacing(5)
        }.frame(maxWidth: .infinity).padding(.vertical, 58)
    }
}

struct CooldownView: View {
    let until: String
    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { context in
            let remaining = max(0, Int((parsedDate(until) ?? context.date).timeIntervalSince(context.date)))
            Label(remaining > 0 ? String(format: "冷却剩余 %02d:%02d · 保留现有进度", remaining / 60, remaining % 60) : "冷却时间已到，继续时将复查页面", systemImage: "clock")
                .font(.system(size: 12)).foregroundStyle(Theme.amber).monospacedDigit()
        }
    }
}

func stopReasonLabel(_ value: String) -> String {
    if value.hasPrefix("consecutive_read_failures:") {
        return "连续 10 次页面读取失败，已安全暂停。请检查手机页面，再选择“检查并继续”；已有成果和其他预算保留。"
    }
    if value.hasPrefix("consecutive_no_progress:") {
        return "连续 10 次页面操作未取得进展，已安全暂停。请检查手机页面，再选择“检查并继续”。"
    }
    if value.contains("capture: persistent retry budget exhausted") {
        return "此任务曾被旧版累计读取限制暂停。现已取消累计限制，可选择“检查并继续”；已有成果和其他预算保留。"
    }
    if value.hasPrefix("read_retry_check:") {
        return "读取恢复检查未通过，请查看诊断详情；已有成果保留。"
    }
    return ["target_collected": "已达到本次采集目标，等待人工核验。", "pause_requested": "已按请求保存进度并暂停。", "operator_pause": "已按请求安全暂停，已保存记录保留，可继续采集。", "interrupted": "采集已中断，已提交的记录仍然保留。",
     "detail_budget_exhausted": "本次详情访问预算已用完。", "swipe_budget_exhausted": "本次滚动预算已用完。", "no_progress": "连续滚动未发现新增候选，本次尝试结束。",
     "manual_resolution_required": "需要先在云手机上处理，再检查并继续。", "rate_limited": "正在按原有策略冷却，稍后复查页面。",
     "cooldown_active": "冷却尚未结束，现有进度已保存。", "device_disconnected": "设备连接中断，请先检查云手机。",
     "unknown_page": "遇到尚未识别的页面，需要查看手机与来源证据。", "budget_exhausted": "本次运行预算已用完，已有结果可以导出。" ][value] ?? value
}

func taskLabel(_ item: TaskItem, bridgeAlive: Bool) -> String {
    if !bridgeAlive && (item.active || item.status == "running") { return "控制连接中断 · 待核对" }
    return item.label
}
