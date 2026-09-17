import AppKit
import Foundation
import SwiftUI
import Network
import SystemConfiguration
import Darwin

struct ConnectionSettingsDraft {
    var enabled = true
    var authorizationMode = "prefix_list"
    var region = "cn-shanghai"
    var prefixListID = ""
    var securityGroupID = ""
    var accountID = ""
    var ownerMarker = ""
    var credentialRef = ""
    var hasSavedCredentials = false
    init(root: JSONObject, deviceID: String) {
        let value = root.object("auto_connection")
        enabled = value["enabled"] == nil || value.bool("enabled")
        authorizationMode = value.string("authorization_mode", "prefix_list")
        region = value.string("region", root.string("region", "cn-shanghai"))
        prefixListID = value.string("prefix_list_id")
        securityGroupID = value.string("security_group_id", root.object("network").string("security_group_id"))
        accountID = value.string("account_id")
        ownerMarker = value.string("owner_marker", "xhs-mobile-" + deviceID)
        credentialRef = value.string("credential_ref")
        hasSavedCredentials = value.bool("credentials_saved")
    }
    func metadata(reference: String) -> JSONObject {
        ["enabled": enabled, "authorization_mode": authorizationMode,
         "region": region.trimmingCharacters(in: .whitespacesAndNewlines),
         "prefix_list_id": prefixListID.trimmingCharacters(in: .whitespacesAndNewlines),
         "security_group_id": securityGroupID.trimmingCharacters(in: .whitespacesAndNewlines),
         "account_id": accountID.trimmingCharacters(in: .whitespacesAndNewlines),
         "owner_marker": ownerMarker.trimmingCharacters(in: .whitespacesAndNewlines), "credential_ref": reference]
    }
}

enum ConnectionSettingsPersistence {
    static func load(path: String) throws -> JSONObject {
        guard path.hasPrefix("/"), let root = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: path))) as? JSONObject else { throw ConnectionFailure.configuration }
        return root
    }
    static func save(path: String, draft: ConnectionSettingsDraft, accessKeyID: String, secret: String) throws {
        guard ["open", "prefix_list"].contains(draft.authorizationMode) else { throw ConnectionFailure.configuration }
        guard (draft.enabled && draft.authorizationMode == "prefix_list") || (accessKeyID.isEmpty && secret.isEmpty) else {
            throw ConnectionFailure(code: "configuration", message: "仅白名单自动连接模式可保存凭据。当前凭据输入尚未保存。")
        }
        var root = try load(path: path)
        var metadata = root.object("auto_connection")
        if draft.enabled && draft.authorizationMode == "prefix_list" {
            let replacing = !accessKeyID.isEmpty || !secret.isEmpty
            let reference = replacing ? "xhs-mobile-" + UUID().uuidString : draft.credentialRef
            let replacement = draft.metadata(reference: reference)
            let config = try AutoConnectionConfiguration(replacement)
            if replacing { try ConnectionKeychain.save(id: accessKeyID, secret: secret, config: config) }
            else { _ = try ConnectionKeychain.load(config, interactive: true) }
            metadata.merge(replacement) { _, new in new }
            metadata["helper_path"] = Bundle.main.executableURL?.path ?? CommandLine.arguments[0]
            metadata["credentials_saved"] = true
        } else {
            // Open mode does not validate cloud metadata or access the Keychain.
            // Keep previous prefix-list settings intact for a later rollback.
            metadata["enabled"] = draft.enabled
            metadata["authorization_mode"] = draft.authorizationMode
        }
        root["auto_connection"] = metadata
        try atomicPrivateJSON(root, path: path)
    }
    static func atomicPrivateJSON(_ root: JSONObject, path: String) throws {
        let data = try JSONSerialization.data(withJSONObject: root, options: [.prettyPrinted, .sortedKeys])
        var template = Array((path + ".XXXXXX").utf8CString)
        let descriptor = mkstemp(&template)
        guard descriptor >= 0 else { throw ConnectionFailure.configuration }
        let temp = String(cString: template)
        defer { close(descriptor); unlink(temp) }
        guard fchmod(descriptor, 0o600) == 0 else { throw ConnectionFailure.configuration }
        try data.withUnsafeBytes { bytes in
            var offset = 0
            while offset < data.count {
                let count = Darwin.write(descriptor, bytes.baseAddress!.advanced(by: offset), data.count - offset)
                if count < 0 { if errno == EINTR { continue }; throw ConnectionFailure.configuration }
                offset += count
            }
        }
        guard fsync(descriptor) == 0, rename(temp, path) == 0 else { throw ConnectionFailure.configuration }
        let parent = URL(fileURLWithPath: path).deletingLastPathComponent().path
        let directory = open(parent, O_RDONLY); if directory >= 0 { _ = fsync(directory); close(directory) }
    }
}

