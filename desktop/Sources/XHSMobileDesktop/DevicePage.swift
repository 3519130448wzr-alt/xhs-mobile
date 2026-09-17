import SwiftUI

struct DevicePage: View {
    @EnvironmentObject private var store: AppStore
    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            Panel {
                HStack(alignment: .center, spacing: 22) {
                    Image(systemName: "iphone.gen3.radiowaves.left.and.right").font(.system(size: 44, weight: .ultraLight)).foregroundStyle(Theme.accent)
                        .frame(width: 102, height: 112).background(Theme.accentSoft, in: RoundedRectangle(cornerRadius: 22))
                    VStack(alignment: .leading, spacing: 12) {
                        HStack { Text("云手机 \(store.state.device.id)").font(.system(size: 20, weight: .semibold)); StatusPill(text: store.state.device.label, status: store.state.device.status) }
                        Text(store.state.device.message).font(.system(size: 12)).foregroundStyle(.secondary).lineSpacing(4).fixedSize(horizontal: false, vertical: true)
                        if store.deviceCheckInProgress, let deadline = parsedDate(store.state.device.connectionDeadline) {
                            TimelineView(.periodic(from: .now, by: 1)) { context in
                                Text("本轮连接最多还需 \(max(0, Int(deadline.timeIntervalSince(context.date)))) 秒 · 可安全取消")
                                    .font(.system(size: 11)).foregroundStyle(.secondary)
                            }
                        }
                        HStack(spacing: 10) {
                            Button { store.perform("check") } label: { Label(store.deviceCheckInProgress ? "正在检查…" : "检查连接", systemImage: "arrow.triangle.2.circlepath") }
                                .buttonStyle(PrimaryButton()).disabled(store.state.busy || store.pendingAction || !store.bridgeAlive || store.terminating)
                                .accessibilityIdentifier("check_device")
                            if store.deviceCheckInProgress {
                                Button("取消连接") { store.perform("pause") }.buttonStyle(SoftButton()).disabled(store.pendingAction || store.terminating).accessibilityIdentifier("cancel_connection")
                            }
                            Button { store.openCloud() } label: { Label("打开云手机", systemImage: "arrow.up.right") }.buttonStyle(SoftButton()).accessibilityIdentifier("device_open_cloud")
                        }
                        HStack(spacing: 12) {
                            Button("自动连接设置") { store.showingConnectionSettings = true }.buttonStyle(SoftButton()).disabled(store.state.busy || store.pendingAction || store.terminating).accessibilityIdentifier("connection_settings")
                            Button("复制链接") { store.copyCloud() }.buttonStyle(.plain).font(.system(size: 11)).foregroundStyle(.secondary)
                        }
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            VStack(alignment: .leading, spacing: 15) {
                SectionLabel(title: "运行条件", subtitle: store.state.busy ? "设备正在工作。此处显示最近检查结果，刷新不会打断采集。" : "打开 App 或网络变化时自动准备连接；日常进度刷新不会操作手机。")
                LazyVGrid(columns: [GridItem(.flexible(), spacing: 18), GridItem(.flexible(), spacing: 18)], spacing: 18) {
                    ForEach(checks) { check in
                        Panel(padding: 20) {
                            HStack {
                                Image(systemName: symbol(check.id)).font(.system(size: 19, weight: .light)).foregroundStyle(.secondary)
                                Spacer()
                                Image(systemName: checkSymbol(check.status)).foregroundStyle(Theme.statusColor(check.status))
                            }
                            VStack(alignment: .leading, spacing: 9) {
                                Text(check.label).font(.system(size: 14, weight: .semibold))
                                Text(check.message.isEmpty ? "尚未检查" : check.message).font(.system(size: 12)).foregroundStyle(.secondary).lineSpacing(4).fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                }
            }
            Panel(padding: 22) {
                HStack(alignment: .top, spacing: 15) {
                    Image(systemName: "questionmark.bubble").font(.system(size: 22, weight: .light)).foregroundStyle(.secondary)
                    VStack(alignment: .leading, spacing: 12) {
                        Text("遇到连接或页面问题").font(.system(size: 14, weight: .semibold))
                        Text("打开云手机，在浏览器中处理连接、登录或页面提示。完成后回到这里检查连接，再到任务详情继续采集。")
                            .font(.system(size: 12)).foregroundStyle(.secondary).lineSpacing(5)
                        Text("本机数据库、保存或导出问题，请查看诊断详情。仅打开云手机链接不会解除暂停或冷却。")
                            .font(.system(size: 11)).foregroundStyle(.secondary).lineSpacing(4)
                        Button("打开诊断详情") { store.openLogs() }.buttonStyle(SoftButton())
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
    }
    private var checks: [DeviceCheck] {
        if !store.state.device.checks.isEmpty { return store.state.device.checks }
        return [
            DeviceCheck(["id": "adb", "label": "手机连接", "status": "unknown", "message": "等待检查设备连接"]),
            DeviceCheck(["id": "automation", "label": "自动化服务", "status": "unknown", "message": "等待检查页面读取与控制服务"]),
            DeviceCheck(["id": "app", "label": "小红书适配", "status": "unknown", "message": "等待核对 App 版本与页面规则"]),
            DeviceCheck(["id": "database", "label": "本机数据库", "status": "unknown", "message": "等待初始化本机存储"])
        ]
    }
    private func symbol(_ id: String) -> String {
        switch id {
        case "adb", "connection", "phone": return "cable.connector"
        case "automation", "uiautomator2": return "hand.tap"
        case "app", "profile", "app_version": return "app.badge.checkmark"
        case "database", "storage": return "externaldrive"
        default: return "checklist"
        }
    }
    private func checkSymbol(_ status: String) -> String {
        if ["ready", "ok", "passed", "healthy"].contains(status) { return "checkmark.circle.fill" }
        if ["offline", "attention", "incomplete", "failed", "error", "missing"].contains(status) { return "exclamationmark.circle.fill" }
        return "circle.dashed"
    }
}
