import SwiftUI

struct ResultsPage: View {
    @EnvironmentObject private var store: AppStore
    var body: some View {
        VStack(spacing: 20) {
            if store.bridgeAlive, let activity = store.state.activity { ActivityPanel(activity: activity) }
            if store.state.tasks.isEmpty {
                Panel {
                    EmptyState(icon: "tray", title: "从第一份采集开始", message: "完成采集后，任务、笔记原文和来源证据会集中显示在这里。")
                    HStack { Spacer(); Button("新建采集") { store.page = .collect }.buttonStyle(PrimaryButton()); Spacer() }.padding(.bottom, 20)
                }
            } else {
                HStack(alignment: .top, spacing: 20) {
                    VStack(alignment: .leading, spacing: 12) {
                        HStack {
                            Text("全部任务").font(.system(size: 12, weight: .semibold))
                            Text("\(store.state.tasks.count)").font(.system(size: 10)).foregroundStyle(.secondary)
                            Spacer()
                            Button { store.refresh() } label: { Image(systemName: "arrow.clockwise") }.buttonStyle(.plain).foregroundStyle(.secondary).help("刷新任务列表")
                        }.padding(.horizontal, 4).padding(.bottom, 2)
                        ForEach(store.state.tasks) { item in
                            Button { store.select(item) } label: { taskRow(item) }.buttonStyle(.plain)
                        }
                    }.frame(width: 235)
                    if let selected = store.selectedTask { TaskDetailPanel(item: selected).frame(maxWidth: .infinity) }
                    else { Panel { EmptyState(icon: "doc.text.magnifyingglass", title: "选择一份任务", message: "查看进度、预览笔记，或继续未完成的采集。") }.frame(maxWidth: .infinity) }
                }
            }
        }
        .onAppear { if store.selectedTaskID == nil, let first = store.state.tasks.first { store.select(first) } }
    }
    private func taskRow(_ item: TaskItem) -> some View {
        VStack(alignment: .leading, spacing: 13) {
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: item.kind == "batch" ? "square.stack.3d.up" : "doc.text").font(.system(size: 12)).foregroundStyle(store.selectedTaskID == item.id ? Theme.accent : .secondary).padding(.top, 1)
                Text(item.title).font(.system(size: 12, weight: .medium)).lineLimit(2).multilineTextAlignment(.leading)
                Spacer(minLength: 0)
            }
            StatusPill(text: taskLabel(item, bridgeAlive: store.bridgeAlive), status: store.bridgeAlive ? item.status : "unknown")
            HStack {
                Text("\(item.eligible) / \(item.target) 条基础字段可读").font(.system(size: 10)).foregroundStyle(.secondary)
                Spacer()
                Text(shortDate(item.createdAt)).font(.system(size: 9)).foregroundStyle(.tertiary)
            }
            ProgressView(value: item.progress).tint(store.selectedTaskID == item.id ? Theme.accent : .secondary)
        }.padding(15).frame(maxWidth: .infinity, alignment: .leading)
            .background(store.selectedTaskID == item.id ? Theme.accentSoft : Theme.card, in: RoundedRectangle(cornerRadius: 13))
            .overlay(RoundedRectangle(cornerRadius: 13).stroke(store.selectedTaskID == item.id ? Theme.accent.opacity(0.26) : Theme.stroke, lineWidth: 1))
    }
}

