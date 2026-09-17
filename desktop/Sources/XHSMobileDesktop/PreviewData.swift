import Foundation

// Synthetic UI fixtures. Never loaded by normal launch or persisted as collection results.
enum PreviewData {
    static let state = AppState([
        "initialized": true, "busy": false,
        "device": ["id": "lab01", "status": "unknown", "message": "开始采集前会自动检查连接。也可以先去云手机确认页面。", "console_url": "https://wya.wuying.aliyun.com/instanceLayouts",
                   "checks": [["id": "adb", "label": "手机连接", "status": "unknown", "message": "点击检查连接后更新"],
                              ["id": "automation", "label": "自动化服务", "status": "unknown", "message": "检查页面读取和控制服务"],
                              ["id": "app", "label": "小红书适配", "status": "unknown", "message": "核对已安装版本与页面规则"],
                              ["id": "database", "label": "本机数据库", "status": "ready", "message": "synthetic 预览状态，不代表真实连接"]]],
        "tasks": [["id": "synthetic-preview-task", "kind": "batch", "title": "界面预览 · synthetic", "status": "collected_awaiting_review", "stop_reason": "target_collected",
                   "observations": 12, "eligible": 10, "confirmed_identities": 0, "target": 10,
                   "created_at": "2026-09-14T10:20:00+08:00", "updated_at": "2026-09-14T10:30:00+08:00", "active": false,
                   "can_resume": false, "requires_ack": false, "tasks": []]]
    ])
    static let records = [NoteRecord(["id": "synthetic-preview-note", "title": "示例笔记 · 仅用于界面预览", "author": "synthetic 测试作者",
                                     "body": "这里展示采集到的笔记原文。\n\n真实运行时，每条记录都保留字段质量与来源证据，便于逐项人工核对。这份文字是明确标记的界面测试样本，不属于真实采集成果。",
                                     "eligible": false, "quality": "synthetic · 仅用于界面测试", "evidence_paths": []])]
}
