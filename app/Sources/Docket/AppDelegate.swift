import AppKit
import Carbon.HIToolbox

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private let store = Store()
    private var panel: PanelController?
    private var statusItem: StatusItemController?
    private var hotKey: HotKey?
    private var debugSettings: SettingsWindowController?

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Always start collapsed. The debug default exists only for screenshots during development.
        if UserDefaults.standard.bool(forKey: "debugStartExpanded") {
            store.expanded = true
        }
        panel = PanelController(store: store)
        statusItem = StatusItemController(store: store)
        hotKey = HotKey(keyCode: UInt32(kVK_ANSI_D), modifiers: UInt32(cmdKey | optionKey)) { [weak self] in
            self?.store.toggle()
        }
        panel?.show()
        store.start()
        // Debug only: open the settings window without going through the status-item menu.
        if UserDefaults.standard.bool(forKey: "debugOpenSettings") {
            let controller = SettingsWindowController(store: store)
            debugSettings = controller
            controller.show()
        }
    }
}
