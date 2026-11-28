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
    case showDocketHelp, hideDocketHelp, statusTooltip
    // States
    case phaseAwaiting, phaseReadyToClose, phasePending, phaseRunning, phaseLimit, phaseTakenOver, phaseClosed, phaseUnknown
    case activityRunning, activityPending, activityDispatching
    case phaseStalled, phaseDocketFault, passWordOn, notePlaceholder
    // Card
    case round, queued, metaSeparator, justNow, nothingToDecide
    case recommended, handledForYou
    case answerChoice, youSaid, goingOnWith, roundsUsed, roundsUsedTerminal
    case replyPlaceholder, send, close, confirmClose, openInOpenCode, continueInTerminal, handBack, handBackHelp
    // Menu bar
    case showDocket, hideDocket, hidePanel, showPanel, panelHidden, settings, quit
    case permissionSettings, permissionSettingsMissing, permissionSettingsFailed, ok
    // Settings
    case settingsTitle, settingsInterface, settingsLanguage, languageSystem, settingsShowStrip
    // Settings: models
    case settingsModels, modelsNeedDaemon, modelsSaved, applyModels
    case pmModelHeading, pmModel, pmModelPlaceholder, pmFallback, pmFallbackPlaceholder, pmCredentials
    case judgeHeading, judgeBackend, backendSystemOne, backendOpenAI, judgeModel, judgeEndpoint, endpointPlaceholder
    case judgeKey, saveKey, keyConfigured, keyMissingPlaceholder, keySaved, keySaveFailed, keySaveFailedLaunch
    case keyFrom, keyNone, sourceKeychain, sourceEnv, sourceSecrets
    case judgeOff, judgeTwoWays, missingEndpoint, missingModel, missingKey
    // Permission popup
    case popupNotice, popupHowTo, copySnippet, copied, dismiss
    case settingsPermissions, popupInstalled, popupMissing, popupAbout
    // Errors
    case unreadableResponse, serviceReturned, someUnreadable
}

/// The one table of interface copy. English is the design language; Chinese is second.
/// Formats use `%d` / `%@`.
struct Strings: Sendable {
    let lang: Lang

