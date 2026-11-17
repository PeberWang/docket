import SwiftUI

/// Command Line Tools ship without the SwiftUIMacros plugin, so the `@State` macro can't expand.
/// Naming the property wrapper through an alias sidesteps the macro and uses `State` directly.
typealias ViewState = SwiftUI.State

struct RootView: View {
    @ObservedObject var store: Store

    var body: some View {
        let radius = store.expanded ? PanelMetrics.expandedRadius : PanelMetrics.pillRadius
        ZStack {
            if store.expanded {
                ExpandedView(store: store)
            } else {
                PillView(store: store)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .clipShape(RoundedRectangle(cornerRadius: radius, style: .circular))
        .overlay(
            RoundedRectangle(cornerRadius: radius, style: .circular)
                .strokeBorder(Color.primary.opacity(0.12), lineWidth: 0.5)
        )
    }
}

// MARK: - Collapsed pill

struct PillView: View {
    @ObservedObject var store: Store
    @ViewState private var hovering = false

    var body: some View {
        VStack(spacing: 10) {
            Image(systemName: store.awaiting > 0 ? "tray.full" : "tray")
                .font(.system(size: 14, weight: .medium))
                .foregroundStyle(store.connection == .down ? .tertiary : .primary)
            if store.awaiting > 0 {
                Text("\(store.awaiting)")
                    .font(.system(size: 11, weight: .semibold).monospacedDigit())
                    .foregroundStyle(.white)
                    .frame(minWidth: 18, minHeight: 18)
                    .background(Capsule().fill(Color.accentColor))
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.primary.opacity(hovering ? 0.06 : 0))
        .contentShape(Rectangle())
        .onHover { hovering = $0 }
        .onTapGesture { store.expanded = true }
        .help(store.connection == .down ? store.s(.serviceNotRunning) : store.s(.showDeskHelp))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(store.awaiting > 0 ? "\(store.s(.panelTitle)), \(store.s.needYou(store.awaiting))" : store.s(.panelTitle))
        .accessibilityAddTraits(.isButton)
    }
}

// MARK: - Expanded panel

struct ExpandedView: View {
    @ObservedObject var store: Store

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider().opacity(0.6)
            content
        }
        .onExitCommand { store.expanded = false }
    }

    private var header: some View {
        HStack(spacing: 8) {
            Text(store.s(.panelTitle))
                .font(.system(size: 15, weight: .semibold))
            if store.awaiting > 0 {
                Text(store.s.needYou(store.awaiting))
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(Color.accentColor)
                    .padding(.horizontal, 7)
                    .padding(.vertical, 2)
                    .background(Capsule().fill(Color.accentColor.opacity(0.12)))
                    .fixedSize()
            }
            Spacer()
            Button {
                store.expanded = false
            } label: {
                Image(systemName: "chevron.right")
                    .font(.system(size: 12, weight: .semibold))
                    .frame(width: 26, height: 26)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .foregroundStyle(.secondary)
            .help(store.s(.hideDeskHelp))
            .accessibilityLabel(store.s(.hideDesk))
        }
        .padding(.leading, 16)
        .padding(.trailing, 10)
        .frame(height: 48)
    }

    @ViewBuilder
    private var content: some View {
        switch store.connection {
        case .unknown:
            ProgressView()
                .controlSize(.small)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        case .down:
            quiet(store.s(.serviceNotRunning))
        case .connected:
            if store.delegations.isEmpty {
                quiet(store.s(.noHandoffs))
            } else {
                ScrollView {
                    VStack(spacing: 10) {
                        ForEach(store.delegations) { delegation in
                            CardView(store: store, delegation: delegation)
                        }
                    }
                    .padding(12)
                }
                .scrollIndicators(.automatic)
            }
        }
    }

    private func quiet(_ text: String) -> some View {
        Text(text)
            .font(.system(size: 12.5))
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

// MARK: - Shared pieces

/// Drawn by hand so the accent color survives the panel being non-key.
struct DeskButtonStyle: ButtonStyle {
    var prominent = false
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 11.5, weight: prominent ? .semibold : .medium))
            .lineLimit(1)
            .fixedSize()
            .foregroundStyle(prominent ? Color.white : Color.primary.opacity(0.85))
            .padding(.horizontal, 10)
            .frame(height: 24)
            .background(
                RoundedRectangle(cornerRadius: 6, style: .continuous)
                    .fill(prominent ? Color.accentColor : Color.primary.opacity(0.07))
            )
            .contentShape(Rectangle())
            .opacity(configuration.isPressed ? 0.7 : (isEnabled ? 1 : 0.45))
    }
}

struct StateBadge: View {
    let phase: Phase
    let s: Strings

    var body: some View {
        let tint = phase == .awaiting ? Color.accentColor : Color.secondary
        Text(s.phase(phase))
            .lineLimit(1)
            .font(.system(size: 10.5, weight: .medium))
            .foregroundStyle(tint)
            .padding(.horizontal, 6)
            .padding(.vertical, 1.5)
            .background(Capsule().fill(tint.opacity(0.13)))
            .fixedSize()
    }
}

struct ActivityLine: View {
    let text: String

    var body: some View {
        HStack(spacing: 6) {
            ProgressView()
                .controlSize(.small)
                .scaleEffect(0.7)
                .frame(width: 12, height: 12)
            Text(text)
        }
        .font(.system(size: 11.5))
        .foregroundStyle(.secondary)
    }
}
