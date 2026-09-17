import AppKit
import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var store: AppStore
    var body: some View {
        HStack(spacing: 0) {
            sidebar.frame(width: 216)
            Rectangle().fill(Theme.stroke).frame(width: 1)
            VStack(spacing: 0) {
                header
                if let issue = store.issue { issueBanner(issue).padding(.horizontal, 30).padding(.bottom, 18) }
                if let notice = store.notice {
                    HStack {
                        Image(systemName: "info.circle").foregroundStyle(.secondary)
                        Text(notice).font(.system(size: 12))
                        Spacer()
                        Button { store.notice = nil } label: { Image(systemName: "xmark") }.buttonStyle(.plain).accessibilityLabel("关闭提示")
                    }.padding(12).background(Color.blue.opacity(0.065), in: RoundedRectangle(cornerRadius: 10)).padding(.horizontal, 30).padding(.bottom, 16)
                }
                ScrollView {
                    Group {
                        switch store.page {
                        case .collect: CollectPage()
                        case .results: ResultsPage()
                        case .device: DevicePage()
                        }
                    }.padding(.horizontal, 30).padding(.bottom, 30)
                }
                footer
            }.frame(maxWidth: .infinity, maxHeight: .infinity).background(Theme.canvas)
        }
        .tint(Theme.accent)
        .alert(store.alertIssue?.title ?? "需要处理", isPresented: $store.showingIssue) {
            if store.alertIssue?.cloudAction == true { Button("打开云手机") { store.openCloud() } }
            if store.alertIssue?.canRetry == true { Button("重新检查") { store.perform("check") }.disabled(store.state.busy || store.pendingAction) }
            if store.alertIssue?.connectionSettingsAction == true { Button("连接设置") { store.showingConnectionSettings = true }.disabled(store.state.busy || store.pendingAction) }
            Button("稍后处理", role: .cancel) {}
        } message: { Text(store.alertIssue?.message ?? "") }
        .sheet(isPresented: $store.showingConnectionSettings) { ConnectionSettingsView().environmentObject(store) }
        .sheet(isPresented: $store.terminating) {
            VStack(alignment: .leading, spacing: 20) {
                HStack(spacing: 14) {
                    ProgressView().controlSize(.small)
                    Text("正在安全退出").font(.system(size: 20, weight: .semibold))
                }
                Text("正在请求暂停并等待采集进程结束。已保存的结果会保留，下次打开后可以继续。").font(.system(size: 13)).foregroundStyle(.secondary).lineSpacing(5)
                HStack { Spacer(); Button(store.cancellingTermination ? "正在取消…" : "取消退出") { store.cancelTermination() }.buttonStyle(SoftButton()).disabled(store.cancellingTermination) }
            }.padding(30).frame(width: 430).interactiveDismissDisabled()
        }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in store.activated() }
    }

    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 11) {
                Image(systemName: "text.viewfinder").font(.system(size: 23, weight: .medium)).foregroundStyle(.white)
                    .frame(width: 44, height: 44).background(LinearGradient(colors: [Theme.accent, Color(red: 0.96, green: 0.46, blue: 0.35)], startPoint: .topLeading, endPoint: .bottomTrailing), in: RoundedRectangle(cornerRadius: 13))
                VStack(alignment: .leading, spacing: 5) {
                    Text("采集助手").font(.system(size: 17, weight: .semibold))
                    Text("小红书 · 手机实验室").font(.system(size: 10)).foregroundStyle(.secondary)
                }
            }.padding(.horizontal, 20).padding(.top, 52).padding(.bottom, 38)
            Text("工作空间").font(.system(size: 10, weight: .semibold)).foregroundStyle(.tertiary).tracking(1).padding(.horizontal, 25).padding(.bottom, 12)
            VStack(spacing: 7) {
                ForEach(NavigationPage.allCases) { page in
                    Button { store.page = page } label: {
                        HStack(spacing: 12) {
                            Image(systemName: page.symbol).font(.system(size: 15, weight: .medium)).frame(width: 21)
                            Text(page.rawValue).font(.system(size: 13, weight: store.page == page ? .semibold : .regular))
                            Spacer()
                            if page == .results && store.state.busy && store.bridgeAlive { Circle().fill(Theme.accent).frame(width: 6, height: 6) }
                        }.foregroundStyle(store.page == page ? Theme.accent : Color.primary.opacity(0.72))
                            .padding(.horizontal, 13).padding(.vertical, 13)
                            .background(store.page == page ? Theme.accentSoft : .clear, in: RoundedRectangle(cornerRadius: 10))
                            .contentShape(RoundedRectangle(cornerRadius: 10))
                    }.buttonStyle(.plain).accessibilityIdentifier("navigation_\(page.id)")
                }
            }.padding(.horizontal, 13)
            Spacer(minLength: 30)
            VStack(alignment: .leading, spacing: 13) {
                HStack {
                    Image(systemName: "iphone.gen3").font(.system(size: 18)).foregroundStyle(.secondary)
                    VStack(alignment: .leading, spacing: 4) {
                        Text("云手机 \(store.state.device.id)").font(.system(size: 11, weight: .medium))
                        Text(store.state.device.label).font(.system(size: 10)).foregroundStyle(Theme.statusColor(store.state.device.status))
                    }
                    Spacer(minLength: 0)
                }
                Button { store.openCloud() } label: { HStack { Text("打开云手机"); Spacer(); Image(systemName: "arrow.up.right") } }
                    .buttonStyle(SoftButton()).accessibilityIdentifier("sidebar_open_cloud")
            }.padding(15).background(Theme.card.opacity(0.7), in: RoundedRectangle(cornerRadius: 13)).padding(.horizontal, 13)
            HStack(spacing: 5) { Image(systemName: "desktopcomputer"); Text("在本机独立运行") }
                .font(.system(size: 10)).foregroundStyle(.tertiary).padding(.horizontal, 25).padding(.top, 18).padding(.bottom, 23)
        }.frame(maxHeight: .infinity).background(.regularMaterial)
    }

    private var header: some View {
        HStack(alignment: .center) {
            VStack(alignment: .leading, spacing: 9) {
                Text(store.preview ? "SYNTHETIC · 界面预览" : "XHS MOBILE LAB").font(.system(size: 9, weight: .semibold)).tracking(2).foregroundStyle(.tertiary)
                Text(store.page.rawValue).font(.system(size: 27, weight: .semibold))
                Text(store.page.subtitle).font(.system(size: 12)).foregroundStyle(.secondary)
            }
            Spacer(minLength: 16)
            StatusPill(text: store.state.device.label, status: store.state.device.status)
        }.padding(.horizontal, 30).padding(.top, 39).padding(.bottom, 26)
    }

    private var footer: some View {
        HStack(spacing: 7) {
            Circle().fill(store.bridgeAlive ? Theme.green : Color.secondary).frame(width: 5, height: 5)
            Text(store.preview ? "synthetic 测试样本 · 不操作设备" : store.bridgeAlive ? (store.state.initialized ? "本机控制程序已连接" : "正在加载本机环境") : "本机控制程序未连接").font(.system(size: 10)).foregroundStyle(.secondary)
            if store.state.busy && store.bridgeAlive { Text("· 关闭窗口后继续运行").font(.system(size: 10)).foregroundStyle(.secondary) }
            Spacer()
            Button("诊断详情") { store.openLogs() }.buttonStyle(.plain).font(.system(size: 10)).foregroundStyle(.secondary)
        }.padding(.horizontal, 30).padding(.vertical, 12).background(Theme.card.opacity(0.45))
    }

    private func issueBanner(_ issue: Incident) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: "exclamationmark.circle.fill").foregroundStyle(Theme.amber).font(.system(size: 17)).padding(.top, 1)
            VStack(alignment: .leading, spacing: 5) {
                Text(issue.title).font(.system(size: 12, weight: .semibold))
                Text(issue.message).font(.system(size: 11)).foregroundStyle(.secondary).lineSpacing(3).fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 12)
            VStack(alignment: .trailing, spacing: 8) {
                if issue.connectionSettingsAction { Button("连接设置") { store.showingConnectionSettings = true }.buttonStyle(SoftButton()).disabled(store.state.busy || store.pendingAction || store.terminating) }
                if issue.cloudAction { Button("打开云手机") { store.openCloud() }.buttonStyle(SoftButton()) }
                else if !issue.connectionSettingsAction { Button("诊断详情") { store.openLogs() }.buttonStyle(SoftButton()) }
            }
        }.padding(16).background(Theme.amber.opacity(0.075), in: RoundedRectangle(cornerRadius: 12))
    }
}

