import AppKit

/// Opens permission-notify's tier settings window (`bin/permission-settings`), a separate native program.
/// Candidates are tried in order; the first executable one wins. Override with
/// `defaults write io.github.peberwang.docket permissionSettingsPaths -array <path> …`
/// (a single string also works). `~` and `$HOME` are expanded.
@MainActor
enum PermissionSettingsLauncher {
    static let defaultsKey = "permissionSettingsPaths"

    static let defaultPaths = [
        "~/Library/Application Support/opencode/permission-notify/bin/permission-settings",
    ]

    static var candidates: [String] {
        let defaults = UserDefaults.standard
        let configured: [String]
        if let list = defaults.stringArray(forKey: defaultsKey) {
            configured = list
        } else if let single = defaults.string(forKey: defaultsKey) {
            configured = [single]
        } else {
            configured = []
        }
        let paths = configured.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
        return (paths.isEmpty ? defaultPaths : paths).map(expand)
    }

    static func expand(_ path: String) -> String {
        let home = NSHomeDirectory()
        var p = path
        if p == "~" || p.hasPrefix("~/") { p = home + p.dropFirst() }
        p = p.replacingOccurrences(of: "${HOME}", with: home)
        p = p.replacingOccurrences(of: "$HOME", with: home)
        return p
    }

    private static var running: Process?

    /// Spawns the settings window and returns at once; on failure tells the user which paths were tried.
    /// A window already open is brought forward instead of opening a second one on the same settings file.
    /// A copy still starting (e.g. held at macOS's folder-access prompt) is left alone.
    static func open(strings s: Strings) {
        if let process = running, process.isRunning {
            NSRunningApplication(processIdentifier: process.processIdentifier)?.activate()
            return
        }
        let paths = candidates
        guard let path = paths.first(where: { FileManager.default.isExecutableFile(atPath: $0) }) else {
            alert(s(.permissionSettingsMissing), detail: paths.joined(separator: "\n\n"), strings: s)
            return
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: path)
        // The window shows in the Dock while it is open: with Docket's icon, not a bare program's.
        if let icon = Bundle.main.path(forResource: "Docket", ofType: "icns") {
            process.arguments = ["--icon", icon]
        }
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
            running = process
        } catch {
            alert(s(.permissionSettingsFailed), detail: "\(path)\n\(error.localizedDescription)", strings: s)
        }
    }

    private static func alert(_ message: String, detail: String, strings s: Strings) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = message
        alert.informativeText = detail
        alert.addButton(withTitle: s(.ok))
        // An accessory app's alert would open behind whatever is frontmost.
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
    }
}
