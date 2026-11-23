import Foundation
import Security
import SwiftUI

/// GET/POST /api/settings. Never carries a key: only whether one is there and where from.
struct ModelSettings: Decodable, Sendable {
    var pmModel: String?
    var pmFallbackModel: String?
    var judge: JudgeStatus
    var judgeConfig: [String: String]

    enum CodingKeys: String, CodingKey {
        case pmModel = "pm_model", pmFallbackModel = "pm_fallback_model", judge, judgeConfig = "judge_config"
    }
}

struct JudgeStatus: Decodable, Sendable {
    var backend: String
    var model: String
    var endpoint: String
    var keychainService: String
    var available: Bool
    var missing: String?
    var backends: [String: BackendStatus]

    enum CodingKeys: String, CodingKey {
        case backend, model, endpoint, keychainService = "keychain_service", available, missing, backends
    }
}

struct BackendStatus: Decodable, Sendable {
    var model: String
    var endpoint: String
    var configured: Bool
    /// "keychain", "env" or "secrets"; nil when there is no key.
    var source: String?
    var problem: String?
}

enum JudgeBackend: String, CaseIterable, Sendable {
    case systemone, openai
}

/// The judge's key in the login keychain: service from config.json (default `docket.judge`), account = backend name.
///
/// Written through `/usr/bin/security -i` with the key on stdin as hex, never in argv. An item added
/// with SecItemAdd belongs to this app's partition, and the daemon, which reads it with `security`
/// from launchd, would get an access prompt it cannot answer. The check below only reads attributes,
/// which never prompts.
enum JudgeKeychain {
    enum Failure: Error {
        case badName
        case launch
        case status(Int32)
    }

    static func save(service: String, account: String, key: String) throws {
        let unsafe = CharacterSet(charactersIn: "\"\\\n\r")
        guard !service.isEmpty, service.rangeOfCharacter(from: unsafe) == nil,
              account.rangeOfCharacter(from: unsafe) == nil else { throw Failure.badName }
        let hex = key.utf8.map { String(format: "%02x", $0) }.joined()
        let command = "add-generic-password -U -s \"\(service)\" -a \"\(account)\" -l \"Docket judge (\(account))\" -X \(hex)\n"

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/security")
        process.arguments = ["-i"]
        let input = Pipe()
        process.standardInput = input
        // Its output is a prompt or a usage text, never the key; nothing here needs it.
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
        } catch {
            throw Failure.launch
        }
        input.fileHandleForWriting.write(Data(command.utf8))
        try? input.fileHandleForWriting.close()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else { throw Failure.status(process.terminationStatus) }
    }

    /// Whether the item exists, through the Security framework, attributes only.
    static func exists(service: String, account: String) -> Bool {
        let query: [CFString: Any] = [
            kSecClass: kSecClassGenericPassword,
            kSecAttrService: service,
            kSecAttrAccount: account,
            kSecReturnAttributes: true,
            kSecMatchLimit: kSecMatchLimitOne,
        ]
        return SecItemCopyMatching(query as CFDictionary, nil) == errSecSuccess
    }
}

/// The settings window's Models group: edits config.json through the daemon, the key through the keychain.
@MainActor
final class ModelSettingsModel: ObservableObject {
    @Published private(set) var loaded: ModelSettings?
    @Published private(set) var reachable = true
    @Published var pmModel = ""
    @Published var pmFallback = ""
    @Published var backend: JudgeBackend = .systemone {
        didSet {
            // Another backend's model and endpoint mean nothing here: fall back to its defaults.
            guard oldValue != backend else { return }
            judgeModel = ""
            endpoint = ""
        }
    }
    @Published var judgeModel = ""
    @Published var endpoint = ""
    @Published var key = ""
    @Published private(set) var message: (text: String, isError: Bool)?
    @Published private(set) var keyMessage: (text: String, isError: Bool)?
    @Published private(set) var working = false

    private let client = DaemonClient()

    var dirty: Bool {
        guard let loaded else { return false }
        return pmModel != (loaded.pmModel ?? "")
            || pmFallback != (loaded.pmFallbackModel ?? "")
            || backend.rawValue != loaded.judge.backend
            || judgeModel != (loaded.judgeConfig["model"] ?? "")
            || endpoint != (loaded.judgeConfig["endpoint"] ?? "")
    }

    var keychainService: String { loaded?.judge.keychainService ?? "docket.judge" }

    func status(for backend: JudgeBackend) -> BackendStatus? {
        loaded?.judge.backends[backend.rawValue]
    }

    func load() async {
        do {
            apply(try await client.fetchModelSettings())
            reachable = true
        } catch {
            reachable = false
            loaded = nil
        }
    }

