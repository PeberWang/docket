import Foundation

/// What the user picked in Settings, stored under `uiLanguage`.
/// A pick of zh or en beats the daemon; `system` leaves the choice to `Lang`.
enum LanguagePreference: String, CaseIterable, Sendable {
    case system, zh, en

    static let defaultsKey = "uiLanguage"

    static var saved: LanguagePreference {
        UserDefaults.standard.string(forKey: defaultsKey).flatMap(LanguagePreference.init(rawValue:)) ?? .system
    }

    func save() {
        UserDefaults.standard.set(rawValue, forKey: Self.defaultsKey)
    }

    func resolve(daemon: Lang?) -> Lang {
        switch self {
        case .zh: .zh
        case .en: .en
        case .system: daemon ?? .system
        }
    }
}

/// Interface language. The daemon's `locale` wins; until it answers, follow the system.
enum Lang: String, Sendable {
    case en, zh

    static var system: Lang {
        (Locale.preferredLanguages.first ?? "en").lowercased().hasPrefix("zh") ? .zh : .en
    }

    init?(daemonValue: String?) {
        guard let value = daemonValue?.lowercased() else { return nil }
        if value.hasPrefix("zh") {
            self = .zh
        } else if value.hasPrefix("en") {
            self = .en
        } else {
            return nil
        }
    }

    var locale: Locale {
        Locale(identifier: self == .zh ? "zh_CN" : "en_US")
    }
}

enum StringKey: Sendable {
    // Panel
    case panelTitle, needYouOne, needYouMany, noHandoffs, serviceNotRunning
    case showDeskHelp, hideDeskHelp, statusTooltip
    // States
    case phaseAwaiting, phaseReadyToClose, phasePending, phaseRunning, phaseTakenOver, phaseClosed, phaseUnknown
    case activityRunning, activityPending
    // Card
    case round, queued, metaSeparator, justNow, nothingToDecide
    case recommended, handledForYou
    case answerChoice, youSaid, roundsUsed, roundsUsedTerminal
    case replyPlaceholder, send, close, confirmClose, openInOpenCode, continueInTerminal
    // Menu bar
    case showDesk, hideDesk, hidePanel, showPanel, panelHidden, settings, quit
    // Settings
    case settingsTitle, settingsInterface, settingsLanguage, languageSystem, settingsShowStrip
    // Permission popup
    case popupNotice, popupHowTo, copySnippet, copied, dismiss
    case settingsPermissions, popupInstalled, popupMissing, popupAbout
    // Errors
    case unreadableResponse, serviceReturned
}

/// The one table of interface copy. English is the design language; Chinese is second.
/// Formats use `%d` / `%@`.
struct Strings: Sendable {
    let lang: Lang

