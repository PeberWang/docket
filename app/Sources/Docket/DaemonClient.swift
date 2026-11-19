import Foundation

enum DeskError: Error {
    case notRunning
    case unreadable
    case status(Int)
    /// Message from the daemon's `{"error": …}` body, shown as is.
    case server(String)

    func message(_ s: Strings) -> String {
        switch self {
        case .notRunning: s(.serviceNotRunning)
        case .unreadable: s(.unreadableResponse)
        case .status(let code): s(.serviceReturned, code)
        case .server(let text): text
        }
    }
}

/// Talks to the local daemon. Discovery file is re-read whenever a request fails,
/// because the port and token change when the daemon restarts.
@MainActor
final class DaemonClient {
    static let discoveryFile: URL = {
        // Development only: point at a dev-daemon started with --discovery.
        if let path = UserDefaults.standard.string(forKey: "debugDiscoveryFile"), !path.isEmpty {
            return URL(fileURLWithPath: (path as NSString).expandingTildeInPath)
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/docket/daemon.json")
    }()

    private var info: DaemonInfo?
    private let session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 4
        config.timeoutIntervalForResource = 8
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.connectionProxyDictionary = [:]
        return URLSession(configuration: config)
    }()

    func fetchState() async throws -> DeskState {
        let data = try await send("GET", "/api/state")
        do {
            return try JSONDecoder().decode(DeskState.self, from: data)
        } catch {
            throw DeskError.unreadable
        }
    }

    func decide(cardID: Int, choices: [String: String], reply: String) async throws {
        var body: [String: Any] = ["choices": choices]
        if !reply.isEmpty { body["reply"] = reply }
        _ = try await send("POST", "/api/cards/\(cardID)/decide", json: body)
    }

    func close(delegationID: String, reply: String) async throws {
        var body: [String: Any] = [:]
        if !reply.isEmpty { body["reply"] = reply }
        _ = try await send("POST", "/api/delegations/\(escaped(delegationID))/close", json: body)
    }

    func takeover(delegationID: String) async throws {
        _ = try await send("POST", "/api/delegations/\(escaped(delegationID))/takeover", json: [:])
    }

    private func escaped(_ id: String) -> String {
        id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed.subtracting(CharacterSet(charactersIn: "/"))) ?? id
    }

    private static func readInfo() -> DaemonInfo? {
        guard let data = try? Data(contentsOf: discoveryFile) else { return nil }
        return try? JSONDecoder().decode(DaemonInfo.self, from: data)
    }

    private func send(_ method: String, _ path: String, json: [String: Any]? = nil) async throws -> Data {
        let body = try json.map { try JSONSerialization.data(withJSONObject: $0) }

        for attempt in 0..<2 {
            if info == nil || attempt > 0 { info = Self.readInfo() }
            guard let info else { throw DeskError.notRunning }
            let base = info.url.hasSuffix("/") ? String(info.url.dropLast()) : info.url
            guard let url = URL(string: base + path) else { throw DeskError.notRunning }

            var request = URLRequest(url: url)
            request.httpMethod = method
            request.setValue(info.token, forHTTPHeaderField: "X-Desk-Token")
            if let body {
                request.httpBody = body
                request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            }

            let result: (Data, URLResponse)
            do {
                result = try await session.data(for: request)
            } catch {
                if attempt == 0 { continue }
                throw DeskError.notRunning
            }

            let (data, response) = result
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            if (200..<300).contains(status) { return data }
            // A stale token means the daemon restarted; re-read discovery once.
            if status == 401, attempt == 0 { continue }
            if let body = try? JSONDecoder().decode(ErrorBody.self, from: data) {
                throw DeskError.server(body.error)
            }
            throw DeskError.status(status)
        }
        throw DeskError.notRunning
    }
}
