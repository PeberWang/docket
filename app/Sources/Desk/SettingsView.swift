import SwiftUI

struct SettingsView: View {
    @ObservedObject var store: Store

    var body: some View {
        let s = store.s
        Form {
            Section(s(.settingsInterface)) {
                Picker(s(.settingsLanguage), selection: $store.languagePreference) {
                    ForEach(LanguagePreference.allCases, id: \.self) { preference in
                        Text(label(preference, s)).tag(preference)
                    }
                }
                .pickerStyle(.radioGroup)
                Toggle(s(.settingsShowStrip), isOn: Binding(
                    get: { !store.hidden },
                    set: { store.hidden = !$0 }
                ))
            }
            Section(s(.settingsPermissions)) {
                let installed = store.popupStatus == .installed
                Label(s(installed ? .popupInstalled : .popupMissing),
                      systemImage: installed ? "checkmark.circle" : "exclamationmark.circle")
                    .foregroundStyle(installed ? Color.primary : Color.orange)
                Text(s(.popupAbout))
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                if !installed {
                    Text(s(.popupHowTo))
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    Button(s(.copySnippet)) { PermissionPopup.copySnippet() }
                }
            }
            .onAppear { store.checkPopup() }
            // Later groups go here as further sections.
        }
        .formStyle(.grouped)
        .scrollDisabled(true)
        .frame(width: 480)
        .fixedSize(horizontal: false, vertical: true)
    }

    /// Language names stay in their own language, as macOS does.
    private func label(_ preference: LanguagePreference, _ s: Strings) -> String {
        switch preference {
        case .system: s(.languageSystem)
        case .zh: "中文"
        case .en: "English"
        }
    }
}
