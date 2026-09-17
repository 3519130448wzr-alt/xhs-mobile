import Foundation
import Security
import CryptoKit
import LocalAuthentication
import Darwin

// Restricted native helper: secrets never cross the Python desktop protocol.
// API and ACS3 signing references: help.aliyun.com/zh/ecs/developer-reference/
// api-ecs-2014-05-26-{describe-prefix-list*,modifyprefixlist} and
// help.aliyun.com/zh/sdk/product-overview/v3-request-structure-and-signature.
struct ConnectionFailure: LocalizedError {
    let code: String
    let message: String
    var errorDescription: String? { message }
    static let configuration = ConnectionFailure(code: "configuration", message: "自动连接设置不完整或名单不符合约束，请打开连接设置核对。")
    static let credentials = ConnectionFailure(code: "credentials", message: "无法读取连接凭据，请打开连接设置解锁钥匙串或重新录入专用凭据。")
    static let timeout = ConnectionFailure(code: "cloud_timeout", message: "云端授权响应超时，连接检查已停止。可稍后重试。")
}

struct AutoConnectionConfiguration {
    let region: String
    let prefixListID: String
    let securityGroupID: String
    let credentialRef: String
    let accountID: String
    let ownerMarker: String
    let enabled: Bool
    init(_ json: JSONObject) throws {
        region = json.string("region"); prefixListID = json.string("prefix_list_id")
        securityGroupID = json.string("security_group_id"); credentialRef = json.string("credential_ref")
        accountID = json.string("account_id"); ownerMarker = json.string("owner_marker"); enabled = json.bool("enabled")
        guard Self.matches(region, "^[a-z]{2}-[a-z0-9-]{2,30}$"), Self.matches(prefixListID, "^pl-[A-Za-z0-9]{5,64}$"),
              Self.matches(securityGroupID, "^sg-[A-Za-z0-9]{5,64}$"), Self.matches(accountID, "^[0-9]{8,20}$"),
              Self.matches(credentialRef, "^xhs-mobile-[A-Za-z0-9._-]{1,100}$"),
              Self.matches(ownerMarker, "^xhs-mobile-[A-Za-z0-9._-]{1,100}$") else { throw ConnectionFailure.configuration }
    }
    static func matches(_ text: String, _ pattern: String) -> Bool {
        guard let range = text.range(of: pattern, options: .regularExpression) else { return false }
        return range.lowerBound == text.startIndex && range.upperBound == text.endIndex
    }
    var binding: String { [region, accountID, prefixListID, securityGroupID, ownerMarker].joined(separator: "|") }
    var resourceARN: String { "acs:ecs:\(region):\(accountID):prefixlist/\(prefixListID)" }
    static func read(_ path: String) throws -> (JSONObject, AutoConnectionConfiguration) {
        guard path.hasPrefix("/"), !path.contains("\0") else { throw ConnectionFailure.configuration }
        let attrs = try FileManager.default.attributesOfItem(atPath: path)
        guard attrs[.type] as? FileAttributeType == .typeRegular,
              (attrs[.ownerAccountID] as? NSNumber)?.uint32Value == getuid(),
              ((attrs[.posixPermissions] as? NSNumber)?.intValue ?? 0o777) & 0o077 == 0,
              (attrs[.size] as? NSNumber)?.intValue ?? Int.max < 1_048_576 else { throw ConnectionFailure.configuration }
        guard let root = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: path))) as? JSONObject else { throw ConnectionFailure.configuration }
        return (root, try AutoConnectionConfiguration(root.object("auto_connection")))
    }
}

struct ConnectionCredentials: Codable {
    let accessKeyID: String
    let accessKeySecret: String
    let binding: String
}