struct CollectPage: View {
    @EnvironmentObject private var store: AppStore
    @FocusState private var editorFocused: Bool
    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            if store.bridgeAlive, let activity = store.state.activity { ActivityPanel(activity: activity) }
            HStack(alignment: .top, spacing: 20) {
                VStack(spacing: 18) {
                    Panel {
                        HStack(alignment: .top) {
                            SectionLabel(title: "你想采集什么？", subtitle: "一个关键词或一组关键词，都从这里开始。")
                            Spacer()
                            Image(systemName: "sparkle.magnifyingglass").font(.system(size: 24, weight: .light)).foregroundStyle(Theme.accent)
                        }
                        VStack(alignment: .leading, spacing: 9) {
                            Text("搜索关键词").font(.system(size: 12, weight: .medium))
                            ZStack(alignment: .topLeading) {
                                if store.keywords.isEmpty {
                                    Text("每行一个关键词，最多 20 个\n例如：香港城市大学\n校园生活").font(.system(size: 13)).foregroundStyle(.tertiary).lineSpacing(8).padding(.horizontal, 12).padding(.top, 12).allowsHitTesting(false)
                                }
                                TextEditor(text: $store.keywords).font(.system(size: 13)).lineSpacing(8).scrollContentBackground(.hidden)
                                    .padding(7).frame(height: 152).focused($editorFocused).accessibilityLabel("搜索关键词，每行一个").accessibilityIdentifier("keyword_editor")
                            }.background(Theme.canvas.opacity(0.65), in: RoundedRectangle(cornerRadius: 10))
                                .overlay(RoundedRectangle(cornerRadius: 10).stroke(editorFocused ? Theme.accent.opacity(0.45) : Theme.stroke, lineWidth: 1))
                            HStack(spacing: 6) {
                                if store.uniqueKeywords.count > 20 { Label("最多支持 20 个不同关键词", systemImage: "exclamationmark.circle").foregroundStyle(Theme.amber) }
                                else if store.duplicateCount > 0 { Label("已合并 \(store.duplicateCount) 个重复关键词", systemImage: "checkmark.circle").foregroundStyle(Theme.green) }
                                else { Text("可直接粘贴多行内容，空行会自动忽略。 ").foregroundStyle(.secondary) }
                                Spacer(minLength: 0)
                                Text("\(store.uniqueKeywords.count) / 20").foregroundStyle(.secondary).monospacedDigit()
                            }.font(.system(size: 10))
                        }
                        HStack(spacing: 12) {
                            VStack(alignment: .leading, spacing: 5) {
                                Text("每个关键词的目标").font(.system(size: 12, weight: .medium))
                                Text("按基础字段可读记录计数，正文完整性由人工核对。 ").font(.system(size: 10)).foregroundStyle(.secondary)
                            }
                            Spacer(minLength: 0)
                            HStack(spacing: 6) {
                                TextField("10", text: $store.targetText).textFieldStyle(.roundedBorder)
                                    .font(.system(size: 14, weight: .semibold)).monospacedDigit().multilineTextAlignment(.center)
                                    .frame(width: 48).accessibilityLabel("每个关键词的目标条数").accessibilityIdentifier("target_field")
                                Text("条").font(.system(size: 11)).foregroundStyle(.secondary)
                                Stepper("", value: Binding(get: { max(1, min(store.target, 500)) }, set: { store.target = $0 }), in: 1...500)
                                    .labelsHidden().fixedSize().accessibilityLabel("调整目标条数").accessibilityIdentifier("target_stepper")
                            }
                        }
                        if !store.validTarget { Text("请输入 1–500 之间的整数。").font(.system(size: 11)).foregroundStyle(Theme.amber) }
                        Rectangle().fill(Theme.stroke).frame(height: 1)
                        HStack(alignment: .center) {
                            VStack(alignment: .leading, spacing: 6) {
                                Text("\(store.uniqueKeywords.count) 个关键词 · 共 \(store.uniqueKeywords.count * store.target) 条目标").font(.system(size: 12, weight: .medium))
                                Text("开始前自动检查设备与存储").font(.system(size: 10)).foregroundStyle(.secondary)
                            }
                            Spacer()
                            Button { editorFocused = false; store.start() } label: {
                                HStack(spacing: 8) { Image(systemName: "play.fill").font(.system(size: 10)); Text(store.pendingAction ? "正在提交" : "开始采集") }
                            }.buttonStyle(PrimaryButton()).disabled(!store.canStart || store.uniqueKeywords.isEmpty || store.uniqueKeywords.count > 20 || !store.validTarget).accessibilityIdentifier("start_keywords")
                        }
                    }
                    Panel(padding: 20) {
                        HStack(spacing: 14) {
                            Image(systemName: "doc.viewfinder").font(.system(size: 22, weight: .light)).foregroundStyle(.secondary).frame(width: 38)
                            VStack(alignment: .leading, spacing: 7) {
                                Text("只采集手机当前笔记").font(.system(size: 12, weight: .medium))
                                Text("先在云手机中打开一篇图文详情，无需填写关键词。 ").font(.system(size: 11)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                            }
                            Spacer(minLength: 8)
                            Button("采集当前页") { store.start(current: true) }.buttonStyle(SoftButton()).disabled(!store.canStart).accessibilityIdentifier("start_current")
                        }
                    }
                }.frame(maxWidth: .infinity)
                VStack(spacing: 18) {
                    DeviceSummaryPanel()
                    Panel(padding: 20) {
                        Text("每一步，都有迹可循").font(.system(size: 13, weight: .semibold))
                        VStack(alignment: .leading, spacing: 20) {
                            workflowStep("1", "自动搜索与采集", "依次执行关键词，保存真实原文。")
                            workflowStep("2", "随时暂停与继续", "保留已提交进度，重新打开即可查看。")
                            workflowStep("3", "查看与导出结果", "支持 CSV、JSONL 和来源证据。")
                        }
                    }
                    Label("打开 App 不会自动开始采集。", systemImage: "hand.raised")
                        .font(.system(size: 10)).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 7)
                }.frame(width: 252)
            }
        }
    }
    private func workflowStep(_ number: String, _ title: String, _ description: String) -> some View {
        HStack(alignment: .top, spacing: 11) {
            Text(number).font(.system(size: 10, weight: .semibold, design: .rounded)).foregroundStyle(Theme.accent)
                .frame(width: 23, height: 23).background(Theme.accentSoft, in: Circle())
            VStack(alignment: .leading, spacing: 6) {
                Text(title).font(.system(size: 11, weight: .medium))
                Text(description).font(.system(size: 10)).foregroundStyle(.secondary).lineSpacing(3).fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

struct DeviceSummaryPanel: View {
    @EnvironmentObject private var store: AppStore
    var body: some View {
        Panel(padding: 20) {
            HStack {
                Image(systemName: "iphone.gen3").font(.system(size: 23, weight: .light)).foregroundStyle(Theme.accent)
                Spacer()
                StatusPill(text: store.state.device.label, status: store.state.device.status)
            }
            VStack(alignment: .leading, spacing: 8) {
                Text("云手机 \(store.state.device.id)").font(.system(size: 15, weight: .semibold))
                Text(!store.state.initialized && store.bridgeAlive ? "首次启动可能需要允许访问“文稿”文件夹。请留意 macOS 系统提示。" : store.state.device.message).font(.system(size: 11)).foregroundStyle(.secondary).lineSpacing(4).fixedSize(horizontal: false, vertical: true)
            }
            Button { store.openCloud() } label: { HStack { Text("打开云手机"); Spacer(); Image(systemName: "arrow.up.right") } }.buttonStyle(SoftButton())
            Button("查看连接详情") { store.page = .device }.buttonStyle(.plain).font(.system(size: 11)).foregroundStyle(.secondary)
        }
    }
}

struct ActivityPanel: View {
    @EnvironmentObject private var store: AppStore
    let activity: Activity
    var body: some View {
        Panel(padding: 20) {
            HStack(spacing: 14) {
                ProgressView().controlSize(.small).padding(9)
                VStack(alignment: .leading, spacing: 7) {
                    Text(activity.label + (activity.keyword.isEmpty ? "" : " · " + activity.keyword)).font(.system(size: 14, weight: .semibold))
                    Text(activity.message.isEmpty ? "已提交的进度会自动保存，关闭窗口后继续运行。" : activity.message).font(.system(size: 11)).foregroundStyle(.secondary)
                }
                Spacer()
                if let item = store.activeTask, activity.phase != "exporting" {
                    Button(activity.phase == "pausing" ? "正在暂停…" : "暂停") { store.perform("pause", params: item.parameters) }
                        .buttonStyle(SoftButton()).disabled(!store.bridgeAlive || store.pendingAction || activity.phase == "pausing")
                }
                Button("查看进度") { store.page = .results; if let item = store.activeTask { store.select(item) } }.buttonStyle(SoftButton())
            }
            if let item = store.activeTask {
                ProgressView(value: item.progress).tint(Theme.accent)
                Text("基础字段可读 \(item.eligible) / \(item.target) · 已保存 \(item.observations) 条记录").font(.system(size: 11)).foregroundStyle(.secondary)
            }
        }
    }
}
