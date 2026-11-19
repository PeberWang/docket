import AppKit
import SwiftUI

/// The app has no main menu (it is an accessory), so ⌘W has to be handled by the window itself.
final class SettingsWindow: NSWindow {
    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        if flags == .command, event.charactersIgnoringModifiers == "w" {
            performClose(nil)
            return true
        }
        return super.performKeyEquivalent(with: event)
    }
}

/// One settings window, created on first use and reused after it is closed.
@MainActor
final class SettingsWindowController {
    private let store: Store
    private var window: SettingsWindow?

    init(store: Store) {
        self.store = store
        store.observe { [weak self] in self?.updateTitle() }
    }

    func show() {
        let window = self.window ?? makeWindow()
        if !window.isVisible { window.center() }
        NSApp.activate()
        window.makeKeyAndOrderFront(nil)
        // Activation is only a request since macOS 14; without this the window can open behind the active app.
        window.orderFrontRegardless()
    }

    private func makeWindow() -> SettingsWindow {
        // The hosting controller hands the SwiftUI size to the window; a bare hosting view
        // has no size before it is in a window, which left the window zero-width.
        let hosting = NSHostingController(rootView: SettingsView(store: store))
        hosting.sizingOptions = [.preferredContentSize]
        let window = SettingsWindow(
            contentRect: NSRect(origin: .zero, size: NSSize(width: 480, height: 160)),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        window.contentViewController = hosting
        window.isReleasedWhenClosed = false
        window.collectionBehavior = [.moveToActiveSpace]
        self.window = window
        updateTitle()
        return window
    }

    private func updateTitle() {
        guard let window else { return }
        let title = store.s(.settingsTitle)
        if window.title != title { window.title = title }
    }
}
