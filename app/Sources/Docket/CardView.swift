import SwiftUI

struct CardView: View {
    @ObservedObject var store: Store
    let delegation: Delegation

    @ViewState private var showHandled = false
    @ViewState private var confirmingClose = false

    private var s: Strings { store.s }
    private var card: Card? { delegation.card }
    private var phase: Phase { delegation.phase }
    private var isOpen: Bool { card?.open == true && phase != .closed && phase != .takenOver }
    private var isLimit: Bool { card?.kind == "limit" }
    private var answered: Bool { card != nil && !isOpen }
    private var busy: Bool { store.busy.contains(delegation.id) }
    private var roundLimit: Int { delegation.roundLimit ?? 3 }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header

            // The run's result (or the conclusion), set apart on its own tinted block.
            if phase == .closed, let conclusion = card?.conclusion, !conclusion.isEmpty {
                summaryBlock(conclusion).opacity(0.6)  // same weight as a taken-over card: quiet
            } else if let progress = card?.progress, !progress.isEmpty {
                summaryBlock(progress)
                    .opacity(answered ? 0.6 : 1)
            }

            switch phase {
            case .running: ActivityLine(text: s(.activityRunning))
            case .pending: ActivityLine(text: s(.activityPending))
            default: EmptyView()
            }

            if isOpen, let card, !card.decisions.isEmpty {
                decisions(card)
            } else if isOpen {
                Text(s(.nothingToDecide))
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if isLimit && phase != .closed && phase != .takenOver {
                Label {
                    Text(s(delegation.opensInTerminal ? .roundsUsedTerminal : .roundsUsed, roundLimit))
                        .fixedSize(horizontal: false, vertical: true)
                } icon: {
                    Image(systemName: "exclamationmark.circle")
                }
                .font(.system(size: 11.5))
                .foregroundStyle(.orange)
            }

            if phase != .closed, phase != .takenOver, let items = card?.decidedForYou, !items.isEmpty {
                SectionDivider()
                handledForYou(items)
            }

            if phase != .closed {
                SectionDivider()
                VStack(alignment: .leading, spacing: 8) {
                    if isOpen {
                        replyField
                    }
                    if let error = store.errors[delegation.id] {
                        Text(error.message(s))
                            .font(.system(size: 11))
                            .foregroundStyle(.orange)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    actionRow
                }
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(Color.primary.opacity(isOpen ? 0.06 : 0.035))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .strokeBorder(Color.primary.opacity(0.07), lineWidth: 0.5)
        )
        .opacity(phase == .closed || phase == .takenOver ? 0.55 : 1)
    }

    // MARK: Pieces

    private var header: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(delegation.title)
                    .font(.system(size: 13, weight: .semibold))
                    .lineLimit(3)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
                StateBadge(phase: phase, s: s, readyToClose: delegation.readyToClose)
            }
            Text(meta)
                .font(.system(size: 11).monospacedDigit())
                .foregroundStyle(.secondary)
                .lineLimit(1)
        }
    }

    private var meta: String {
        var parts = [s(.round, delegation.rounds ?? 0, roundLimit)]
        if let ms = delegation.updatedAt ?? delegation.dispatchedAt {
            parts.append(s.relative(Date(timeIntervalSince1970: ms / 1000)))
        }
        if let more = delegation.queued, more > 0 { parts.append(s(.queued, more)) }
        return parts.joined(separator: s(.metaSeparator))
    }

    private func decisions(_ card: Card) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            ForEach(card.decisions) { decision in
                VStack(alignment: .leading, spacing: 6) {
                    Text(verbatim: decision.question)
                        .font(.system(size: 12.5, weight: .medium))
                        .fixedSize(horizontal: false, vertical: true)
                    ForEach(decision.options) { option in
                        OptionButton(
                            option: option,
                            selected: store.selection(card: card.id, decision: decision.id) == option.id,
                            s: s
                        ) {
                            store.choose(card: card.id, decision: decision.id, option: option.id)
                        }
                    }
                }
            }
        }
    }

    private func handledForYou(_ items: [String]) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Button {
                withAnimation(.easeInOut(duration: 0.15)) { showHandled.toggle() }
            } label: {
                HStack(spacing: 4) {
                    Text(s(.handledForYou, items.count))
                    Image(systemName: "chevron.right")
                        .font(.system(size: 8.5, weight: .semibold))
                        .rotationEffect(.degrees(showHandled ? 90 : 0))
                }
                .font(.system(size: 11.5))
                .foregroundStyle(.secondary)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            if showHandled {
                VStack(alignment: .leading, spacing: 3) {
                    ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                        HStack(alignment: .firstTextBaseline, spacing: 6) {
                            Text("·")
                            Text(verbatim: item).fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
                .font(.system(size: 11.5))
                .foregroundStyle(.secondary)
                .padding(.leading, 4)
            }
        }
    }

    private func summaryBlock(_ text: String) -> some View {
        Text(verbatim: text)
            .font(.system(size: 12.5))
            .foregroundStyle(.primary.opacity(0.9))
            .fixedSize(horizontal: false, vertical: true)
            .padding(.horizontal, 10)
            .padding(.vertical, 8)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .fill(Color.primary.opacity(0.06))
            )
    }

    private var replyField: some View {
        let draft = Binding(
            get: { store.draft(for: delegation.id) },
            set: { store.drafts[delegation.id] = $0 }
        )
        let canSend = store.canSend(delegation)
        return HStack(spacing: 6) {
            TextField(s(.replyPlaceholder), text: draft)
                .textFieldStyle(.plain)
                .font(.system(size: 12.5))
                .onSubmit { store.send(delegation) }
            Button {
                store.send(delegation)
            } label: {
                if busy {
                    ProgressView().controlSize(.small).scaleEffect(0.7).frame(width: 18, height: 18)
                } else {
                    Image(systemName: "arrow.up.circle.fill")
                        .font(.system(size: 18))
                        .foregroundStyle(canSend ? Color.accentColor : Color.secondary.opacity(0.45))
                }
            }
            .buttonStyle(.plain)
            .disabled(!canSend)
            .help(s(.send))
            .accessibilityLabel(s(.send))
        }
        .padding(.leading, 10)
        .padding(.trailing, 4)
        .frame(height: 30)
        .background(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(Color.primary.opacity(0.05))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .strokeBorder(Color.primary.opacity(0.08), lineWidth: 0.5)
        )
    }

    @ViewBuilder
    private var actionRow: some View {
        switch phase {
        case .closed:
            EmptyView()
        case .takenOver:
            EmptyView()  // taken over: the session is yours, the panel has nothing left to offer
        default:
            HStack(spacing: 8) {
                Spacer(minLength: 0)
                Button(confirmingClose ? s(.confirmClose) : s(.close)) {
                    if confirmingClose {
                        confirmingClose = false
                        store.close(delegation)
                    } else {
                        confirmingClose = true
                        Task {
                            try? await Task.sleep(for: .seconds(3))
                            confirmingClose = false
                        }
                    }
                }
                .buttonStyle(DeskButtonStyle(prominent: delegation.readyToClose))
                Button(s(delegation.opensInTerminal ? .continueInTerminal : .openInOpenCode)) { store.takeover(delegation) }
                    .buttonStyle(DeskButtonStyle(prominent: isLimit && !delegation.readyToClose))
            }
            .disabled(busy)
        }
    }
}

