import Foundation

// All fixtures below are synthetic. Tests never touch the user's Keychain or cloud.
enum ConnectionSelfTests {
    private enum Failure: Error { case assertion(String) }
    private static func require(_ value: @autoclosure () -> Bool, _ message: String) throws {
        if !value() { throw Failure.assertion(message) }
    }
    static let metadata: JSONObject = ["enabled": true, "region": "cn-shanghai", "account_id": "1234567890123456",
                                       "prefix_list_id": "pl-synthetic123", "security_group_id": "sg-synthetic123",
                                       "credential_ref": "xhs-mobile-synthetic", "owner_marker": "xhs-mobile-synthetic"]
    static func attributes(_ entries: [String] = []) -> JSONObject {
        ["PrefixListId": "pl-synthetic123", "PrefixListName": "xhs-mobile-synthetic", "MaxEntries": 2,
         "AddressFamily": "IPv4", "Entries": ["Entry": entries.map { ["Cidr": $0] }]]
    }
    static let associations: JSONObject = ["PrefixListAssociations": ["PrefixListAssociation": [["ResourceId": "sg-synthetic123", "ResourceType": "securitygroup"]]]]
    static func fails(_ operation: () throws -> Void) throws {
        do { try operation(); throw Failure.assertion("invalid input accepted") }
        catch is ConnectionFailure {}
    }
    static var cases: [(String, () throws -> Void)] { [
        ("public_ipv4_only_cidr32", {
            for value in ["8.8.8.8/32", "144.214.0.7/32", "218.252.211.231/32"] { try require(PublicIPv4.validCIDR(value), "valid public CIDR rejected") }
            for value in ["0.0.0.0/0", "0.0.0.0/32", "127.0.0.1/32", "10.0.0.1/32", "100.64.0.1/32", "192.168.0.2/32", "192.0.2.1/32", "198.18.0.1/32", "198.51.100.1/32", "203.0.113.1/32", "224.0.0.1/32", "255.255.255.255/32", "2001:db8::1/32", "8.8.8.8/24", "008.8.8.8/32", "8.8.8.8/32\n"] { try require(!PublicIPv4.validCIDR(value), "invalid CIDR accepted: " + value) }
        }),
        ("credential_binding_and_fixed_region", {
            let original = try AutoConnectionConfiguration(metadata)
            var changed = metadata; changed["prefix_list_id"] = "pl-otherlist123"
            let rebound = try AutoConnectionConfiguration(changed)
            try require(original.binding != rebound.binding, "resource substitution did not change binding")
            changed["region"] = "cn-shanghai\n"
            try fails { _ = try AutoConnectionConfiguration(changed) }
            changed["region"] = "cn-shanghai.evil.example"
            try fails { _ = try AutoConnectionConfiguration(changed) }
            try require(original.resourceARN == "acs:ecs:cn-shanghai:1234567890123456:prefixlist/pl-synthetic123", "resource ARN widened")
        }),
        ("prefix_list_guard_and_capacity", {
            let config = try AutoConnectionConfiguration(metadata)
            let valid = try PrefixListGuard.validate(attributes: attributes(["8.8.8.8/32"]), associations: associations, config: config)
            try require(valid == ["8.8.8.8/32"], "valid list failed")
            for (key, value) in [("MaxEntries", 3 as Any), ("PrefixListName", "unrelated" as Any), ("AddressFamily", "IPv6" as Any), ("PrefixListId", "pl-other123" as Any)] {
                var invalid = attributes(); invalid[key] = value
                try fails { _ = try PrefixListGuard.validate(attributes: invalid, associations: associations, config: config) }
            }
            try fails { _ = try PrefixListGuard.validate(attributes: attributes(["8.8.8.8/32", "8.8.8.8/32"]), associations: associations, config: config) }
            try fails { _ = try PrefixListGuard.validate(attributes: attributes(["0.0.0.0/0"]), associations: associations, config: config) }
        }),
        ("prefix_list_malformed_response_rejected", {
            let config = try AutoConnectionConfiguration(metadata)
            for value: Any in [NSNull(), [], ["Entry": "invalid"], ["Entry": [["wrong": "8.8.8.8/32"]]]] {
                var malformed = attributes(); malformed["Entries"] = value
                try fails { _ = try PrefixListGuard.validate(attributes: malformed, associations: associations, config: config) }
            }
            var missing = attributes(); missing.removeValue(forKey: "Entries")
            try fails { _ = try PrefixListGuard.validate(attributes: missing, associations: associations, config: config) }
        }),
        ("prefix_list_diagnostics_distinguish_missing_and_valid_empty", {
            let config = try AutoConnectionConfiguration(metadata)
            let empty = try PrefixListGuard.validate(attributes: attributes(), associations: associations, config: config)
            try require(empty.isEmpty, "documented empty Entry array must remain valid")
            func diagnostic(_ raw: JSONObject, _ linked: JSONObject = associations) throws -> String {
                do { _ = try PrefixListGuard.validate(attributes: raw, associations: linked, config: config); throw Failure.assertion("malformed response accepted") }
                catch let error as ConnectionFailure {
                    try require(error.code == "configuration", "diagnostic changed error category")
                    return error.message
                }
            }
            var missingContainer = attributes(); missingContainer.removeValue(forKey: "Entries")
            let missingMessage = try diagnostic(missingContainer)
            try require(missingMessage.contains("Entries") && missingMessage.contains("字段缺失"), "missing container reason unclear")
            var missingArray = attributes(); missingArray["Entries"] = [:] as JSONObject
            let missingArrayMessage = try diagnostic(missingArray)
            try require(missingArrayMessage.contains("Entry") && missingArrayMessage.contains("字段缺失"), "missing Entry array reason unclear")
            try require(missingMessage != missingArrayMessage, "different missing structures conflated")
            var nullArray = attributes(); nullArray["Entries"] = ["Entry": NSNull()]
            let nullMessage = try diagnostic(nullArray)
            try require(nullMessage.contains("空值"), "null Entry array reason unclear")
            var wrongType = attributes(); wrongType["Entries"] = ["Entry": "synthetic-sensitive-response-do-not-echo"]
            let typeMessage = try diagnostic(wrongType)
            try require(typeMessage.contains("字符串") && !typeMessage.contains("synthetic-sensitive"), "diagnostic echoes response payload")
            let cases: [(String, Any, String)] = [("PrefixListId", "pl-private-value", "ID"), ("PrefixListName", "private-name", "名称"), ("AddressFamily", "IPv6", "IPv4"), ("MaxEntries", 3, "容量")]
            var reasons = Set<String>()
            for (key, value, expected) in cases {
                var raw = attributes(); raw[key] = value
                let message = try diagnostic(raw)
                try require(message.contains(expected), "field mismatch reason unclear")
                try require(!message.contains("private"), "mismatch echoed raw value")
                reasons.insert(message)
            }
            try require(reasons.count == cases.count, "field mismatch reasons conflated")
            var paginated = associations; paginated["NextToken"] = "synthetic-private-page-token"
            let pagination = try diagnostic(attributes(), paginated)
            try require(pagination.contains("分页") && !pagination.contains("synthetic-private"), "pagination reason leaks or is unclear")
        }),
        ("verified_empty_list_omission_is_narrowly_supported", {
            let config = try AutoConnectionConfiguration(metadata)
            var emptyResponse = attributes(); emptyResponse.removeValue(forKey: "Entries")
            emptyResponse["RequestId"] = "synthetic-request"
            let empty = try PrefixListGuard.validate(attributes: emptyResponse, associations: associations, config: config)
            try require(empty.isEmpty, "verified service empty shape rejected")
            for requestID: Any in [NSNull(), "", " \n\t", 123] {
                var invalid = emptyResponse; invalid["RequestId"] = requestID
                try fails { _ = try PrefixListGuard.validate(attributes: invalid, associations: associations, config: config) }
            }
            var noRequestID = emptyResponse; noRequestID.removeValue(forKey: "RequestId")
            try fails { _ = try PrefixListGuard.validate(attributes: noRequestID, associations: associations, config: config) }
            for entryShape: Any in [NSNull(), [:] as JSONObject, ["Entry": NSNull()], ["Entry": "invalid"], [], "invalid"] {
                var invalid = emptyResponse; invalid["Entries"] = entryShape
                try fails { _ = try PrefixListGuard.validate(attributes: invalid, associations: associations, config: config) }
            }
            var wrongID = emptyResponse; wrongID["PrefixListId"] = "pl-unrelated123"
            try fails { _ = try PrefixListGuard.validate(attributes: wrongID, associations: associations, config: config) }
            try fails { _ = try PrefixListGuard.validate(attributes: emptyResponse, associations: [:], config: config) }
            for capacity: Any in [2.5, "2", true, NSNull()] {
                var invalid = emptyResponse; invalid["MaxEntries"] = capacity
                try fails { _ = try PrefixListGuard.validate(attributes: invalid, associations: associations, config: config) }
            }
            for token: Any in [NSNull(), 0, false, [], [:] as JSONObject, "next-page"] {
                var invalid = associations; invalid["NextToken"] = token
                try fails { _ = try PrefixListGuard.validate(attributes: emptyResponse, associations: invalid, config: config) }
            }
            var completePage = associations; completePage["NextToken"] = ""
            let complete = try PrefixListGuard.validate(attributes: emptyResponse, associations: completePage, config: config)
            try require(complete.isEmpty, "explicit complete pagination rejected")
        }),
        ("empty_compatibility_preserves_resource_checks_and_mutation_readback", {
            let config = try AutoConnectionConfiguration(metadata)
            let credentials = ConnectionCredentials(accessKeyID: "synthetic", accessKeySecret: "synthetic", binding: config.binding)
            func absentEntries() -> JSONObject {
                var value = attributes(); value.removeValue(forKey: "Entries"); value["RequestId"] = "synthetic-request"; return value
            }
            var mutated = false, actions: [String] = []
            let client = AlibabaPrefixClient(config: config, credentials: credentials, timeout: 15) { action, _ in
                actions.append(action)
                if action == "DescribePrefixListAttributes" { return mutated ? attributes(["8.8.8.8/32"]) : absentEntries() }
                if action == "DescribePrefixListAssociations" { return associations }
                mutated = true; return [:]
            }
            let result = try client.execute(operation: "add_candidate", cidr: "8.8.8.8/32")
            try require(result == ["8.8.8.8/32"], "actual added entry not returned")
            try require(actions == ["DescribePrefixListAttributes", "DescribePrefixListAssociations", "ModifyPrefixList", "DescribePrefixListAttributes", "DescribePrefixListAssociations"], "mutation omitted exact-resource readback")
            var writes = 0
            let wrongScope = AlibabaPrefixClient(config: config, credentials: credentials, timeout: 15) { action, _ in
                if action == "DescribePrefixListAttributes" { var wrong = absentEntries(); wrong["PrefixListId"] = "pl-unrelated123"; return wrong }
                if action == "DescribePrefixListAssociations" { return associations }
                writes += 1; return [:]
            }
            try fails { _ = try wrongScope.execute(operation: "add_candidate", cidr: "8.8.8.8/32") }
            try require(writes == 0, "missing Entries bypassed resource guard before writing")
            let delayed = AlibabaPrefixClient(config: config, credentials: credentials, timeout: 15) { action, _ in
                if action == "DescribePrefixListAttributes" { return absentEntries() }
                if action == "DescribePrefixListAssociations" { return associations }
                return [:]
            }
            let observed = try delayed.execute(operation: "add_candidate", cidr: "8.8.8.8/32")
            try require(observed.isEmpty, "helper invented candidate before service readback; manager must continue waiting")
        }),
        ("association_scope_and_pagination_fail_closed", {
            let config = try AutoConnectionConfiguration(metadata)
            var paginated = associations; paginated["NextToken"] = "synthetic-next"
            try fails { _ = try PrefixListGuard.validate(attributes: attributes(), associations: paginated, config: config) }
            for linked: [JSONObject] in [[], [["ResourceId": "sg-unrelated", "ResourceType": "securitygroup"]],
                [["ResourceId": "sg-synthetic123", "ResourceType": "securitygroup"], ["ResourceId": "sg-other123", "ResourceType": "securitygroup"]]] {
                try fails { _ = try PrefixListGuard.validate(attributes: attributes(), associations: ["PrefixListAssociations": ["PrefixListAssociation": linked]], config: config) }
            }
        }),
        ("acs3_signature_independent_golden", {
            // Independently calculated with Python hashlib/hmac per official ACS3 canonical format.
            let creds = ConnectionCredentials(accessKeyID: "synthetic-access-id", accessKeySecret: "synthetic-secret-for-unit-test", binding: "synthetic")
            let request = try AlibabaSigner.sign(action: "ModifyPrefixList", query: ["RegionId": "cn-shanghai", "PrefixListId": "pl-synthetic123", "AddEntry.1.Cidr": "8.8.8.8/32", "AddEntry.1.Description": "synthetic + 中文"], region: "cn-shanghai", credentials: creds, date: "2026-09-15T00:00:00Z", nonce: "synthetic-nonce")
            try require(request.value(forHTTPHeaderField: "Authorization")?.hasSuffix("Signature=517007a0c7c25fe2e73f5dfeba0d8386458c7cbdb1847401413bccd860527220") == true, "ACS3 golden signature differs")
            try require(request.url?.host == "ecs.cn-shanghai.aliyuncs.com" && request.url?.scheme == "https", "endpoint mismatch")
            try require(request.url?.absoluteString.contains("synthetic%20%2B%20%E4%B8%AD%E6%96%87") == true, "RFC3986 encoding incorrect")
            try require(request.url?.absoluteString.contains(creds.accessKeyID) == false && request.httpBody == nil, "credential in URL or body")
            try fails { _ = try AlibabaSigner.sign(action: "AuthorizeSecurityGroup", query: [:], region: "cn-shanghai", credentials: creds, date: "", nonce: "") }
        }),
        ("production_add_entry_obeys_vendor_description_contract", {
            let config = try AutoConnectionConfiguration(metadata)
            var entries: [String] = [], captured: [String: String] = [:], mutations = 0
            let client = AlibabaPrefixClient(config: config, credentials: ConnectionCredentials(accessKeyID: "synthetic", accessKeySecret: "synthetic", binding: config.binding), timeout: 15) { action, parameters in
                if action == "DescribePrefixListAttributes" { return attributes(entries) }
                if action == "DescribePrefixListAssociations" { return associations }
                try require(action == "ModifyPrefixList", "unexpected cloud operation")
                captured = parameters; mutations += 1
                entries = [parameters["AddEntry.1.Cidr"] ?? ""]
                return [:]
            }
            let observed = try client.execute(operation: "add_candidate", cidr: "8.8.8.8/32")
            let description = captured["AddEntry.1.Description"] ?? ""
            try require((2...32).contains(description.count), "production description violates official 2-32 character limit")
            try require(!description.hasPrefix("http://") && !description.hasPrefix("https://"), "production description has forbidden prefix")
            try require(captured["AddEntry.1.Cidr"] == "8.8.8.8/32" && Set(captured.keys) == ["AddEntry.1.Cidr", "AddEntry.1.Description"], "entry operation changed scope or added unrelated parameters")
            try require(config.resourceARN == "acs:ecs:cn-shanghai:1234567890123456:prefixlist/pl-synthetic123", "configured resource scope changed")
            try require(observed == ["8.8.8.8/32"] && mutations == 1, "successful mutation not read back exactly once")
        }),
        ("native_cloud_mutation_is_bounded_and_idempotent", {
            let config = try AutoConnectionConfiguration(metadata)
            var entries = ["8.8.8.8/32"], mutations = 0
            let client = AlibabaPrefixClient(config: config, credentials: ConnectionCredentials(accessKeyID: "synthetic", accessKeySecret: "synthetic", binding: config.binding), timeout: 15) { action, values in
                if action == "DescribePrefixListAttributes" { return attributes(entries) }
                if action == "DescribePrefixListAssociations" { return associations }
                mutations += 1
                if let cidr = values["AddEntry.1.Cidr"] { entries.append(cidr) }
                if let cidr = values["RemoveEntry.1.Cidr"] { entries.removeAll { $0 == cidr } }
                return [:]
            }
            _ = try client.execute(operation: "add_candidate", cidr: "8.8.8.8/32")
            try require(mutations == 0, "same entry mutated")
            _ = try client.execute(operation: "add_candidate", cidr: "1.1.1.1/32")
            try require(mutations == 1 && entries.count == 2, "candidate missing")
            try fails { _ = try client.execute(operation: "add_candidate", cidr: "9.9.9.9/32") }
            try require(mutations == 1, "capacity enlarged")
            _ = try client.execute(operation: "remove_candidate", cidr: "1.1.1.1/32")
            try require(entries == ["8.8.8.8/32"], "rollback damaged original address")
            try fails { _ = try client.execute(operation: "add_candidate", cidr: "0.0.0.0/0") }
            try fails { _ = try client.execute(operation: "arbitrary_api", cidr: nil) }
        }),
        ("native_cloud_checks_scope_before_write", {
            let config = try AutoConnectionConfiguration(metadata)
            var mutations = 0
            let client = AlibabaPrefixClient(config: config, credentials: ConnectionCredentials(accessKeyID: "synthetic", accessKeySecret: "synthetic", binding: ""), timeout: 15) { action, _ in
                if action == "DescribePrefixListAttributes" { return attributes() }
                if action == "DescribePrefixListAssociations" { return [:] }
                mutations += 1; return [:]
            }
            try fails { _ = try client.execute(operation: "add_candidate", cidr: "8.8.8.8/32") }
            try require(mutations == 0, "scope failure still wrote")
            let expired = AlibabaPrefixClient(config: config, credentials: ConnectionCredentials(accessKeyID: "synthetic", accessKeySecret: "synthetic", binding: ""), timeout: -1) { _, _ in mutations += 1; return [:] }
            try fails { _ = try expired.execute(operation: "inspect", cidr: nil) }
            try require(mutations == 0, "expired deadline still called API")
        }),
        ("open_connection_settings_do_not_need_keychain_or_cloud_fields", {
            let directory = FileManager.default.temporaryDirectory.appendingPathComponent("xhs-synthetic-" + UUID().uuidString)
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            defer { try? FileManager.default.removeItem(at: directory) }
            let path = directory.appendingPathComponent("connection.json").path
            let original: JSONObject = ["adb": ["local_serial": "127.0.0.1:6100"], "future_field": "preserve"]
            try ConnectionSettingsPersistence.atomicPrivateJSON(original, path: path)
            var draft = ConnectionSettingsDraft(root: original, deviceID: "synthetic")
            try require(draft.authorizationMode == "prefix_list", "legacy default changed")
            draft.authorizationMode = "open"
            try ConnectionSettingsPersistence.save(path: path, draft: draft, accessKeyID: "", secret: "")
            let saved = try ConnectionSettingsPersistence.load(path: path)
            try require(saved.object("auto_connection").string("authorization_mode") == "open" && saved.object("auto_connection").bool("enabled"), "open mode disabled automatic reconnect")
            try require(saved.object("auto_connection")["credentials_saved"] == nil && saved.object("auto_connection")["credential_ref"] == nil, "open save invented credentials")
            try require(saved.object("adb").string("local_serial") == "127.0.0.1:6100" && saved.string("future_field") == "preserve", "open save changed unrelated settings")
        }),
        ("open_settings_keep_previous_authorization_and_reject_secret_input", {
            let directory = FileManager.default.temporaryDirectory.appendingPathComponent("xhs-synthetic-" + UUID().uuidString)
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            defer { try? FileManager.default.removeItem(at: directory) }
            let path = directory.appendingPathComponent("connection.json").path
            var retained = metadata; retained["credentials_saved"] = true; retained["helper_path"] = "/synthetic/helper"
            let original: JSONObject = ["auto_connection": retained]
            try ConnectionSettingsPersistence.atomicPrivateJSON(original, path: path)
            var draft = ConnectionSettingsDraft(root: original, deviceID: "synthetic"); draft.authorizationMode = "open"
            draft.prefixListID = "unused-edit-must-not-overwrite-retained-value"
            for (id, secret) in [("synthetic-id", ""), ("", "synthetic-secret"), ("synthetic-id", "synthetic-secret")] {
                try fails { try ConnectionSettingsPersistence.save(path: path, draft: draft, accessKeyID: id, secret: secret) }
            }
            try ConnectionSettingsPersistence.save(path: path, draft: draft, accessKeyID: "", secret: "")
            let saved = try ConnectionSettingsPersistence.load(path: path).object("auto_connection")
            for (key, value) in retained {
                try require(NSDictionary(dictionary: [key: saved[key] as Any]).isEqual(to: [key: value]), "open mode destroyed retained metadata: " + key)
            }
            try require(saved.string("authorization_mode") == "open", "mode not persisted")
            let before = try Data(contentsOf: URL(fileURLWithPath: path))
            draft.authorizationMode = "invalid"
            try fails { try ConnectionSettingsPersistence.save(path: path, draft: draft, accessKeyID: "", secret: "") }
            let after = try Data(contentsOf: URL(fileURLWithPath: path))
            try require(before == after, "invalid mode changed saved metadata")
        }),
        ("disabled_connection_never_silently_discards_credentials", {
            let directory = FileManager.default.temporaryDirectory.appendingPathComponent("xhs-synthetic-" + UUID().uuidString)
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            defer { try? FileManager.default.removeItem(at: directory) }
            let path = directory.appendingPathComponent("connection.json").path
            let original: JSONObject = ["auto_connection": metadata, "future_field": "preserve"]
            try ConnectionSettingsPersistence.atomicPrivateJSON(original, path: path)
            let before = try Data(contentsOf: URL(fileURLWithPath: path))
            var draft = ConnectionSettingsDraft(root: original, deviceID: "synthetic"); draft.enabled = false
            for (id, secret) in [("synthetic-id", ""), ("", "synthetic-secret"), ("synthetic-id", "synthetic-secret")] {
                try fails { try ConnectionSettingsPersistence.save(path: path, draft: draft, accessKeyID: id, secret: secret) }
            }
            let afterRejected = try Data(contentsOf: URL(fileURLWithPath: path))
            try require(before == afterRejected, "disabled save changed metadata despite rejected credentials")
            try ConnectionSettingsPersistence.save(path: path, draft: draft, accessKeyID: "", secret: "")
            let saved = try ConnectionSettingsPersistence.load(path: path)
            try require(!saved.object("auto_connection").bool("enabled"), "disable was not persisted")
            try require(saved.object("auto_connection").string("credential_ref") == "xhs-mobile-synthetic" && saved.string("future_field") == "preserve", "disable destroyed existing metadata")
        }),
        ("private_connection_metadata_preserves_other_fields", {
            let directory = FileManager.default.temporaryDirectory.appendingPathComponent("xhs-synthetic-" + UUID().uuidString)
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            defer { try? FileManager.default.removeItem(at: directory) }
            let path = directory.appendingPathComponent("connection.json").path
            let original: JSONObject = ["adb": ["local_serial": "127.0.0.1:6100"], "future_field": "preserve", "auto_connection": metadata]
            try ConnectionSettingsPersistence.atomicPrivateJSON(original, path: path)
            let (loaded, _) = try AutoConnectionConfiguration.read(path)
            try require(loaded.string("future_field") == "preserve" && loaded.object("adb").string("local_serial") == "127.0.0.1:6100", "unrelated config lost")
            let mode = try FileManager.default.attributesOfItem(atPath: path)[.posixPermissions] as? NSNumber
            try require(mode?.intValue == 0o600, "metadata not private")
            try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: path)
            try fails { _ = try AutoConnectionConfiguration.read(path) }
        })
    ] }
}
