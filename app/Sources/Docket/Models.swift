import Foundation

struct DocketState: Decodable, Sendable {
    var locale: String?
    var awaiting: Int?
    var delegations: [Delegation]
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
        kind = try c.decodeIfPresent(String.self, forKey: .kind) ?? "report"
        open = try c.decodeIfPresent(Bool.self, forKey: .open) ?? false
        progress = try c.decodeIfPresent(String.self, forKey: .progress)
        decisions = try c.decodeIfPresent([Decision].self, forKey: .decisions) ?? []
        decidedForYou = try c.decodeIfPresent([String].self, forKey: .decidedForYou) ?? []
        conclusion = try c.decodeIfPresent(String.self, forKey: .conclusion)
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
}

struct Option: Decodable, Sendable, Identifiable {
    var id: String
    var label: String
    var why: String?
    var recommended: Bool?

    var isRecommended: Bool { recommended ?? false }
}

struct DaemonInfo: Decodable, Sendable {
    var url: String
    var token: String
}

struct ErrorBody: Decodable, Sendable {
    var error: String
}