enum ConnectionKeychain {
    static let service = "com.xhs.mobile.desktop.auto-connection"
    static func query(_ reference: String) -> JSONObject {
        [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service, kSecAttrAccount as String: reference]
    }
    static func save(id: String, secret: String, config: AutoConnectionConfiguration) throws {
        guard AutoConnectionConfiguration.matches(id, "^[A-Za-z0-9]{10,128}$"),
              AutoConnectionConfiguration.matches(secret, "^[A-Za-z0-9+/=_-]{16,256}$") else {
            throw ConnectionFailure(code: "credentials", message: "请完整填写专用 AccessKey ID 和 AccessKey Secret。")
        }
        let data = try JSONEncoder().encode(ConnectionCredentials(accessKeyID: id, accessKeySecret: secret, binding: config.binding))
        let item = query(config.credentialRef)
        let changes: JSONObject = [kSecValueData as String: data]
        let result = SecItemUpdate(item as CFDictionary, changes as CFDictionary)
        if result == errSecItemNotFound {
            var creation = item; creation[kSecValueData as String] = data
            creation[kSecAttrLabel as String] = "小红书采集助手 · 自动连接"
            let added = SecItemAdd(creation as CFDictionary, nil)
            guard added == errSecSuccess else { throw ConnectionFailure.credentials }
        } else if result != errSecSuccess { throw ConnectionFailure.credentials }
    }
    static func load(_ config: AutoConnectionConfiguration, interactive: Bool = false) throws -> ConnectionCredentials {
        var request = query(config.credentialRef)
        request[kSecReturnData as String] = true; request[kSecMatchLimit as String] = kSecMatchLimitOne
        // Startup never steals focus or waits forever for a keychain dialog.
        let context = LAContext(); context.interactionNotAllowed = !interactive
        request[kSecUseAuthenticationContext as String] = context
        var result: CFTypeRef?
        guard SecItemCopyMatching(request as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data, let value = try? JSONDecoder().decode(ConnectionCredentials.self, from: data),
              value.binding == config.binding else { throw ConnectionFailure.credentials }
        return value
    }
}

enum PublicIPv4 {
    static func validCIDR(_ value: String) -> Bool {
        let pair = value.split(separator: "/", omittingEmptySubsequences: false)
        guard pair.count == 2, pair[1] == "32" else { return false }
        let fields = pair[0].split(separator: ".", omittingEmptySubsequences: false)
        guard fields.count == 4 else { return false }
        var bytes: [UInt32] = []
        for field in fields {
            guard let number = UInt32(field), number < 256, String(number) == field else { return false }
            bytes.append(number)
        }
        let address = bytes.reduce(UInt32(0)) { ($0 << 8) | $1 }
        let denied: [(UInt32, UInt32)] = [
            (0x00000000, 8), (0x0a000000, 8), (0x64400000, 10), (0x7f000000, 8),
            (0xa9fe0000, 16), (0xac100000, 12), (0xc0000000, 24), (0xc0000200, 24),
            (0xc0586300, 24), (0xc0a80000, 16), (0xc6120000, 15), (0xc6336400, 24),
            (0xcb007100, 24), (0xe0000000, 4), (0xf0000000, 4)
        ]
        return !denied.contains { prefix, bits in (address >> (32 - bits)) == (prefix >> (32 - bits)) }
    }
}

enum AlibabaSigner {
    static func hex(_ data: Data) -> String { data.map { String(format: "%02x", $0) }.joined() }
    static func sha256(_ data: Data) -> String { hex(Data(SHA256.hash(data: data))) }
    static func encode(_ value: String) -> String {
        value.addingPercentEncoding(withAllowedCharacters: CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~"))!
    }
    static func sign(action: String, query: [String: String], region: String, credentials: ConnectionCredentials, date: String, nonce: String) throws -> URLRequest {
        let allowed = ["DescribePrefixListAttributes", "DescribePrefixListAssociations", "ModifyPrefixList"]
        guard allowed.contains(action), AutoConnectionConfiguration.matches(region, "^[a-z]{2}-[a-z0-9-]{2,30}$") else { throw ConnectionFailure.configuration }
        let host = "ecs.\(region).aliyuncs.com"
        let payloadHash = sha256(Data())
        let headers = ["host": host, "x-acs-action": action, "x-acs-content-sha256": payloadHash,
                       "x-acs-date": date, "x-acs-signature-nonce": nonce, "x-acs-version": "2014-05-26"]
        let names = headers.keys.sorted()
        let signedHeaders = names.joined(separator: ";")
        let canonicalHeaders = names.map { "\($0):\(headers[$0]!)\n" }.joined()
        let canonicalQuery = query.keys.sorted().map { encode($0) + "=" + encode(query[$0]!) }.joined(separator: "&")
        let canonical = ["POST", "/", canonicalQuery, canonicalHeaders, signedHeaders, payloadHash].joined(separator: "\n")
        let stringToSign = "ACS3-HMAC-SHA256\n" + sha256(Data(canonical.utf8))
        let signature = hex(Data(HMAC<SHA256>.authenticationCode(for: Data(stringToSign.utf8), using: SymmetricKey(data: Data(credentials.accessKeySecret.utf8)))))
        var request = URLRequest(url: URL(string: "https://\(host)/?\(canonicalQuery)")!)
        request.httpMethod = "POST"
        for (key, value) in headers { request.setValue(value, forHTTPHeaderField: key) }
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("ACS3-HMAC-SHA256 Credential=\(credentials.accessKeyID),SignedHeaders=\(signedHeaders),Signature=\(signature)", forHTTPHeaderField: "Authorization")
        return request
    }
}

private final class NoRedirectDelegate: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
}

// This transport has no arbitrary endpoint, logging, cookies, credentials or redirect forwarding.
final class AlibabaPrefixClient {
    let config: AutoConnectionConfiguration
    let credentials: ConnectionCredentials
    let deadline: Date
    private let fixtureTransport: ((String, [String: String]) throws -> JSONObject)?
    init(config: AutoConnectionConfiguration, credentials: ConnectionCredentials, timeout: Double, fixtureTransport: ((String, [String: String]) throws -> JSONObject)? = nil) {
        self.config = config; self.credentials = credentials; deadline = Date().addingTimeInterval(timeout); self.fixtureTransport = fixtureTransport
    }
    func call(_ action: String, _ parameters: [String: String] = [:]) throws -> JSONObject {
        let remaining = deadline.timeIntervalSinceNow
        guard remaining > 0 else { throw ConnectionFailure.timeout }
        if let fixtureTransport { return try fixtureTransport(action, parameters) }
        var query = ["RegionId": config.region, "PrefixListId": config.prefixListID]
        for (key, value) in parameters { query[key] = value }
        let date = ISO8601DateFormatter().string(from: Date())
        var request = try AlibabaSigner.sign(action: action, query: query, region: config.region, credentials: credentials, date: date, nonce: UUID().uuidString)
        request.timeoutInterval = remaining
        let settings = URLSessionConfiguration.ephemeral
        settings.timeoutIntervalForRequest = remaining; settings.timeoutIntervalForResource = remaining
        settings.httpCookieStorage = nil; settings.urlCache = nil
        let session = URLSession(configuration: settings, delegate: NoRedirectDelegate(), delegateQueue: nil)
        defer { session.invalidateAndCancel() }
        let semaphore = DispatchSemaphore(value: 0)
        let box = HTTPResultBox()
        let task = session.dataTask(with: request) { data, response, error in
            box.set(data: data, response: response, failed: error != nil); semaphore.signal()
        }
        task.resume()
        guard semaphore.wait(timeout: .now() + remaining) == .success else { task.cancel(); throw ConnectionFailure.timeout }
        let result = box.get()
        guard !result.failed, let data = result.data, data.count < 1_048_576, let response = result.response as? HTTPURLResponse,
              let json = (try? JSONSerialization.jsonObject(with: data)) as? JSONObject else { throw ConnectionFailure.timeout }
        guard response.statusCode == 200, json["Code"] == nil else {
            let code = json.string("Code")
            if response.statusCode == 401 || response.statusCode == 403 || code.contains("AccessKey") || code.contains("Signature") || code.contains("Forbidden") {
                throw ConnectionFailure(code: "authorization", message: "专用连接凭据未获准访问这份名单，请核对限定名单的 RAM 权限。")
            }
            throw ConnectionFailure(code: "cloud_error", message: "云端未接受名单操作，请检查名单配置并稍后重试；原始响应未写入日志。")
        }
        return json
    }
    func inspect() throws -> [String] {
        let attributes = try call("DescribePrefixListAttributes")
        let associations = try call("DescribePrefixListAssociations", ["MaxResults": "100"])
        return try PrefixListGuard.validate(attributes: attributes, associations: associations, config: config)
    }
    func execute(operation: String, cidr: String?) throws -> [String] {
        guard ["inspect", "add_candidate", "remove_candidate"].contains(operation) else { throw ConnectionFailure.configuration }
        if operation != "inspect", !PublicIPv4.validCIDR(cidr ?? "") { throw ConnectionFailure.configuration }
        let entries = try inspect()
        if operation == "inspect" { return entries }
        let cidr = cidr!
        if operation == "add_candidate" {
            if entries.contains(cidr) { return entries }
            guard entries.count < 2 else { throw ConnectionFailure(code: "configuration", message: "名单中已有两个地址。请先完成上一次连接核对，程序不会扩大容量。") }
            _ = try call("ModifyPrefixList", ["AddEntry.1.Cidr": cidr, "AddEntry.1.Description": "xhs-mobile connection"])
        } else {
            if !entries.contains(cidr) { return entries }
            _ = try call("ModifyPrefixList", ["RemoveEntry.1.Cidr": cidr])
        }
        return try inspect()
    }
}

private final class HTTPResultBox: @unchecked Sendable {
    private let lock = NSLock()
    private var data: Data?; private var response: URLResponse?; private var failed = false
    func set(data: Data?, response: URLResponse?, failed: Bool) { lock.lock(); defer { lock.unlock() }; self.data = data; self.response = response; self.failed = failed }
    func get() -> (data: Data?, response: URLResponse?, failed: Bool) { lock.lock(); defer { lock.unlock() }; return (data, response, failed) }
}

enum PrefixListGuard {
    private static func mismatch(_ message: String) -> ConnectionFailure {
        ConnectionFailure(code: "configuration", message: message)
    }
    // Report structure only. No cloud response values or credentials enter diagnostics.
    private static func responseType(_ value: Any?) -> String {
        guard let value else { return "字段缺失" }
        if value is NSNull { return "空值" }
        if value is JSONObject { return "对象" }
        if value is [Any] { return "数组" }
        if value is String { return "字符串" }
        if value is NSNumber { return "数值或布尔" }
        return "未知类型"
    }
    static func validate(attributes: JSONObject, associations: JSONObject, config: AutoConnectionConfiguration) throws -> [String] {
        guard attributes.string("PrefixListId") == config.prefixListID else {
            throw mismatch("云端返回的前缀列表 ID 与连接设置不匹配。")
        }
        guard attributes.string("PrefixListName") == config.ownerMarker else {
            throw mismatch("云端返回的名单名称与连接设置不匹配。")
        }
        guard attributes.string("AddressFamily") == "IPv4" else {
            throw mismatch("云端返回的名单地址族不是预期的 IPv4。")
        }
        guard let capacity = attributes["MaxEntries"] as? NSNumber, capacity.doubleValue == 2 else {
            throw mismatch("云端返回的名单容量不是预期的 2。")
        }
        guard associations["NextToken"] == nil || (associations["NextToken"] as? String)?.isEmpty == true else {
            throw mismatch("云端关联资源响应仍有分页，尚不能确认名单仅关联当前安全组。")
        }
        let linked = associations.object("PrefixListAssociations").objects("PrefixListAssociation")
        guard linked.count == 1, linked[0].string("ResourceId") == config.securityGroupID, linked[0].string("ResourceType") == "securitygroup" else {
            throw mismatch("这份地址名单必须只关联当前云手机安全组，请核对云端关联关系。")
        }
        // Observed with the authenticated service and console 0/2 entries on 2026-09-15:
        // the service omits top-level Entries for an empty list. The official SDK model
        // also makes entries optional: aliyun/alibabacloud-python-sdk/ecs-20140526/
        // alibabacloud_ecs20140526/models/_describe_prefix_list_attributes_response_body.py.
        // Normalize only this exact shape, after all resource and association guards.
        if attributes["Entries"] == nil {
            guard let requestID = attributes["RequestId"] as? String,
                  !requestID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
                throw mismatch("云端 Entries 字段缺失，且未返回有效的请求标识。")
            }
            return []
        }
        guard let entryContainer = attributes["Entries"] as? JSONObject else {
            throw mismatch("云端 Entries 容器不是预期的对象（\(responseType(attributes["Entries"]))）。")
        }
        guard let rawEntries = entryContainer["Entry"] as? [JSONObject] else {
            throw mismatch("云端 Entry 条目不是预期的对象数组（\(responseType(entryContainer["Entry"]))）。")
        }
        guard rawEntries.allSatisfy({ $0["Cidr"] is String }) else {
            throw mismatch("云端条目的 Cidr 字段缺失或类型不是字符串。")
        }
        let entries = rawEntries.map { $0.string("Cidr") }
        guard entries.count <= 2 else { throw mismatch("云端名单条目超过允许的两个地址。") }
        guard Set(entries).count == entries.count else { throw mismatch("云端名单包含重复的地址条目。") }
        guard entries.allSatisfy(PublicIPv4.validCIDR) else { throw mismatch("云端名单包含不符合单个公网 IPv4 /32 要求的条目。") }
        return entries.sorted()
    }
}

