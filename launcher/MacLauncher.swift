import AppKit

final class Launcher: NSObject, NSApplicationDelegate, NSWindowDelegate {
    let root = Bundle.main.bundleURL.deletingLastPathComponent()
    lazy var messages: [String: String] = loadMessages()
    func loadMessages() -> [String: String] {
        var table: [String: String] = [:]
        let file = Bundle.main.url(forResource: "start_messages", withExtension: "txt")
            ?? root.appendingPathComponent("launcher").appendingPathComponent("start_messages.txt")
        guard let text = try? String(contentsOf: file, encoding: .utf8) else { return table }
        for line in text.components(separatedBy: .newlines) {
            guard line.range(of: "^[a-z_]+=", options: .regularExpression) != nil,
                  let equal = line.firstIndex(of: "=") else { continue }
            table[String(line[..<equal])] = String(line[line.index(after: equal)...])
        }
        return table
    }
    func T(_ key: String, _ value: String = "") -> String {
        if let text = messages[key] { return text.replacingOccurrences(of: "{0}", with: value) }
        let english = Bundle.main.object(forInfoDictionaryKey: "EZRLanguage") as? String == "en"
        switch key {
        case "gui_title": return english ? "EZR Standalone (R 4.6.1)" : "EZR Standalone 実行環境（R 4.6.1）"
        case "gui_app_alone":
            return english
                ? "This app cannot start on its own. Open the EZR Standalone folder and follow the guide to launch from start_mac.command."
                : "このアプリだけでは起動できません。EZR Standalone のフォルダを開き、手順書に従って start_mac.command から起動してください。"
        default: return value.isEmpty ? "[start_messages.txt: \(key)]" : "[start_messages.txt: \(key)] \(value)"
        }
    }
    var window: NSWindow!
    let status = NSTextField(wrappingLabelWithString: "")
    let log = NSTextView()
    var startButton: NSButton!
    var stopButton: NSButton!
    var browserButton: NSButton!
    var copyButton: NSButton!
    var process: Process?
    var input: Pipe?
    var output: Pipe?
    var buffer = Data()
    var browserURL: URL?
    var closeAfterStop = false
    var canStop = false
    var requestedStop = false
    var waitCount = 0

