import Foundation
import SwiftUI

enum Connection {
    case unknown, connected, down
}

struct SentAnswer {
    var choices: [String: String]
    var reply: String
}

@MainActor
final class Store: ObservableObject {
    @Published var expanded = false {
        didSet {
            guard oldValue != expanded else { return }
            notify()
            restartPolling()
        }
    }
    /// Whether the strip is withdrawn from the screen entirely. Persisted under `uiPanelHidden`
    /// so it stays hidden across launches; the menu bar item remains either way.
    @Published var hidden = UserDefaults.standard.bool(forKey: "uiPanelHidden") {
        didSet {
            guard oldValue != hidden else { return }
            UserDefaults.standard.set(hidden, forKey: "uiPanelHidden")
            notify()
        }
    }
    @Published private(set) var delegations: [Delegation] = []
    @Published private(set) var awaiting = 0
    @Published private(set) var connection: Connection = .unknown
    @Published private(set) var lang: Lang = LanguagePreference.saved.resolve(daemon: nil)
    @Published var languagePreference = LanguagePreference.saved {
        didSet {
            guard oldValue != languagePreference else { return }
            languagePreference.save()
            applyLang()
            notify()
        }
    }
    /// The daemon's last `locale`, nil while it is down or silent.
    private var daemonLang: Lang?

    var s: Strings { Strings(lang: lang) }

    private func applyLang() {
        lang = languagePreference.resolve(daemon: daemonLang)
    }

    /// cardID → decisionID → optionID
    @Published private(set) var selections: [Int: [String: String]] = [:]
    /// delegationID → draft reply text
    @Published var drafts: [String: String] = [:]
    @Published private(set) var busy: Set<String> = []
    @Published private(set) var errors: [String: DeskError] = [:]
    /// What the user sent, kept locally so answered cards can show it.
    @Published private(set) var sentAnswers: [Int: SentAnswer] = [:]

    /// Whether the permission popup is installed; nil until first checked.
    @Published private(set) var popupStatus: PermissionPopup.Status?
    /// A permission request came in and the popup is missing. Shown once per launch, until dismissed.
    @Published private(set) var showPopupNotice = false
    private var popupNoticeDismissed = false
    private var seenPermissionCards: Set<Int> = []

    private let client = DaemonClient()
    private var pollTask: Task<Void, Never>?
    private var refreshSeq = 0
    private var listeners: [@MainActor () -> Void] = []

    func observe(_ listener: @escaping @MainActor () -> Void) {
        listeners.append(listener)
    }

    private func notify() {
        listeners.forEach { $0() }
    }

    func start() {
        restartPolling()
    }

    func toggle() {
        if hidden { hidden = false } else { expanded.toggle() }
    }

    /// Collapse first, then withdraw the strip, so hiding never leaves a half-open panel.
    func hide() {
        if expanded { expanded = false }
        hidden = true
    }

    func toggleHidden() {
        if hidden { hidden = false } else { hide() }
    }

    // MARK: Polling

    private func restartPolling() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                let seconds: Double = (self?.expanded ?? false) ? 2 : 10
                try? await Task.sleep(for: .seconds(seconds))
            }
        }
    }

    func refresh() async {
        refreshSeq += 1
        let seq = refreshSeq
        do {
            let state = try await client.fetchState()
            guard seq == refreshSeq else { return }
            let (shown, permissions) = Self.withoutPermissionCards(state.delegations)
            delegations = shown
            // Counted here rather than taken from the daemon: its count includes permission requests.
            awaiting = shown.filter { $0.phase == .awaiting }.count
            connection = .connected
            notePermissionRequests(permissions)
            daemonLang = Lang(daemonValue: state.locale)
            applyLang()
            pruneSelections()
        } catch {
            guard seq == refreshSeq else { return }
            delegations = []
            awaiting = 0
            connection = .down
            daemonLang = nil
            applyLang()
        }
        notify()
    }

    // MARK: Permission requests

    /// Permission requests belong to the popup. A handoff waiting on one shows as still working,
    /// and its card is dropped. Returns the dropped cards' ids.
    private static func withoutPermissionCards(_ list: [Delegation]) -> ([Delegation], [Int]) {
        var dropped: [Int] = []
        let shown = list.map { delegation -> Delegation in
            guard delegation.phase == .awaiting, let card = delegation.card, card.open, card.isPermissionRequest else {
                return delegation
            }
            dropped.append(card.id)
            var working = delegation
            working.state = Phase.running.rawValue
            working.card = nil
            return working
        }
        return (shown, dropped)
    }

    private func notePermissionRequests(_ cards: [Int]) {
        let fresh = Set(cards).subtracting(seenPermissionCards)
        guard !fresh.isEmpty else { return }
        seenPermissionCards.formUnion(fresh)
        checkPopup()
        if popupStatus != .installed, !popupNoticeDismissed {
            showPopupNotice = true
        }
    }

    func checkPopup() {
        popupStatus = PermissionPopup.detect()
        if popupStatus == .installed { showPopupNotice = false }
    }

    func dismissPopupNotice() {
        popupNoticeDismissed = true
        showPopupNotice = false
        notify()
    }

    private func pruneSelections() {
        let openCards = Set(delegations.compactMap { $0.card?.open == true ? $0.card?.id : nil })
        selections = selections.filter { openCards.contains($0.key) }
    }

    // MARK: Card actions

    func selection(card: Int, decision: String) -> String? {
        selections[card]?[decision]
    }

    func choose(card: Int, decision: String, option: String) {
        var picked = selections[card] ?? [:]
        picked[decision] = picked[decision] == option ? nil : option
        selections[card] = picked
    }

    func draft(for id: String) -> String {
        drafts[id] ?? ""
    }

    func trimmedDraft(for id: String) -> String {
        draft(for: id).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    func canSend(_ delegation: Delegation) -> Bool {
        guard let card = delegation.card, card.open, !busy.contains(delegation.id) else { return false }
        return !(selections[card.id] ?? [:]).isEmpty || !trimmedDraft(for: delegation.id).isEmpty
    }

    func send(_ delegation: Delegation) {
        guard canSend(delegation), let card = delegation.card else { return }
        let choices = selections[card.id] ?? [:]
        let reply = trimmedDraft(for: delegation.id)
        perform(delegation.id) { client in
            try await client.decide(cardID: card.id, choices: choices, reply: reply)
        } onSuccess: { store in
            store.sentAnswers[card.id] = SentAnswer(choices: choices, reply: reply)
            store.selections[card.id] = nil
            store.drafts[delegation.id] = nil
        }
    }

    func close(_ delegation: Delegation) {
        let reply = trimmedDraft(for: delegation.id)
        perform(delegation.id) { client in
            try await client.close(delegationID: delegation.id, reply: reply)
        } onSuccess: { store in
            store.drafts[delegation.id] = nil
        }
    }

    func takeover(_ delegation: Delegation) {
        perform(delegation.id) { client in
            try await client.takeover(delegationID: delegation.id)
        } onSuccess: { _ in }
    }

    private func perform(
        _ id: String,
        _ request: @escaping @MainActor (DaemonClient) async throws -> Void,
        onSuccess: @escaping @MainActor (Store) -> Void
    ) {
        guard !busy.contains(id) else { return }
        busy.insert(id)
        errors[id] = nil
        Task {
            do {
                try await request(client)
                onSuccess(self)
                await refresh()
            } catch let error as DeskError {
                errors[id] = error
            } catch {
                errors[id] = .server(error.localizedDescription)
            }
            busy.remove(id)
        }
    }
}