struct TaskDetailPanel: View {
    @EnvironmentObject private var store: AppStore
    let item: TaskItem
    @State private var showingResumeConfirmation = false
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Panel(padding: 22) {
                VStack(alignment: .leading, spacing: 9) {
                    Text(item.kind == "batch" ? "关键词采集" : "笔记采集").font(.system(size: 11, weight: .medium)).foregroundStyle(.secondary)
                    Text(item.title).font(.system(size: 19, weight: .semibold)).textSelection(.enabled)
                    HStack { StatusPill(text: taskLabel(item, bridgeAlive: store.bridgeAlive), status: store.bridgeAlive ? item.status : "unknown"); Spacer(); Text(shortDate(item.createdAt)).font(.system(size: 11)).foregroundStyle(.secondary) }
                }
                LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], alignment: .leading, spacing: 16) {
                    MetricView(value: "\(item.observations)", label: "已保存记录")
                    MetricView(value: "\(item.eligible)", label: "基础字段可读", accent: true)
                    MetricView(value: "\(item.reliableIdentities)", label: "可靠 ID 不同笔记")
                    MetricView(value: "\(item.confirmedIdentities)", label: "人工确认不同笔记")
                }
                Text("标题、作者和正文可读即计入目标；正文完整性由人工核对。缺少可靠 ID 的记录可能重复，仍计入可读数量。")
                    .font(.system(size: 11)).foregroundStyle(.secondary).lineSpacing(3)
                if let until = item.cooldownUntil { CooldownView(until: until) }
                if !item.stopReason.isEmpty {
                    Text(stopReasonLabel(item.stopReason)).font(.system(size: 11)).foregroundStyle(.secondary).lineSpacing(4).textSelection(.enabled)
                }
                actions
                if !store.failureEvidence.isEmpty {
                    VStack(alignment: .leading, spacing: 9) {
                        Text("最近故障证据").font(.system(size: 11, weight: .medium))
                        ForEach(store.failureEvidence) { link in
                            Button { store.openPath(link.path) } label: { Label(link.label, systemImage: "doc.viewfinder") }.buttonStyle(SoftButton())
                        }
                    }
                }
                if let path = item.exportPath {
                    HStack(spacing: 8) {
                        Image(systemName: "checkmark.circle.fill").foregroundStyle(Theme.green)
                        Text("结果已导出").font(.system(size: 11)).foregroundStyle(.secondary)
                        Spacer()
                        Button("打开结果文件夹") { store.openPath(path) }.buttonStyle(.plain).font(.system(size: 11)).foregroundStyle(Theme.accent)
                    }.padding(12).background(Theme.green.opacity(0.06), in: RoundedRectangle(cornerRadius: 9))
                }
                if !item.children.isEmpty {
                    DisclosureGroup("每个关键词的进度（\(item.children.count)）") {
                        VStack(spacing: 12) {
                            ForEach(item.children) { child in
                                VStack(alignment: .leading, spacing: 8) {
                                    HStack {
                                        Text(child.title).font(.system(size: 11, weight: .medium)).lineLimit(2)
                                        Spacer()
                                        Text("\(child.eligible) / \(child.target)").font(.system(size: 11)).monospacedDigit().foregroundStyle(.secondary)
                                    }
                                    ProgressView(value: child.progress).tint(Theme.accent)
                                    Text(taskLabel(child, bridgeAlive: store.bridgeAlive)).font(.system(size: 11)).foregroundStyle(.secondary)
                                }
                            }
                        }.padding(.top, 12)
                    }.font(.system(size: 11)).tint(.secondary)
                }
            }
            Panel(padding: 22) {
                HStack {
                    SectionLabel(title: "笔记原文", subtitle: "共 \(store.recordTotal) 条观察记录 · 保留原文与质量标记")
                    Spacer()
                    Button { store.loadRecords() } label: { Image(systemName: "arrow.clockwise") }.buttonStyle(SoftButton()).disabled(store.detailLoading).help("刷新笔记预览")
                }
                if store.detailLoading && store.records.isEmpty { ProgressView("正在读取已保存记录…").font(.system(size: 11)).frame(maxWidth: .infinity).padding(.vertical, 25) }
                else if store.records.isEmpty {
                    Text("当前尚无已保存的记录。采集中的记录将在提交后显示。")
                        .font(.system(size: 12)).foregroundStyle(.secondary).padding(.vertical, 22)
                }
                else {
                    LazyVStack(alignment: .leading, spacing: 20) {
                        ForEach(store.records) { record in NoteRecordView(record: record); if record.id != store.records.last?.id { Divider() } }
                    }
                    if store.nextOffset != nil { Button(store.detailLoading ? "正在加载…" : "加载更多记录") { store.loadRecords(more: true) }.buttonStyle(SoftButton()).disabled(store.detailLoading) }
                }
            }
        }
        .confirmationDialog("确认手机上的问题已处理？", isPresented: $showingResumeConfirmation, titleVisibility: .visible) {
            Button("已处理，检查并继续") { store.resume(item) }
            Button("打开云手机") { store.openCloud() }
            Button("取消", role: .cancel) {}
        } message: { Text("程序会重新检查页面；验证码、登录和冷却状态仍按现有策略处理。打开云手机链接不会自动解除限制。") }
    }

    private var actions: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 9) { actionButtons }
            VStack(alignment: .leading, spacing: 9) { actionButtons }
        }
    }

    @ViewBuilder private var actionButtons: some View {
        if item.active {
            Button(store.state.activity?.phase == "pausing" ? "正在暂停…" : "暂停采集") { store.perform("pause", params: item.parameters) }
                .buttonStyle(PrimaryButton()).disabled(!store.bridgeAlive || store.pendingAction || store.state.activity?.phase == "pausing")
        } else if item.canResume {
            Button(item.requiresAck ? "处理后检查并继续" : (item.requiresReadCheck ? "检查并继续" : "继续采集")) {
                if item.requiresAck { showingResumeConfirmation = true } else { store.resume(item) }
            }.buttonStyle(PrimaryButton()).disabled(!store.canStart || coolingDown || store.state.issue?.code == "app_version")
        }
        Button { store.perform("export", params: item.parameters) } label: { Label(item.exportPath == nil ? "导出结果" : "重新导出", systemImage: "square.and.arrow.up") }
            .buttonStyle(SoftButton()).disabled(store.pendingAction || store.terminating || !store.bridgeAlive)
    }
    private var coolingDown: Bool { if let date = parsedDate(item.cooldownUntil) { return date > Date() }; return false }
}

