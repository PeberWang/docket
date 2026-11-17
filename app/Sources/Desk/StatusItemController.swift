import AppKit

@MainActor
final class StatusItemController: NSObject {
    private let store: Store
    private let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private var symbolName: String?

    init(store: Store) {
        self.store = store
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
        let name = store.awaiting > 0 ? "tray.full" : "tray"
        if name != symbolName {
            symbolName = name
            let config = NSImage.SymbolConfiguration(pointSize: 14, weight: .regular)
            let image = NSImage(systemSymbolName: name, accessibilityDescription: s(.panelTitle))?
                .withSymbolConfiguration(config)
            image?.isTemplate = true
            button.image = image
        }
        let title = store.awaiting > 0 ? "\(store.awaiting)" : ""
        if button.title != title { button.title = title }
        button.imagePosition = title.isEmpty ? .imageOnly : .imageLeading
        button.toolTip = store.connection == .down ? s(.serviceNotRunning) : s(.statusTooltip)
        button.setAccessibilityLabel(store.awaiting > 0 ? "\(s(.panelTitle)), \(s.needYou(store.awaiting))" : s(.panelTitle))
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
        let toggle = NSMenuItem(title: store.expanded ? s(.hideDesk) : s(.showDesk), action: #selector(togglePanel), keyEquivalent: "d")
        toggle.keyEquivalentModifierMask = [.command, .option]
        toggle.target = self
        menu.addItem(toggle)
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

    @objc private func quit() {
        NSApp.terminate(nil)
    }
}