struct ConnectionSettingsView: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss
    @State private var draft = ConnectionSettingsDraft(root: [:], deviceID: "lab01")
    @State private var accessKeyID = ""
    @State private var secret = ""
    @State private var saving = false
    @State private var message: String?
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack {
                Image(systemName: "network.badge.shield.half.filled").font(.system(size: 26)).foregroundStyle(Theme.accent)
                VStack(alignment: .leading, spacing: 5) {
                    Text("自动连接设置").font(.system(size: 21, weight: .semibold))
                    Text("由本机自动选择可用网络并连接云手机。").font(.system(size: 12)).foregroundStyle(.secondary)
                }
                Spacer()
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    Toggle("自动识别网络并连接云手机", isOn: $draft.enabled)
                    Picker("连接方式", selection: $draft.authorizationMode) {
                        Text("开放连接").tag("open")
                        Text("自动白名单").tag("prefix_list")
                    }.pickerStyle(.segmented).accessibilityIdentifier("connection_authorization_mode")
                    Text(draft.authorizationMode == "open"
                         ? "开放连接，无需地址授权。云手机开放来源连接后，本机直接连接，无需填写 RAM 凭据；原有白名单设置仍会保留。设置完成不会自动开始或继续采集。"
                         : "需要先在阿里云准备专用 IPv4 地址名单（容量 2），仅关联当前安全组的 TCP 5555 规则，以及只允许管理该名单的 RAM 凭据。设置完成不会自动开始或继续采集。")
                        .font(.system(size: 12)).foregroundStyle(.secondary).lineSpacing(4)
                    HStack {
                        Button("查看首次配置说明") { store.openConnectionGuide() }.buttonStyle(SoftButton())
                        Button("打开云手机") { store.openCloud() }.buttonStyle(SoftButton())
                    }
                    if draft.authorizationMode == "prefix_list" {
                      Grid(alignment: .leading, horizontalSpacing: 18, verticalSpacing: 12) {
                        settingField("地域", text: $draft.region, hint: "cn-shanghai")
                        settingField("阿里云账号 ID", text: $draft.accountID, hint: "资源所属主账号数字 ID")
                        settingField("前缀列表 ID", text: $draft.prefixListID, hint: "pl-…")
                        settingField("名单名称", text: $draft.ownerMarker, hint: "xhs-mobile-lab01")
                        settingField("安全组 ID", text: $draft.securityGroupID, hint: "sg-…")
                    }.disabled(!draft.enabled)
                    Divider()
                    Text(draft.hasSavedCredentials ? "凭据已配置 · 留空可继续使用；修改资源范围需重新录入。" : "专用 RAM 凭据 · 只保存在这台 Mac 的钥匙串")
                        .font(.system(size: 12, weight: .medium))
                    if !draft.enabled {
                        Text("先开启上方的自动连接开关，即可填写并保存凭据。").font(.system(size: 12)).foregroundStyle(.secondary)
                    }
                    SecureField("AccessKey ID", text: $accessKeyID).textFieldStyle(.roundedBorder).disabled(!draft.enabled || saving).accessibilityIdentifier("connection_access_key_id")
                    SecureField("AccessKey Secret", text: $secret).textFieldStyle(.roundedBorder).disabled(!draft.enabled || saving).accessibilityIdentifier("connection_access_key_secret")
                    Text("请在这里录入密钥，无需发给聊天助手。应用升级或钥匙串锁定时，可能需要一次系统授权。")
                        .font(.system(size: 11)).foregroundStyle(.secondary)
                    }
                    Text("默认先尝试 VPN／系统路线，失败后仅将采集连接改走物理网络。不会关闭 VPN 或修改全局路由。")
                        .font(.system(size: 11)).foregroundStyle(.secondary).lineSpacing(4)
                }.padding(.trailing, 5)
            }
            if let message { Text(message).font(.system(size: 12)).foregroundStyle(Theme.accent).fixedSize(horizontal: false, vertical: true) }
            HStack {
                if saving { ProgressView().controlSize(.small); Text(draft.enabled && draft.authorizationMode == "prefix_list" ? "正在保存到钥匙串…" : "正在保存设置…").font(.system(size: 12)).foregroundStyle(.secondary) }
                Spacer()
                Button("取消") { clearSecrets(); dismiss() }.buttonStyle(SoftButton()).disabled(saving)
                Button(draft.enabled ? "保存并检查连接" : "保存设置") { save() }.buttonStyle(PrimaryButton()).disabled(saving || store.state.busy || store.pendingAction || store.terminating)
                    .accessibilityIdentifier("save_connection_settings")
            }
        }.padding(28).frame(width: 610, height: 675).interactiveDismissDisabled(saving)
            .onAppear { load() }.onDisappear { clearSecrets() }
            .onChange(of: draft.enabled) { _, enabled in
                if !enabled && (!accessKeyID.isEmpty || !secret.isEmpty) {
                    clearSecrets(); message = "已清除尚未保存的凭据输入，钥匙串中原有凭据保留。"
                }
            }
            .onChange(of: draft.authorizationMode) { _, mode in
                if mode == "open" { clearSecrets() }
            }
    }
    @ViewBuilder private func settingField(_ label: String, text: Binding<String>, hint: String) -> some View {
        GridRow {
            Text(label).font(.system(size: 12)).foregroundStyle(.secondary)
            TextField(hint, text: text).textFieldStyle(.roundedBorder).frame(minWidth: 310)
        }
    }
    private func clearSecrets() { accessKeyID = ""; secret = "" }
    private func load() {
        guard !store.preview else { draft = ConnectionSettingsDraft(root: [:], deviceID: "synthetic"); return }
        do { draft = ConnectionSettingsDraft(root: try ConnectionSettingsPersistence.load(path: store.connectionRecordPath), deviceID: store.state.device.id) }
        catch { message = "找不到设备连接配置，请先检查本机安装与诊断详情。" }
    }
    private func save() {
        guard !store.preview else { message = "这是 synthetic 界面预览，不会保存凭据或修改云端。"; clearSecrets(); return }
        saving = true; message = nil
        let path = store.connectionRecordPath, copy = draft, id = accessKeyID, value = secret
        clearSecrets()
        Task {
            let result: Result<Void, Error> = await Task.detached {
                Result { try ConnectionSettingsPersistence.save(path: path, draft: copy, accessKeyID: id, secret: value) }
            }.value
            saving = false
            switch result {
            case .success:
                store.notice = copy.enabled ? "连接设置已保存。正在检查连接，采集任务不会自动启动。" : "自动连接已关闭，已有任务和已保存的凭据会保留。"
                dismiss()
                if copy.enabled { store.perform("check") }
            case let .failure(error): message = (error as? ConnectionFailure)?.message ?? "连接设置未保存，请核对配置和钥匙串授权后重试。"
            }
        }
    }
}

