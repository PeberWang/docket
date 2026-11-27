import Foundation

struct DocketState: Decodable, Sendable {
    var locale: String?
    var awaiting: Int?
    var delegations: [Delegation]
    /// Handoffs in the response this panel could not read: left out, so every other one still shows.
    var unreadable = 0

    enum CodingKeys: String, CodingKey {
        case locale, awaiting, delegations
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        locale = try? c.decodeIfPresent(String.self, forKey: .locale)
        awaiting = try? c.decodeIfPresent(Int.self, forKey: .awaiting)
        let list = try c.decode(LossyList<Delegation>.self, forKey: .delegations)
        delegations = list.items
        unreadable = list.skipped
    }
}

/// A list that steps past the elements it cannot read instead of failing as a whole.
struct LossyList<Element: Decodable & Sendable>: Decodable, Sendable {
    var items: [Element] = []
    var skipped = 0

    init(from decoder: Decoder) throws {
        var list = try decoder.unkeyedContainer()
        while !list.isAtEnd {
            if let item = try? list.decode(Element.self) {
                items.append(item)
            } else {
                _ = try? list.decode(Ignored.self)
                skipped += 1
            }
        }
    }
}

/// Reads as anything, so a lossy list can move past an element that failed.
private struct Ignored: Decodable {
    init(from decoder: Decoder) throws {}
}

struct Delegation: Decodable, Sendable, Identifiable {
    var id: String
    var title: String
    var engine: String?
    var state: String
    var rounds: Int?
    var handbacks: Int?
    var queued: Int?
    var roundLimit: Int?
    var dispatchedAt: Double?
    var updatedAt: Double?
    var card: Card?
    /// Where the work stands as the daemon's watchdog sees it (awaiting_dispatch … stalled).
    var work: String?

    enum CodingKeys: String, CodingKey {
        case id, title, engine, state, rounds, handbacks, queued, card
        case work = "phase"
        case roundLimit = "round_limit"
        case dispatchedAt = "dispatched_at"
        case updatedAt = "updated_at"
    }

    var phase: Phase { Phase(rawValue: state) ?? .unknown }

    /// The docket could not get the work moving and says so on the card: never shown as normal progress.
    var stalled: Bool { work == "stalled" || (card?.open == true && card?.stalled == true) }
    /// The docket's own failure (not the task's), waiting on a retry.
    var docketFault: Bool { card?.open == true && card?.docketFault == true && !stalled }
    /// Handed to OpenCode, not yet seen working.
    var dispatching: Bool { work == "awaiting_dispatch" || work == "dispatching" }

    /// A card with nothing to decide: the panel says so instead of showing an empty question.
    /// A failure card is never "ready to close": the docket's own error is not the work being done.
    var readyToClose: Bool { state == "awaiting" && card?.open == true && card?.kind != "failure" && (card?.decisions.isEmpty ?? false) }

    /// OpenCode tasks open in OpenCode; CLI agents (Claude Code, Codex) continue in Terminal.
    var opensInTerminal: Bool { engine == "claude" || engine == "codex" }
}

enum Phase: String, Sendable {
    case running, pending, awaiting, limit, takenOver = "taken_over", closed, unknown
}

struct Card: Decodable, Sendable {
    var id: Int
    var kind: String
    var open: Bool
    var progress: String?
    var decisions: [Decision]
    var decidedForYou: [String]
    var conclusion: String?
    // Not in the spec: if the daemon echoes the user's answer on an answered card, show it.
    var choices: [String: String]?
    var reply: String?
    var stalled: Bool?
    var docketFault: Bool?

    enum CodingKeys: String, CodingKey {
        case id, kind, open, progress, decisions, conclusion, choices, reply, stalled
        case decidedForYou = "decided_for_you"
        case docketFault = "docket_fault"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        // Everything else a card holds was written by a model: a wrong type reads as absent
        // and never takes the card, let alone the panel, down with it.
        kind = (try? c.decodeIfPresent(String.self, forKey: .kind)) ?? "report"
        open = (try? c.decodeIfPresent(Bool.self, forKey: .open)) ?? false
        progress = try? c.decodeIfPresent(String.self, forKey: .progress)
        decisions = (try? c.decodeIfPresent(LossyList<Decision>.self, forKey: .decisions))?.items ?? []
        decidedForYou = (try? c.decodeIfPresent([String].self, forKey: .decidedForYou)) ?? []
        conclusion = try? c.decodeIfPresent(String.self, forKey: .conclusion)
        choices = try? c.decodeIfPresent([String: String].self, forKey: .choices)
        reply = try? c.decodeIfPresent(String.self, forKey: .reply)
        stalled = try? c.decodeIfPresent(Bool.self, forKey: .stalled)
        docketFault = try? c.decodeIfPresent(Bool.self, forKey: .docketFault)
    }
}

struct Decision: Decodable, Sendable, Identifiable {
    var id: String
    var question: String
    var options: [Option]

    enum CodingKeys: String, CodingKey {
        case id, question, options
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        question = try c.decode(String.self, forKey: .question)
        options = (try? c.decodeIfPresent(LossyList<Option>.self, forKey: .options))?.items ?? []
    }
}

struct Option: Decodable, Sendable, Identifiable {
    var id: String
    var label: String
    var why: String?
    var recommended: Bool?

    var isRecommended: Bool { recommended ?? false }

    enum CodingKeys: String, CodingKey {
        case id, label, why, recommended
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        label = try c.decode(String.self, forKey: .label)
        why = try? c.decodeIfPresent(String.self, forKey: .why)
        recommended = try? c.decodeIfPresent(Bool.self, forKey: .recommended)
    }
}

struct DaemonInfo: Decodable, Sendable {
    var url: String
    var token: String
}

struct ErrorBody: Decodable, Sendable {
    var error: String
}