/// A hairline between the parts of a card.
struct SectionDivider: View {
    var body: some View {
        Rectangle()
            .fill(Color.primary.opacity(0.07))
            .frame(height: 0.5)
            .frame(maxWidth: .infinity)
    }
}

struct OptionButton: View {
    let option: Option
    let selected: Bool
    let s: Strings
    let action: () -> Void

    @ViewState private var hovering = false

    private var fill: Color {
        // The recommendation is only the quiet tag on the right; the option itself looks like the others.
        if selected { return .accentColor }
        return Color.primary.opacity(hovering ? 0.08 : 0.045)
    }

    private var labelColor: Color {
        selected ? .white : .primary
    }

    private var noteColor: Color {
        selected ? Color.white.opacity(0.85) : .secondary
    }

    private var label: Text {
        Text(verbatim: option.label).fontWeight(selected ? .semibold : .regular)
    }

    var body: some View {
        Button(action: action) {
            HStack(alignment: .firstTextBaseline, spacing: 7) {
                Image(systemName: selected ? "checkmark.circle.fill" : "circle")
                    .font(.system(size: 11.5))
                    .foregroundStyle(selected ? Color.white : Color.secondary)
                // One line per option: the label, and a quiet tag on the recommended one. No reasons.
                label
                    .foregroundStyle(labelColor)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 6)
                if option.isRecommended {
                    Text(s(.recommended))
                        .font(.system(size: 10.5, weight: .medium))
                        .foregroundStyle(noteColor)
                }
            }
            .font(.system(size: 12.5))
            .padding(.horizontal, 10)
            .padding(.vertical, 7)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(fill))
            .overlay(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .strokeBorder(Color.clear, lineWidth: 0.75)
            )
            .contentShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}