// NWPath changes are coalesced. Startup preparation and busy-state policy live in
// the Python controller; this monitor only reports a change and never touches ADB.
final class ConnectionNetworkMonitor {
    private let monitor = NWPathMonitor()
    private let queue = DispatchQueue(label: "com.xhs.mobile.connection.network")
    private let queueKey = DispatchSpecificKey<Bool>()
    private var receivedPath = false
    init() { queue.setSpecific(key: queueKey, value: true) }
    private var pending: DispatchWorkItem?
    private var store: SCDynamicStore?
    private var changed: (@Sendable () -> Void)?
    func start(changed: @escaping @Sendable () -> Void) {
        self.changed = changed
        monitor.pathUpdateHandler = { [weak self] _ in
            guard let self else { return }
            if self.receivedPath { self.schedule() }; self.receivedPath = true
        }
        monitor.start(queue: queue)
        var context = SCDynamicStoreContext(version: 0, info: Unmanaged.passUnretained(self).toOpaque(), retain: { pointer in
            _ = Unmanaged<ConnectionNetworkMonitor>.fromOpaque(pointer).retain(); return pointer
        }, release: { pointer in
            Unmanaged<ConnectionNetworkMonitor>.fromOpaque(pointer).release()
        }, copyDescription: nil)
        store = SCDynamicStoreCreate(nil, "xhs-mobile-network" as CFString, { _, _, context in
            guard let context else { return }
            Unmanaged<ConnectionNetworkMonitor>.fromOpaque(context).takeUnretainedValue().schedule()
        }, &context)
        if let store {
            let patterns = ["State:/Network/Interface/.*/IPv4", "State:/Network/Interface/.*/IPv6",
                            "State:/Network/Service/.*/IPv4", "State:/Network/Service/.*/IPv6",
                            "State:/Network/Global/IPv4", "State:/Network/Global/IPv6"]
            SCDynamicStoreSetNotificationKeys(store, nil, patterns as CFArray)
            SCDynamicStoreSetDispatchQueue(store, queue)
        }
    }
    private func schedule() {
        pending?.cancel()
        guard let changed else { return }
        let work = DispatchWorkItem(block: changed); pending = work
        queue.asyncAfter(deadline: .now() + 3, execute: work)
    }
    func cancel() {
        if let store { SCDynamicStoreSetDispatchQueue(store, nil) }
        store = nil; monitor.cancel()
        let cleanup = { self.pending?.cancel(); self.pending = nil; self.changed = nil }
        if DispatchQueue.getSpecific(key: queueKey) == true { cleanup() } else { queue.sync(execute: cleanup) }
    }
    deinit { cancel() }
}
