import AppKit

enum SingleInstance {
    /// Another running copy of this bundle, if there is one. Without a bundle id (a bare `swift run`) there is nothing to match.
    @MainActor
    static func otherInstance() -> NSRunningApplication? {
        guard let id = Bundle.main.bundleIdentifier else { return nil }
        let me = ProcessInfo.processInfo.processIdentifier
        return NSRunningApplication.runningApplications(withBundleIdentifier: id)
            .first { $0.processIdentifier != me && !$0.isTerminated }
    }
}