    func applicationDidFinishLaunching(_ notification: Notification) {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 760, height: 510),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = T("gui_title")
        window.delegate = self
        window.isReleasedWhenClosed = false
        window.minSize = NSSize(width: 630, height: 420)
        let title = NSTextField(labelWithString: T("gui_title"))
        title.font = .boldSystemFont(ofSize: 22)
        status.font = .systemFont(ofSize: 15)
        status.setContentCompressionResistancePriority(.required, for: .vertical)
        let folder = NSTextField(labelWithString: root.lastPathComponent)
        folder.textColor = .secondaryLabelColor
        startButton = NSButton(title: T("gui_start"), target: self, action: #selector(start))
        browserButton = NSButton(title: T("gui_browser"), target: self, action: #selector(openBrowser))
        copyButton = NSButton(title: T("gui_copy"), target: self, action: #selector(copyAddress))
        stopButton = NSButton(title: T("gui_stop"), target: self, action: #selector(stop))
        let records = NSButton(title: T("gui_records"), target: self, action: #selector(openRecords))
        let buttons = NSStackView(views: [startButton, browserButton, copyButton, stopButton, records])
        buttons.orientation = .horizontal
        buttons.spacing = 12
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        log.isEditable = false
        log.isSelectable = true
        log.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        log.autoresizingMask = [.width]
        log.isVerticallyResizable = true
        log.isHorizontallyResizable = false
        log.textContainer?.widthTracksTextView = true
        scroll.documentView = log
        let help = NSTextField(wrappingLabelWithString: T("gui_help"))
        help.textColor = .secondaryLabelColor
        let stack = NSStackView(views: [title, folder, status, buttons, scroll, help])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 15
        stack.translatesAutoresizingMaskIntoConstraints = false
        window.contentView!.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor, constant: 22),
            stack.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor, constant: -22),
            stack.topAnchor.constraint(equalTo: window.contentView!.topAnchor, constant: 22),
            stack.bottomAnchor.constraint(equalTo: window.contentView!.bottomAnchor, constant: -22),
            scroll.widthAnchor.constraint(equalTo: stack.widthAnchor),
            scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 170),
            status.widthAnchor.constraint(equalTo: stack.widthAnchor),
            help.widthAnchor.constraint(equalTo: stack.widthAnchor)
        ])
        let menu = NSMenu()
        let item = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: T("gui_quit_menu"), action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        item.submenu = appMenu
        menu.addItem(item)
        NSApp.mainMenu = menu
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        start()
    }

    func append(_ text: String) {
        let clean = text.replacingOccurrences(of: "\u{001B}\\[[0-9;?]*[A-Za-z]", with: "", options: .regularExpression)
        log.textStorage?.append(NSAttributedString(string: clean, attributes: [.font: NSFont.monospacedSystemFont(ofSize: 11, weight: .regular), .foregroundColor: NSColor.textColor]))
        if log.string.count > 100_000 { log.textStorage?.deleteCharacters(in: NSRange(location: 0, length: 30_000)) }
        log.scrollToEndOfDocument(nil)
    }

    @objc func start() {
        guard process == nil else { return }
        guard FileManager.default.fileExists(atPath: root.appendingPathComponent("start_mac.command").path) else {
            status.stringValue = T("gui_app_alone")
            return
        }
        canStop = false; requestedStop = false; closeAfterStop = false; waitCount = 0
        browserURL = nil; buffer = Data(); log.string = ""
        startButton.isEnabled = false; stopButton.isEnabled = false; browserButton.isEnabled = false; copyButton.isEnabled = false
        status.stringValue = T("gui_preparing")
        let task = Process()
        let stdinPipe = Pipe(), stdoutPipe = Pipe()
        input = stdinPipe; output = stdoutPipe; process = task
        task.executableURL = URL(fileURLWithPath: "/bin/bash")
        task.arguments = [root.appendingPathComponent("start_mac.command").path, "--gui-worker"]
        task.currentDirectoryURL = root
        task.standardInput = stdinPipe
        task.standardOutput = stdoutPipe
        task.standardError = stdoutPipe
        stdoutPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            DispatchQueue.main.async { self?.consume(data) }
        }
        task.terminationHandler = { [weak self] task in
            DispatchQueue.main.async { self?.finished(task.terminationStatus) }
        }
        do { try task.run() }
        catch {
            stdoutPipe.fileHandleForReading.readabilityHandler = nil
            append(error.localizedDescription + "\n")
            finished(1)
        }
    }

    func consume(_ data: Data) {
        buffer.append(data)
        while let newline = buffer.firstIndex(of: 10) {
            let lineData = buffer.prefix(upTo: newline)
            buffer.removeSubrange(...newline)
            let line = String(decoding: lineData, as: UTF8.self)
            if line.hasPrefix("EZR_UI\t") {
                let fields = line.components(separatedBy: "\t")
                guard fields.count >= 2 else { continue }
                switch fields[1] {
                case "BUILD_FAILED":
                    append(T("gui_build_failed") + "\n")
                case "READY":
                    if fields.count == 3, let url = URL(string: fields[2]), url.scheme == "http", url.host == "localhost" {
                        browserURL = url; browserButton.isEnabled = true; copyButton.isEnabled = true
                        canStop = true; stopButton.isEnabled = true
                        status.stringValue = T("gui_ready")
                        openBrowser()
                    }
                case "WAIT_RECORDS":
                    let closing = closeAfterStop
                    requestedStop = false; closeAfterStop = false; canStop = true; stopButton.isEnabled = true
                    waitCount += 1
                    status.stringValue = T("gui_wait_records")
                    if waitCount >= 2 { askForceStop(closing: closing) }
                case "STOP_FAILED":
                    requestedStop = false; closeAfterStop = false; canStop = true; stopButton.isEnabled = true
                    status.stringValue = T("gui_stop_failed")
                default: break
                }
            } else { append(line + "\n") }
        }
    }

    @objc func stop() {
        guard process != nil, canStop, !requestedStop else { return }
        requestedStop = true; stopButton.isEnabled = false
        status.stringValue = T("gui_checking_stop")
        send("stop\n")
    }

    func askForceStop(closing: Bool) {
        let alert = NSAlert()
        alert.messageText = T("gui_force_title")
        alert.informativeText = T("gui_force_text")
        alert.addButton(withTitle: T("gui_wait_button"))
        alert.addButton(withTitle: T("gui_force_button"))
        guard alert.runModal() == .alertSecondButtonReturn, process != nil, canStop, !requestedStop else { return }
        requestedStop = true; stopButton.isEnabled = false; closeAfterStop = closing
        status.stringValue = T("gui_force_stopping")
        send("force-stop\n")
    }

    func send(_ text: String) {
        do { try input?.fileHandleForWriting.write(contentsOf: Data(text.utf8)) }
        catch { requestedStop = false; stopButton.isEnabled = true; append(error.localizedDescription + "\n") }
    }

    @objc func openBrowser() {
        guard let url = browserURL else { return }
        openURL(url)
    }
    @objc func copyAddress() {
        guard let url = browserURL else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(url.absoluteString, forType: .string)
        status.stringValue = T("gui_copied_mac")
    }
    func openURL(_ url: URL) {
        let opener = Process()
        let errors = Pipe()
        opener.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        opener.arguments = [url.absoluteString]
        opener.standardError = errors
        opener.terminationHandler = { [weak self] task in
            let message = String(decoding: errors.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
            DispatchQueue.main.async {
                if task.terminationStatus != 0 {
                    self?.append(message + "\n")
                    self?.status.stringValue = self?.T("gui_open_failed") ?? ""
                }
            }
        }
        do { try opener.run() }
        catch { append(error.localizedDescription + "\n") }
    }
    @objc func openRecords() {
        let folder = root.appendingPathComponent("app/projects")
        openURL(FileManager.default.fileExists(atPath: folder.path) ? folder : root)
    }
    func finished(_ code: Int32) {
        process = nil; input = nil; canStop = false
        startButton.isEnabled = true; stopButton.isEnabled = false; browserButton.isEnabled = false; copyButton.isEnabled = false
        status.stringValue = code == 0
            ? T("gui_stopped")
            : T("gui_failed", "\(code)")
        if code == 0 && closeAfterStop { NSApp.terminate(nil) }
    }
    func requestClose() -> Bool {
        guard process != nil else { return true }
        let alert = NSAlert()
        if canStop && !requestedStop {
            alert.messageText = T("gui_quit_title")
            alert.informativeText = T("gui_quit_text")
            alert.addButton(withTitle: T("gui_quit_button"))
            alert.addButton(withTitle: T("gui_back_button"))
            if alert.runModal() == .alertFirstButtonReturn { closeAfterStop = true; stop() }
        } else {
            alert.messageText = T("gui_busy")
            alert.addButton(withTitle: T("gui_back_button")); alert.runModal()
        }
        return false
    }
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if requestClose() { NSApp.terminate(nil) }
        return false
    }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        requestClose() ? .terminateNow : .terminateCancel
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        window.makeKeyAndOrderFront(nil); return true
    }
}
let application = NSApplication.shared
let delegate = Launcher()
application.setActivationPolicy(.regular)
application.delegate = delegate
application.run()