    private static let table: [StringKey: (en: String, zh: String)] = [
        .panelTitle: ("Desk", "案头"),
        .needYouOne: ("%d waiting on you", "%d 件待你定"),
        .needYouMany: ("%d waiting on you", "%d 件待你定"),
        .noHandoffs: ("Nothing handed off", "暂无交办"),
        .serviceNotRunning: ("Desk service isn’t running", "案头后台未运行"),
        .showDeskHelp: ("Show Desk (⌥⌘D)", "展开案头（⌥⌘D）"),
        .hideDeskHelp: ("Hide (Esc or ⌥⌘D)", "收起（Esc / ⌥⌘D）"),
        .statusTooltip: ("OpenClaw Desk (⌥⌘D)", "案头（⌥⌘D）"),

        .phaseAwaiting: ("Waiting on you", "待你定"),
        .phaseReadyToClose: ("Ready to close", "待结案"),
        .nothingToDecide: ("Nothing needs a decision. Close it when you are happy.", "没有要你定的事，确认没问题就可以结案。"),
        .phasePending: ("Writing brief", "处理中"),
        .phaseRunning: ("Working", "执行中"),
        .phaseTakenOver: ("With you", "已接管"),
        .phaseClosed: ("Done", "已结案"),
        .phaseUnknown: ("Unknown", "未知"),
        .activityRunning: ("Working…", "执行中…"),
        .activityPending: ("Writing the next brief…", "PM 整理中…"),

        .round: ("Round %d of %d", "第 %d/%d 轮"),
        .queued: ("%d more waiting", "另有 %d 张等候"),
        .metaSeparator: (" · ", " · "),
        .justNow: ("Just now", "刚刚"),
        .recommended: ("Recommended", "推荐"),
        .handledForYou: ("Decided for you (%d)", "已代你定 %d 项"),
        .answerChoice: ("%@ → %@", "%@ → %@"),
        .youSaid: ("You said: %@", "你说：%@"),
        .roundsUsed: ("%d rounds used. Take it over in OpenCode.", "已满 %d 轮，建议接管"),
        .roundsUsedTerminal: ("%d rounds used. Take it over in Terminal.", "已满 %d 轮，建议在终端接管"),
        .replyPlaceholder: ("Reply…", "回一句…"),
        .send: ("Send", "发送"),
        .close: ("Mark done", "结案"),
        .confirmClose: ("Confirm", "确认结案"),
        .openInOpenCode: ("Open in OpenCode", "在 OpenCode 打开"),
        .continueInTerminal: ("Continue in Terminal", "在终端接管"),

        .showDesk: ("Show Panel", "展开面板"),
        .hideDesk: ("Collapse Panel", "收起面板"),
        .hidePanel: ("Hide Sidebar", "隐藏右侧条带"),
        .showPanel: ("Restore Sidebar", "恢复右侧条带"),
        .panelHidden: ("Panel hidden", "面板已隐藏"),
        .settings: ("Settings…", "设置…"),
        .quit: ("Quit", "退出"),

        .settingsTitle: ("Settings", "设置"),
        .settingsInterface: ("Interface", "界面"),
        .settingsLanguage: ("Language", "语言"),
        .languageSystem: ("Follow System", "跟随系统"),
        .settingsShowStrip: ("Show the right-side strip", "显示右侧条带"),

        .popupNotice: ("OpenCode is asking for a permission, and the popup that answers it isn’t installed.",
                       "OpenCode 有授权要处理，但授权弹窗组件没装。"),
        .popupHowTo: ("Add \"opencode-permission-notify\" to plugins in ~/.config/opencode/opencode.jsonc, then restart OpenCode. Until then, answer in the OpenCode window.",
                      "在 ~/.config/opencode/opencode.jsonc 的 plugins 里加上 \"opencode-permission-notify\"，重启 OpenCode。装好前请回 OpenCode 窗口处理。"),
        .copySnippet: ("Copy config", "复制配置"),
        .copied: ("Copied", "已复制"),
        .dismiss: ("Dismiss", "关闭"),
        .settingsPermissions: ("Permissions", "授权"),
        .popupInstalled: ("Permission popup installed", "授权弹窗已安装"),
        .popupMissing: ("Permission popup not installed", "授权弹窗未安装"),
        .popupAbout: ("OpenCode’s permission requests are answered in the popup at the top right, not on the Desk.",
                      "OpenCode 的授权请求在右上角弹窗里回答，不进案头。"),

        .unreadableResponse: ("Couldn’t read the service’s response", "后台返回的数据无法解析"),
        .serviceReturned: ("The service returned %d", "后台返回 %d"),
    ]

    func callAsFunction(_ key: StringKey, _ args: CVarArg...) -> String {
        guard let entry = Self.table[key] else { return "\(key)" }
        let format = lang == .zh ? entry.zh : entry.en
        return args.isEmpty ? format : String(format: format, locale: lang.locale, arguments: args)
    }


    func needYou(_ count: Int) -> String {
        self(count == 1 ? .needYouOne : .needYouMany, count)
    }

    func phase(_ phase: Phase) -> String {
        switch phase {
        case .awaiting: self(.phaseAwaiting)
        case .pending: self(.phasePending)
        case .running: self(.phaseRunning)
        case .takenOver: self(.phaseTakenOver)
        case .closed: self(.phaseClosed)
        case .unknown: self(.phaseUnknown)
        }
    }

    func relative(_ date: Date) -> String {
        if Date().timeIntervalSince(date) < 60 { return self(.justNow) }
        let formatter = RelativeDateTimeFormatter()
        formatter.locale = lang.locale
        formatter.unitsStyle = .full
        return formatter.localizedString(for: date, relativeTo: Date())
    }
}