    private static let table: [StringKey: (en: String, zh: String)] = [
        .panelTitle: ("Docket", "案头"),
        .needYouOne: ("%d waiting on you", "%d 件待你定"),
        .needYouMany: ("%d waiting on you", "%d 件待你定"),
        .noHandoffs: ("Nothing handed off", "暂无交办"),
        .serviceNotRunning: ("Docket service isn’t running", "案头后台未运行"),
        .showDocketHelp: ("Show Docket (⌥⌘D)", "展开案头（⌥⌘D）"),
        .hideDocketHelp: ("Hide (Esc or ⌥⌘D)", "收起（Esc / ⌥⌘D）"),
        .statusTooltip: ("OpenClaw Docket (⌥⌘D)", "案头（⌥⌘D）"),

        .phaseAwaiting: ("Waiting on you", "待你定"),
        .phaseReadyToClose: ("Ready to close", "待结案"),
        .nothingToDecide: ("Nothing needs a decision. Close it when you are happy.", "没有要你定的事，确认没问题就可以结案。"),
        .phasePending: ("Writing brief", "处理中"),
        .phaseRunning: ("Working", "执行中"),
        .phaseLimit: ("Take it over", "请接管"),
        .phaseTakenOver: ("With you", "已接管"),
        .phaseClosed: ("Done", "已结案"),
        .phaseUnknown: ("Unknown", "未知"),
        .activityRunning: ("Working…", "执行中…"),
        .activityPending: ("Writing the next brief…", "PM 整理中…"),
        .activityDispatching: ("Handing it to OpenCode…", "派发中，等 OpenCode 接手…"),
        .phaseStalled: ("Stuck", "卡住了"),
        .phaseDocketFault: ("Docket fault", "案头故障"),
        .passWordOn: ("Pass a word on", "转达一句"),
        .notePlaceholder: ("For the PM…", "要转达给 PM 的话…"),

        .round: ("Round %d of %d", "第 %d 轮 / 共 %d 轮"),
        .queued: ("%d more waiting", "另有 %d 张等候"),
        .metaSeparator: (" · ", " · "),
        .justNow: ("Just now", "刚刚"),
        .recommended: ("Recommended", "推荐"),
        .handledForYou: ("Decided for you (%d)", "已代你定 %d 项"),
        .answerChoice: ("%@ → %@", "%@ → %@"),
        .youSaid: ("You said: %@", "你说：%@"),
        .goingOnWith: ("Going on with your answer: %@", "已按你的回答继续：%@"),
        .roundsUsed: ("All %d rounds used. Take it over in OpenCode.", "已满 %d 轮，请接管"),
        .roundsUsedTerminal: ("All %d rounds used. Take it over in Terminal.", "已满 %d 轮，请在终端接管"),
        .replyPlaceholder: ("Reply…", "回一句…"),
        .send: ("Send", "发送"),
        .close: ("Mark done", "结案"),
        .confirmClose: ("Confirm", "确认结案"),
        .openInOpenCode: ("Open in OpenCode", "在 OpenCode 打开"),
        .continueInTerminal: ("Continue in Terminal", "在终端接管"),
        .handBack: ("Hand back", "交还"),
        .handBackHelp: ("Give it back to the PM to carry on. Rounds already used stay used.", "交还给 PM 继续推进，已用的轮次不清零"),

        .showDocket: ("Show Panel", "展开面板"),
        .hideDocket: ("Collapse Panel", "收起面板"),
        .hidePanel: ("Hide Sidebar", "隐藏右侧条带"),
        .showPanel: ("Restore Sidebar", "恢复右侧条带"),
        .panelHidden: ("Panel hidden", "面板已隐藏"),
        .settings: ("Settings…", "设置…"),
        .quit: ("Quit", "退出"),
        .permissionSettings: ("Permission Tiers…", "权限分级设置…"),
        .permissionSettingsMissing: ("permission-settings not found. Paths tried:", "未找到 permission-settings，路径是："),
        .permissionSettingsFailed: ("Couldn’t open permission-settings", "无法启动 permission-settings"),
        .ok: ("OK", "好"),

        .settingsTitle: ("Settings", "设置"),
        .settingsInterface: ("Interface", "界面"),
        .settingsLanguage: ("Language", "语言"),
        .languageSystem: ("Follow System", "跟随系统"),
        .settingsShowStrip: ("Show the right-side strip", "显示右侧条带"),

        .settingsModels: ("Models", "模型"),
        .modelsNeedDaemon: ("The Docket service isn’t running, so these settings can’t be read or changed.",
                            "案头后台没在运行，这一组设置暂时读不到也改不了。"),
        .modelsSaved: ("Saved. Takes effect within a few seconds, no restart needed.", "已保存，几秒内生效，无需重启。"),
        .applyModels: ("Apply", "应用"),
        .pmModelHeading: ("PM model", "PM 模型"),
        .pmModel: ("Model", "模型"),
        .pmModelPlaceholder: ("OpenClaw’s default", "OpenClaw 默认模型"),
        .pmFallback: ("Fallback", "备用模型"),
        .pmFallbackPlaceholder: ("Same as the model", "与主模型相同"),
        .pmCredentials: ("The PM runs through OpenClaw, which holds its credentials; there is no key to enter here.",
                         "PM 经 OpenClaw 运行，凭据由 OpenClaw 管理，这里不用填 key。"),
        .judgeHeading: ("Card attribution model", "卡片归属模型"),
        .judgeBackend: ("Backend", "后端"),
        .backendSystemOne: ("System One (probabilities)", "System One 概率模型"),
        .backendOpenAI: ("OpenAI-compatible language model", "OpenAI 兼容语言模型"),
        .judgeModel: ("Model", "模型"),
        .judgeEndpoint: ("Endpoint", "端点"),
        .endpointPlaceholder: ("Base URL, e.g. https://api.openai.com/v1", "base URL，如 https://api.openai.com/v1"),
        .judgeKey: ("API key", "API key"),
        .saveKey: ("Save", "保存"),
        .keyConfigured: ("Configured", "已配置"),
        .keyMissingPlaceholder: ("Not set", "未配置"),
        .keySaved: ("Key saved to the keychain.", "密钥已存入钥匙串。"),
        .keySaveFailed: ("Couldn’t write to the keychain (security returned %d). The key was not saved.",
                         "写入钥匙串失败（security 返回 %d），密钥没有保存。"),
        .keySaveFailedLaunch: ("Couldn’t write to the keychain. The key was not saved.", "写入钥匙串失败，密钥没有保存。"),
        .keyFrom: ("Key configured, from %@.", "密钥已配置，来源：%@。"),
        .keyNone: ("No key configured.", "未配置密钥。"),
        .sourceKeychain: ("the keychain", "钥匙串"),
        .sourceEnv: ("an environment variable", "环境变量"),
        .sourceSecrets: ("OpenClaw’s secrets.json", "OpenClaw 的 secrets.json"),
        .judgeOff: ("Not configured (%@). Attribution is off; cards still arrive, without picked background.",
                    "未配置（%@）。归属判断已停用，卡片照常到达，只是不挑背景。"),
        .judgeTwoWays: ("Two ways to turn it on: save a key above, or switch the backend to an OpenAI-compatible language model and fill in its endpoint, model and key.",
                        "两种启用方式：在上面保存密钥；或把后端改为 OpenAI 兼容语言模型，填端点、模型和密钥。"),
        .missingEndpoint: ("no endpoint", "缺端点"),
        .missingModel: ("no model name", "缺模型名"),
        .missingKey: ("no API key", "缺 API key"),

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
        .popupAbout: ("OpenCode’s permission requests are answered in the popup at the top right, not on Docket.",
                      "OpenCode 的授权请求在右上角弹窗里回答，不进案头。"),

        .unreadableResponse: ("Couldn’t read the service’s response", "后台返回的数据无法解析"),
        .serviceReturned: ("The service returned %d", "后台返回 %d"),
        .someUnreadable: ("%d handoff could not be read here; the service log has the details", "有 %d 件交办在这里读不出来，详情见服务日志"),
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
        case .limit: self(.phaseLimit)
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
