import AppKit

/// OpenCode permission requests are answered in the `opencode-permission-notify` popup, never on the Desk.
/// The Desk only checks whether that popup is installed, so it can say so once when it is not.
enum PermissionPopup {
    static let package = "opencode-permission-notify"
    static let snippet = "\"plugins\": [\"opencode-permission-notify\"]"

    enum Status {
        /// Listed in the OpenCode config, and its package or native helper is on disk.
        case installed
        /// Not listed in the config, or listed but nothing on disk. Doubt counts as not installed.
        case missing
    }

    static func detect() -> Status {
        configured() && onDisk() ? .installed : .missing
    }

    private static var home: URL { FileManager.default.homeDirectoryForCurrentUser }

    /// The plugin name on any line that is not a `//` comment of the user's OpenCode config.
    private static func configured() -> Bool {
        let dir = home.appendingPathComponent(".config/opencode")
        for name in ["opencode.jsonc", "opencode.json"] {
            guard let text = try? String(contentsOf: dir.appendingPathComponent(name), encoding: .utf8) else { continue }
            let listed = text.split(whereSeparator: \.isNewline).contains { line in
                let trimmed = line.trimmingCharacters(in: .whitespaces)
                return !trimmed.hasPrefix("//") && trimmed.contains("\"\(package)")
            }
            if listed { return true }
        }
        return false
    }

    /// The native helper the plugin installs, or the npm package OpenCode fetched.
    private static func onDisk() -> Bool {
        let fm = FileManager.default
        let helper = home.appendingPathComponent(".local/share/opencode/permission-notify/notify-request").path
        if fm.isExecutableFile(atPath: helper) { return true }
        let modules = [".cache/opencode/node_modules", ".config/opencode/node_modules"]
        if modules.contains(where: { fm.fileExists(atPath: home.appendingPathComponent("\($0)/\(package)").path) }) { return true }
        let packages = home.appendingPathComponent(".cache/opencode/packages")
        let entries = (try? fm.contentsOfDirectory(atPath: packages.path)) ?? []
        return entries.contains { $0.contains(package) }
    }

    static func copySnippet() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(snippet, forType: .string)
    }
}

extension Card {
    /// The daemon's card for an OpenCode permission request: one "permission" decision, allow once or reject.
    var isPermissionRequest: Bool {
        guard decisions.count == 1, let decision = decisions.first, decision.id == "permission" else { return false }
        return Set(decision.options.map(\.id)) == ["once", "reject"]
    }
}