enum ConnectionHelper {
    static func run() -> Int32 {
        do {
            let args = CommandLine.arguments
            guard let i = args.firstIndex(of: "--record"), args.count == i + 2, i == 2 else { throw ConnectionFailure.configuration }
            guard let line = readLine(), line.utf8.count < 8_192,
                  let request = (try? JSONSerialization.jsonObject(with: Data(line.utf8))) as? JSONObject,
                  Set(request.keys).isSubset(of: ["operation", "cidr", "timeout_seconds"]) else { throw ConnectionFailure.configuration }
            let operation = request.string("operation")
            guard ["inspect", "add_candidate", "remove_candidate"].contains(operation), operation == "inspect" || PublicIPv4.validCIDR(request.string("cidr")) else { throw ConnectionFailure.configuration }
            let (_, config) = try AutoConnectionConfiguration.read(args[i + 1])
            guard config.enabled else { throw ConnectionFailure.configuration }
            let credentials = try ConnectionKeychain.load(config)
            let timeout = min(15, max(1, (request["timeout_seconds"] as? NSNumber)?.doubleValue ?? 15))
            let entries = try AlibabaPrefixClient(config: config, credentials: credentials, timeout: timeout).execute(operation: operation, cidr: request["cidr"] as? String)
            emit(["ok": true, "entries": entries, "max_entries": 2, "associated": true, "resource_arn": config.resourceARN])
            return 0
        } catch let error as ConnectionFailure {
            emit(["ok": false, "code": error.code, "message": error.message]); return 1
        } catch {
            emit(["ok": false, "code": "configuration", "message": "无法读取连接设置，请打开 App 的连接设置核对。"]); return 1
        }
    }
    private static func emit(_ value: JSONObject) {
        guard var data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) else { return }
        data.append(10); try? FileHandle.standardOutput.write(contentsOf: data)
    }
}
