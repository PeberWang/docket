import AppKit
import Carbon.HIToolbox

/// A single global hotkey via Carbon `RegisterEventHotKey` (no accessibility permission needed).
@MainActor
final class HotKey {
    nonisolated(unsafe) private static var action: (@MainActor () -> Void)?

    private var hotKeyRef: EventHotKeyRef?
    private var handlerRef: EventHandlerRef?

    init(keyCode: UInt32, modifiers: UInt32, action: @escaping @MainActor () -> Void) {
        HotKey.action = action

        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), hotKeyCallback, 1, &spec, nil, &handlerRef)

        let id = EventHotKeyID(signature: OSType(0x444F_434B), id: 1) // "DOCK"
        let status = RegisterEventHotKey(keyCode, modifiers, id, GetApplicationEventTarget(), 0, &hotKeyRef)
        if status != noErr {
            NSLog("Docket: global hotkey unavailable (status \(status))")
        }
    }

    fileprivate static func fire() {
        action?()
    }
}

private func hotKeyCallback(_: EventHandlerCallRef?, _: EventRef?, _: UnsafeMutableRawPointer?) -> OSStatus {
    // Carbon delivers application-target events on the main thread.
    MainActor.assumeIsolated { HotKey.fire() }
    return noErr
}
