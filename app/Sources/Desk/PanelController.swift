import AppKit
import SwiftUI

enum PanelMetrics {
    static let pillSize = NSSize(width: 34, height: 120)
    static let expandedSize = NSSize(width: 380, height: 640)
    static let pillRadius: CGFloat = 17
    static let expandedRadius: CGFloat = 16
    static let pillInset: CGFloat = 4
    static let expandedInset: CGFloat = 10
}

final class DeskPanel: NSPanel {
    // Borderless windows refuse key status by default; the reply field needs it.
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

final class FirstMouseHostingView<Content: View>: NSHostingView<Content> {
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}

@MainActor
final class PanelController {
    private let store: Store
    private let panel: DeskPanel
    private let effect = NSVisualEffectView()
    private var appliedExpanded: Bool?
    private var keyMonitor: Any?
    private var screenObserver: NSObjectProtocol?

    init(store: Store) {
        self.store = store
        panel = DeskPanel(
            contentRect: NSRect(origin: .zero, size: PanelMetrics.pillSize),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        // Collapsed, the strip is an ordinary window in one space: other apps' windows cover it,
        // and it does not follow into full-screen spaces. apply() makes it a floating,
        // all-spaces window only while it is expanded. (.canJoinAllSpaces alone is enough to
        // keep a normal-level window on top of a full-screen app, so it is off when collapsed.)
        panel.isFloatingPanel = false
        panel.level = .normal
        panel.collectionBehavior = [.ignoresCycle]
        panel.hidesOnDeactivate = false
        // Only take key status when a control needs it (the reply field), so we never steal focus.
        panel.becomesKeyOnlyIfNeeded = true
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.isMovable = false
        panel.isReleasedWhenClosed = false
        panel.animationBehavior = .none

        effect.material = .popover
        effect.blendingMode = .behindWindow
        effect.state = .active
        // Screenshots of a single window lose what the material blends with; draw it solid instead.
        if UserDefaults.standard.bool(forKey: "debugOpaque") {
            effect.material = .windowBackground
            effect.blendingMode = .withinWindow
            effect.wantsLayer = true
            effect.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
        }

        let hosting = FirstMouseHostingView(rootView: RootView(store: store))
        hosting.sizingOptions = []
        hosting.frame = effect.bounds
        hosting.autoresizingMask = [.width, .height]
        effect.addSubview(hosting)
        panel.contentView = effect

        store.observe { [weak self] in self?.apply() }

        screenObserver = NotificationCenter.default.addObserver(
            forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated {
                self?.appliedExpanded = nil
                self?.apply()
            }
        }

        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard event.keyCode == 53 else { return event } // Esc
            let handled = MainActor.assumeIsolated { () -> Bool in
                guard let self, self.store.expanded else { return false }
                self.store.expanded = false
                return true
            }
            return handled ? nil : event
        }
    }

    func show() {
        apply()
    }

    private func apply() {
        let expanded = store.expanded
        let summoned = appliedExpanded != expanded
        if summoned {
            appliedExpanded = expanded
            effect.maskImage = Self.mask(radius: expanded ? PanelMetrics.expandedRadius : PanelMetrics.pillRadius)
            panel.setFrame(Self.frame(expanded: expanded), display: true)
            panel.invalidateShadow()
            // Expanded is the summoned state: float above ordinary windows on every space,
            // full-screen apps included, so the hotkey works from anywhere. Collapsed drops back
            // to an ordinary, coverable window in a single space.
            panel.level = expanded ? .floating : .normal
            panel.collectionBehavior = expanded
                ? [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
                : [.ignoresCycle]
        }
        if !panel.isVisible {
            panel.orderFrontRegardless()
        } else if summoned && expanded {
            // Put it in front and give it the keyboard without activating the app, so the reply
            // field types straight away and later polling (which lands here without `summoned`)
            // cannot pull focus back.
            panel.orderFrontRegardless()
            panel.makeKey()
        }
    }

    private static func frame(expanded: Bool) -> NSRect {
        let screen = NSScreen.screens.first ?? NSScreen.main
        let visible = screen?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let size = expanded ? PanelMetrics.expandedSize : PanelMetrics.pillSize
        let inset = expanded ? PanelMetrics.expandedInset : PanelMetrics.pillInset
        let height = min(size.height, visible.height - 2 * inset)
        return NSRect(
            x: visible.maxX - size.width - inset,
            y: (visible.midY - height / 2).rounded(),
            width: size.width,
            height: height
        )
    }

    private static func mask(radius: CGFloat) -> NSImage {
        let edge = radius * 2 + 1
        let image = NSImage(size: NSSize(width: edge, height: edge), flipped: false) { rect in
            NSColor.black.setFill()
            NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius).fill()
            return true
        }
        image.capInsets = NSEdgeInsets(top: radius, left: radius, bottom: radius, right: radius)
        image.resizingMode = .stretch
        return image
    }
}