struct NoteRecordView: View {
    @EnvironmentObject private var store: AppStore
    let record: NoteRecord
    @State private var expanded = true
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top) {
                Text(record.title.isEmpty ? record.titlePlaceholder : record.title).font(.system(size: 14, weight: .semibold)).textSelection(.enabled)
                Spacer(minLength: 8)
                Image(systemName: record.eligible ? "checkmark.seal" : "exclamationmark.circle").foregroundStyle(record.eligible ? Theme.green : Theme.amber).help(record.eligible ? "基础字段可读，正文完整性由人工核对" : "请核对具体缺失字段")
            }
            Label(record.author.isEmpty ? "作者未读取" : record.author, systemImage: "person.crop.circle").font(.system(size: 11)).foregroundStyle(.secondary).textSelection(.enabled)
            Label("\(record.timeLabel)：\(record.pageTime.isEmpty ? record.timeStatusLabel : record.pageTime + " · " + record.timeStatusLabel)", systemImage: "calendar")
                .font(.system(size: 11)).foregroundStyle(.secondary).textSelection(.enabled)
            Label(record.topicsLabel, systemImage: "number").font(.system(size: 11)).foregroundStyle(.secondary).textSelection(.enabled)
            Text(record.body.isEmpty ? "正文未读取，请核对质量标记与来源证据。" : record.body)
                .font(.system(size: 12)).lineSpacing(6).foregroundStyle(record.body.isEmpty ? .secondary : .primary).lineLimit(expanded ? nil : 6).textSelection(.enabled)
            if record.body.count > 180 || record.body.components(separatedBy: .newlines).count > 5 {
                Button(expanded ? "收起正文" : "展开已保存正文") { expanded.toggle() }.buttonStyle(.plain).font(.system(size: 11)).foregroundStyle(Theme.accent)
            }
            Text(record.identityLabel).font(.system(size: 10)).foregroundStyle(.secondary).textSelection(.enabled)
            if !record.quality.isEmpty {
                Text(record.quality).font(.system(size: 11)).foregroundStyle(.secondary).lineSpacing(3).textSelection(.enabled)
                    .padding(10).frame(maxWidth: .infinity, alignment: .leading).background(Theme.canvas, in: RoundedRectangle(cornerRadius: 8))
            }
            if !record.evidence.isEmpty {
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 7) { evidenceButtons }
                    VStack(alignment: .leading, spacing: 7) { evidenceButtons }
                }
            }
        }
    }
    @ViewBuilder private var evidenceButtons: some View {
        ForEach(record.evidence) { link in Button { store.openPath(link.path) } label: { Label(link.label, systemImage: link.path.lowercased().hasSuffix(".xml") ? "chevron.left.forwardslash.chevron.right" : "photo") }.buttonStyle(SoftButton()) }
    }
}
