import AppKit

MainActor.assumeIsolated {
    // One Docket at a time: if another copy of this bundle is already running, bring it forward and leave.
    // Info.plist's LSMultipleInstancesProhibited covers launches through Launch Services; this covers the rest.
    if let other = SingleInstance.otherInstance() {
        other.activate()
        exit(0)
    }
    let app = NSApplication.shared
    let delegate = AppDelegate()
    app.delegate = delegate
    app.setActivationPolicy(.accessory)
    app.run()
}