    func save(_ s: Strings) async {
        working = true
        defer { working = false }
        var judge: [String: Any] = ["backend": backend.rawValue, "model": judgeModel, "endpoint": endpoint]
        if let service = loaded?.judgeConfig["keychain_service"] { judge["keychain_service"] = service }
        let body: [String: Any] = ["pm_model": pmModel, "pm_fallback_model": pmFallback, "judge": judge]
        do {
            apply(try await client.saveModelSettings(body))
            message = (s(.modelsSaved), false)
        } catch let error as DocketError {
            message = (error.message(s), true)
        } catch {
            message = (s(.unreadableResponse), true)
        }
    }

    func saveKey(_ s: Strings) async {
        let key = self.key.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !key.isEmpty else { return }
        let service = keychainService, account = backend.rawValue
        working = true
        defer { working = false }
        do {
            try await Task.detached { try JudgeKeychain.save(service: service, account: account, key: key) }.value
            self.key = ""
            keyMessage = (s(.keySaved), false)
        } catch JudgeKeychain.Failure.status(let code) {
            keyMessage = (s(.keySaveFailed, Int(code)), true)
        } catch {
            keyMessage = (s(.keySaveFailedLaunch), true)
        }
        // The daemon rereads the key when asked for its settings, so the judge uses it from now on.
        if let fresh = try? await client.fetchModelSettings() {
            loaded = fresh
        }
    }

    private func apply(_ settings: ModelSettings) {
        loaded = settings
        pmModel = settings.pmModel ?? ""
        pmFallback = settings.pmFallbackModel ?? ""
        backend = JudgeBackend(rawValue: settings.judge.backend) ?? .systemone
        judgeModel = settings.judgeConfig["model"] ?? ""
        endpoint = settings.judgeConfig["endpoint"] ?? ""
    }
}

struct ModelsSection: View {
    let s: Strings
    @StateObject private var model = ModelSettingsModel()

    var body: some View {
        Section(s(.settingsModels)) {
            if !model.reachable {
                Label(s(.modelsNeedDaemon), systemImage: "exclamationmark.circle")
                    .foregroundStyle(Color.orange)
            }
            Group {
                Text(s(.pmModelHeading)).font(.headline)
                TextField(s(.pmModel), text: $model.pmModel, prompt: Text(s(.pmModelPlaceholder)))
                TextField(s(.pmFallback), text: $model.pmFallback, prompt: Text(s(.pmFallbackPlaceholder)))
                note(s(.pmCredentials))

                Text(s(.judgeHeading)).font(.headline)
                Picker(s(.judgeBackend), selection: $model.backend) {
                    Text(s(.backendSystemOne)).tag(JudgeBackend.systemone)
                    Text(s(.backendOpenAI)).tag(JudgeBackend.openai)
                }
                let defaults = model.status(for: model.backend)
                TextField(s(.judgeModel), text: $model.judgeModel,
                          prompt: Text(placeholder(defaults?.model)))
                TextField(s(.judgeEndpoint), text: $model.endpoint,
                          prompt: Text(placeholder(defaults?.endpoint, fallback: s(.endpointPlaceholder))))
                HStack {
                    SecureField(s(.judgeKey), text: $model.key,
                                prompt: Text(s(defaults?.configured == true ? .keyConfigured : .keyMissingPlaceholder)))
                    Button(s(.saveKey)) { Task { await model.saveKey(s) } }
                        .disabled(model.key.trimmingCharacters(in: .whitespaces).isEmpty || model.working)
                }
                note(keyStatus(defaults))
                if let message = model.keyMessage {
                    note(message.text, error: message.isError)
                }
                if let judge = model.loaded?.judge, !judge.available, judge.backend == model.backend.rawValue {
                    note(s(.judgeOff, missing(judge.missing)), error: true)
                    note(s(.judgeTwoWays))
                }
                HStack {
                    if let message = model.message {
                        note(message.text, error: message.isError)
                    }
                    Spacer()
                    Button(s(.applyModels)) { Task { await model.save(s) } }
                        .disabled(!model.dirty || model.working)
                }
            }
            .disabled(!model.reachable)
        }
        .task { await model.load() }
    }

    private func placeholder(_ value: String?, fallback: String = "") -> String {
        guard let value, !value.isEmpty else { return fallback }
        return value
    }

    private func keyStatus(_ status: BackendStatus?) -> String {
        guard let status, status.configured else {
            if JudgeKeychain.exists(service: model.keychainService, account: model.backend.rawValue) {
                return s(.keyFrom, s(.sourceKeychain))
            }
            return s(.keyNone)
        }
        let source: String = switch status.source {
        case "keychain": s(.sourceKeychain)
        case "env": s(.sourceEnv)
        case "secrets": s(.sourceSecrets)
        default: status.source ?? "-"
        }
        return s(.keyFrom, source)
    }

    private func missing(_ reason: String?) -> String {
        switch reason {
        case "no endpoint": s(.missingEndpoint)
        case "no model": s(.missingModel)
        default: s(.missingKey)
        }
    }

    private func note(_ text: String, error: Bool = false) -> some View {
        Text(text)
            .font(.callout)
            .foregroundStyle(error ? Color.orange : Color.secondary)
            .fixedSize(horizontal: false, vertical: true)
    }
}
