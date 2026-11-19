import AppKit

@MainActor
final class StatusItemController: NSObject {
    private let store: Store
    private let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private var symbolName: String?
    private let settingsWindow: SettingsWindowController

    init(store: Store) {
        self.store = store
        settingsWindow = SettingsWindowController(store: store)
        super.init()
        if let button = item.button {
            button.target = self
            button.action = #selector(clicked(_:))
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }
        update()
        store.observe { [weak self] in self?.update() }
    }

    private func update() {
        guard let button = item.button else { return }
        let s = store.s
        let name = store.hidden ? "line.3.horizontal" : (store.awaiting > 0 ? "tray.full" : "tray")
        if name != symbolName {
            symbolName = name
            let config = NSImage.SymbolConfiguration(pointSize: 14, weight: .regular)
            let image = NSImage(systemSymbolName: name, accessibilityDescription: s(.panelTitle))?
                .withSymbolConfiguration(config)
            image?.isTemplate = true
            button.image = image
        }
        // Hidden means quiet: no count on the icon until the strip comes back.
        let count = store.hidden ? 0 : store.awaiting
        let title = count > 0 ? "\(count)" : ""
        if button.title != title { button.title = title }
        button.imagePosition = title.isEmpty ? .imageOnly : .imageLeading
        if store.hidden {
            button.toolTip = s(.panelHidden)
        } else {
            button.toolTip = store.connection == .down ? s(.serviceNotRunning) : s(.statusTooltip)
        }
        button.setAccessibilityLabel(count > 0 ? "\(s(.panelTitle)), \(s.needYou(count))" : s(.panelTitle))
    }

    @objc private func clicked(_ sender: NSStatusBarButton) {
        let event = NSApp.currentEvent
        if event?.type == .rightMouseUp || event?.modifierFlags.contains(.control) == true {
            showMenu()
        } else {
            store.toggle()
        }
    }

    private func showMenu() {
        let s = store.s
        let menu = NSMenu()
        let toggle = NSMenuItem(title: store.expanded ? s(.hideDocket) : s(.showDocket), action: #selector(togglePanel), keyEquivalent: "d")
        toggle.keyEquivalentModifierMask = [.command, .option]
        toggle.target = self
        menu.addItem(toggle)
        let panelItem = NSMenuItem(title: store.hidden ? s(.showPanel) : s(.hidePanel), action: #selector(toggleHiddenPanel), keyEquivalent: "")
        panelItem.target = self
        menu.addItem(panelItem)
        let settings = NSMenuItem(title: s(.settings), action: #selector(openSettings), keyEquivalent: ",")
        settings.keyEquivalentModifierMask = [.command]
        settings.target = self
        menu.addItem(settings)
        menu.addItem(.separator())
        let quit = NSMenuItem(title: s(.quit), action: #selector(quit), keyEquivalent: "")
        quit.target = self
        menu.addItem(quit)

        item.menu = menu
        item.button?.performClick(nil)
        item.menu = nil
    }

    @objc private func togglePanel() {
        store.toggle()
    }

    @objc private func toggleHiddenPanel() {
        store.toggleHidden()
    }

    @objc private func openSettings() {
        settingsWindow.show()
    }

    @objc private func quit() {
        NSApp.terminate(nil)
    }
}
