import AppKit
import Darwin
import Foundation
import Network
import Security
import SwiftUI

private let keychainService = "dev.local.VidaaRemoteMac"
private let support = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Application Support/VIDAA Remote", isDirectory: true)

enum PairingKeychain {
    static func query(_ account: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: keychainService,
         kSecAttrAccount as String: account,
         kSecAttrSynchronizable as String: false]
    }

    static func load(_ account: String) throws -> Any {
        var search = query(account)
        search[kSecReturnData as String] = true
        search[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(search as CFDictionary, &result)
        if status == errSecItemNotFound { return NSNull() }
        guard status == errSecSuccess, let data = result as? Data else {
            throw NSError(domain: NSOSStatusErrorDomain, code: Int(status))
        }
        return try JSONSerialization.jsonObject(with: data)
    }

    static func save(_ account: String, value: Any) throws {
        let data = try JSONSerialization.data(withJSONObject: value)
        let search = query(account)
        let update = [kSecValueData as String: data]
        var status = SecItemUpdate(search as CFDictionary, update as CFDictionary)
        if status == errSecItemNotFound {
            var insert = search
            insert[kSecValueData as String] = data
            insert[kSecAttrLabel as String] = "VIDAA Remote: Mac TV pairing"
            status = SecItemAdd(insert as CFDictionary, nil)
        }
        guard status == errSecSuccess else {
            throw NSError(domain: NSOSStatusErrorDomain, code: Int(status))
        }
    }
}

final class BackendProcess {
    private let process = Process()
    private let input = Pipe()
    private let output = Pipe()
    private let errors = Pipe()
    private let inputLock = NSLock()
    private let keychainQueue = DispatchQueue(label: "vidaa.keychain")
    var onEvent: ((String, Any) -> Void)?

    func start(automation: Bool) throws {
        guard let resources = Bundle.main.resourceURL else { throw CocoaError(.fileNoSuchFile) }
        let config = try Data(contentsOf: resources.appendingPathComponent("runtime.json"))
        let runtime = try JSONSerialization.jsonObject(with: config) as? [String: String]
        process.executableURL = URL(fileURLWithPath: runtime?["python"] ?? "/usr/bin/python3")
        process.arguments = ["-u", resources.appendingPathComponent("worker.py").path] + (automation ? ["--automation"] : [])
        process.standardInput = input
        process.standardOutput = output
        process.standardError = errors
        var env = ProcessInfo.processInfo.environment
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONNOUSERSITE"] = "1"
        env.removeValue(forKey: "PYTHONPATH")
        env.removeValue(forKey: "PYTHONHOME")
        process.environment = env
        process.terminationHandler = { [weak self] _ in
            self?.onEvent?("status", ["state": "offline", "text": "TV backend stopped. Quit and reopen VIDAA Remote."])
        }
        try process.run()
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in self?.readEvents() }
        // Drain diagnostics without logging raw network data or private pipe messages.
        errors.fileHandleForReading.readabilityHandler = { handle in _ = handle.availableData }
    }

    func send(_ object: [String: Any]) {
        guard process.isRunning, var data = try? JSONSerialization.data(withJSONObject: object) else { return }
        data.append(0x0a)
        inputLock.lock()
        defer { inputLock.unlock() }
        do { try input.fileHandleForWriting.write(contentsOf: data) }
        catch { onEvent?("error", "The TV backend is unavailable. Reopen the app.") }
    }

    private func readEvents() {
        var buffer = Data()
        while process.isRunning {
            let data = output.fileHandleForReading.availableData
            if data.isEmpty { break }
            buffer.append(data)
            if buffer.count > 4_194_304 { buffer = Data(); continue }
            while let newline = buffer.firstIndex(of: 0x0a) {
                let line = Data(buffer[..<newline])
                buffer.removeSubrange(...newline)
                guard let object = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any],
                      let event = object["event"] as? String else { continue }
                let value = object["value"] ?? NSNull()
                if event == "keychain", let request = value as? [String: Any] {
                    keychainQueue.async { [weak self] in self?.keychain(request) }
                } else { onEvent?(event, value) }
            }
        }
    }

    private func keychain(_ request: [String: Any]) {
        guard let id = request["id"] as? String, let account = request["account"] as? String,
              account.hasPrefix("tv:"), account.count <= 300 else { return }
        do {
            switch request["method"] as? String {
            case "load": send(["reply": id, "value": try PairingKeychain.load(account)])
            case "save":
                guard let value = request["data"] as? [String: Any] else { return }
                try PairingKeychain.save(account, value: value)
                send(["reply": id, "value": true])
            default: send(["reply": id, "error": true])
            }
        } catch { send(["reply": id, "error": true]) }
    }

    func stop() {
        try? input.fileHandleForWriting.close()
    }
}

struct TVRow: Identifiable {
    let id: Int
    let name: String
    let payload: [String: Any]
}

final class RemoteModel: ObservableObject {
    @Published var state = "offline"
    @Published var status = "Find your TV to get started"
    @Published var tvName = "VIDAA Remote"
    @Published var model = ""
    @Published var host = ""
    @Published var volume: Double = 0
    @Published var sources: [TVRow] = []
    @Published var apps: [TVRow] = []
    @Published var discovered: [TVRow] = []
    @Published var pairing = false
    @Published var pin = ""
    @Published var busy = false
    @Published var error = ""
    @Published var showingError = false
    @Published var finding = false
    @Published var tab = 0
    private let backend = BackendProcess()
    private var initialDevice = true
    private var networkProbe: NWConnection?
    let preview: Bool
    var ready: Bool { state == "ready" }

    init(preview: Bool) {
        self.preview = preview
        if preview {
            tvName = "Living Room TV"; model = "VIDAA"; host = "192.0.2.10"
            state = "ready"; status = "Demo preview"; volume = 15
            sources = ["TV", "HDMI1", "HDMI2", "HDMI3", "HDMI4"].enumerated().map {
                TVRow(id: $0.offset, name: $0.element, payload: ["sourceid": $0.element])
            }
            apps = ["YouTube", "Netflix", "Prime Video"].enumerated().map {
                TVRow(id: $0.offset, name: $0.element, payload: [:])
            }
            return
        }
        backend.onEvent = { [weak self] event, value in
            DispatchQueue.main.async { self?.receive(event, value) }
        }
        do {
            try backend.start(automation: CommandLine.arguments.contains("--automation"))
        } catch { fail("Could not start the TV backend. Reinstall the app using the macOS instructions.") }
    }

    func action(_ method: String, _ args: [String: Any] = [:]) {
        if preview { return }
        if method == "connect", !host.isEmpty {
            networkProbe?.cancel()
            let probe = NWConnection(host: NWEndpoint.Host(host), port: 36669, using: .tcp)
            networkProbe = probe
            state = "connecting"; busy = true
            status = "Waiting for TV and Local Network access"
            probe.stateUpdateHandler = { [weak self, weak probe] state in
                DispatchQueue.main.async {
                    guard let self, let probe, self.networkProbe === probe else { return }
                    switch state {
                    case .ready:
                        self.networkProbe = nil
                        probe.cancel()
                        self.backend.send(["method": method, "args": args])
                    case .waiting:
                        self.status = "Check TV connection and allow Local Network access"
                    case .failed:
                        self.networkProbe = nil
                        self.fail("Cannot reach the TV. Check its address and Mac Local Network permission, then reconnect.")
                    default: break
                    }
                }
            }
            probe.start(queue: DispatchQueue(label: "vidaa.local-network"))
        } else {
            if method == "disconnect" { networkProbe?.cancel(); networkProbe = nil }
            backend.send(["method": method, "args": args])
        }
    }

    func find() { finding = true; action("discover") }
    func connectAddress() { action("manual", ["host": host]); action("connect") }
    func pair() { pin = ""; action("pair"); pairing = true }
    func submit() { action("pin", ["pin": pin]); pin = "" }
    func key(_ key: String) { if ready { action("key", ["key": key]) } }
    func fail(_ message: String) {
        error = message; showingError = true; busy = false
        if !ready { state = "error"; status = "Connection needs attention" }
    }
    func stop() { networkProbe?.cancel(); backend.stop() }

    func importCertificate() {
        let panel = NSOpenPanel()
        panel.title = "Import VIDAA client certificate and private key (PEM)"
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        panel.begin { [weak self] result in
            if result == .OK, let path = panel.url?.path { self?.action("import-certificate", ["path": path]) }
        }
    }

    private func rows(_ value: Any, kind: String) -> [TVRow] {
        (value as? [[String: Any]] ?? []).enumerated().compactMap { index, item in
            let name: String?
            if kind == "sources" {
                name = item["displayname"] as? String ?? item["sourcename"] as? String ?? item["sourceid"] as? String
            } else { name = item["name"] as? String ?? item["appName"] as? String ?? item["title"] as? String }
            guard let name else { return nil }
            return TVRow(id: index, name: name, payload: item)
        }
    }

    private func receive(_ event: String, _ value: Any) {
        switch event {
        case "status":
            let data = value as? [String: String] ?? [:]
            state = data["state"] ?? "offline"; status = data["text"] ?? state
            busy = state == "connecting"
            if ready { pairing = false; pin = "" }
        case "device":
            let data = value as? [String: Any] ?? [:]
            tvName = data["name"] as? String ?? "VIDAA Remote"
            model = data["model"] as? String ?? ""
            host = data["host"] as? String ?? ""
            if initialDevice {
                initialDevice = false
                if !host.isEmpty { action("connect") }
            }
        case "sources": sources = rows(value, kind: "sources")
        case "apps": apps = rows(value, kind: "apps")
        case "volume": if let number = value as? NSNumber { volume = number.doubleValue }
        case "pin-needed", "pair-acknowledged": pairing = true
        case "paired": pairing = false; pin = ""
        case "discovered":
            finding = false
            discovered = (value as? [[String: Any]] ?? []).enumerated().map {
                TVRow(id: $0.offset, name: $0.element["name"] as? String ?? "VIDAA TV", payload: $0.element)
            }
            if discovered.isEmpty { fail("No TV found. Check that the TV is on, or enter its network address.") }
        case "certificate-imported": status = "Certificate imported. Connect to your TV."
        case "error": fail((value as? String ?? "The TV action failed.").replacingOccurrences(of: "GNOME Keyring", with: "macOS Keychain"))
        default: break
        }
    }
}

struct RemoteView: View {
    @ObservedObject var remote: RemoteModel

    func key(_ title: String, _ symbol: String, _ code: String, width: CGFloat = 74) -> some View {
        Button { remote.key(code) } label: {
            VStack(spacing: 6) { Image(systemName: symbol).font(.title3); Text(title).font(.caption) }
                .frame(width: width, height: 48)
        }.buttonStyle(.bordered).disabled(!remote.ready).help(title)
    }

    var body: some View {
        VStack(spacing: 18) {
            HStack(spacing: 14) {
                Image(systemName: "tv").font(.system(size: 32)).foregroundStyle(.tint)
                VStack(alignment: .leading, spacing: 4) {
                    Text(remote.tvName).font(.title2.weight(.semibold))
                    if !remote.model.isEmpty { Text(remote.model).font(.caption).foregroundStyle(.secondary) }
                    HStack {
                        Circle().fill(remote.ready ? .green : .orange).frame(width: 7, height: 7)
                        Text(remote.status).font(.caption).foregroundStyle(.secondary)
                    }
                }
                Spacer()
                if remote.busy { ProgressView().controlSize(.small) }
                Button { remote.action("connect") } label: { Image(systemName: "arrow.clockwise") }.help("Reconnect")
            }
            HStack {
                TextField("TV IP address or hostname", text: $remote.host).onSubmit { remote.connectAddress() }
                Button("Connect") { remote.connectAddress() }.disabled(remote.host.isEmpty || remote.busy)
                Button("Find TV") { remote.find() }.disabled(remote.finding)
            }
            if !remote.discovered.isEmpty {
                Menu("Discovered TVs") {
                    ForEach(remote.discovered) { tv in
                        Button(tv.name) {
                            remote.host = tv.payload["host"] as? String ?? ""
                            remote.connectAddress()
                        }
                    }
                }
            }
            if remote.state == "pairing" {
                Button("Pair this Mac with TV") { remote.pair() }.buttonStyle(.borderedProminent)
            }
            Picker("Controls", selection: $remote.tab) {
                Text("Remote").tag(0); Text("Inputs").tag(1); Text("Apps").tag(2)
            }.pickerStyle(.segmented)
            if remote.tab == 0 {
                VStack(spacing: 14) {
                    HStack(spacing: 10) {
                        key("Home", "house", "KEY_HOME")
                        key("Back", "arrow.uturn.backward", "KEY_RETURNS")
                        key("Menu", "line.3.horizontal", "KEY_MENU")
                    }
                    VStack(spacing: 8) {
                        key("Up", "chevron.up", "KEY_UP")
                        HStack(spacing: 8) {
                            key("Left", "chevron.left", "KEY_LEFT")
                            key("OK", "checkmark", "KEY_OK")
                            key("Right", "chevron.right", "KEY_RIGHT")
                        }
                        key("Down", "chevron.down", "KEY_DOWN")
                    }
                    Divider()
                    HStack(spacing: 10) {
                        key("Volume down", "speaker.minus", "KEY_VOLUMEDOWN")
                        Text("\(Int(remote.volume))").font(.title2.monospacedDigit()).frame(width: 36)
                        key("Volume up", "speaker.plus", "KEY_VOLUMEUP")
                        key("Mute", "speaker.slash", "KEY_MUTE", width: 60)
                    }
                    Slider(value: $remote.volume, in: 0...100, step: 1, onEditingChanged: { editing in
                        if !editing { remote.action("volume", ["value": Int(remote.volume)]) }
                    }).disabled(!remote.ready).accessibilityLabel("TV volume")
                    HStack { key("Channel down", "minus", "KEY_CHANNELDOWN", width: 120)
                        key("Channel up", "plus", "KEY_CHANNELUP", width: 120) }
                    HStack(spacing: 8) {
                        key("Play", "play", "KEY_PLAY", width: 60)
                        key("Pause", "pause", "KEY_PAUSE", width: 60)
                        key("Stop", "stop", "KEY_STOP", width: 60)
                        key("Power", "power", "KEY_POWER", width: 60)
                    }
                }.padding(.vertical, 4)
            } else {
                let rows = remote.tab == 1 ? remote.sources : remote.apps
                if rows.isEmpty { Text("Connect to load TV \(remote.tab == 1 ? "inputs" : "apps").").foregroundStyle(.secondary).padding(40) }
                List(rows) { row in
                    Button {
                        remote.action(remote.tab == 1 ? "source" : "app", [remote.tab == 1 ? "source" : "app": row.payload])
                    } label: {
                        HStack { Image(systemName: remote.tab == 1 ? "rectangle.connected.to.line.below" : "square.grid.2x2")
                            Text(row.name); Spacer(); Image(systemName: "chevron.right").foregroundStyle(.secondary) }
                        .padding(.vertical, 7)
                    }.buttonStyle(.plain).disabled(!remote.ready)
                }.frame(minHeight: 330)
            }
            Spacer(minLength: 0)
            HStack {
                Text(remote.preview ? "Demo data" : "Pairing uses macOS Keychain").font(.caption).foregroundStyle(.secondary)
                Spacer()
                Menu { Button("Import certificate…") { remote.importCertificate() }
                    Button("Wake TV") { remote.action("wake") }
                    Button("Disconnect") { remote.action("disconnect") }
                } label: { Image(systemName: "gearshape") }
            }
        }
        .padding(24).frame(minWidth: 400, idealWidth: 450, maxWidth: 700, minHeight: 690)
        .sheet(isPresented: $remote.pairing) {
            VStack(spacing: 18) {
                Image(systemName: "tv.badge.wifi").font(.largeTitle).foregroundStyle(.tint)
                Text("Pair this Mac").font(.title2.bold())
                Text("Enter the four-digit code displayed on your TV.").multilineTextAlignment(.center)
                TextField("Four-digit code", text: $remote.pin).textFieldStyle(.roundedBorder)
                    .frame(width: 150).onSubmit { remote.submit() }
                HStack {
                    Button("Cancel") { remote.pairing = false; remote.pin = "" }
                    Button("New code") { remote.pair() }
                    Button("Pair") { remote.submit() }.buttonStyle(.borderedProminent)
                        .disabled(remote.pin.count != 4 || remote.pin.contains(where: { !$0.isASCII || !$0.isNumber }))
                }
            }.padding(30).frame(width: 340)
        }
        .alert("VIDAA Remote", isPresented: $remote.showingError) { Button("OK", role: .cancel) {} }
            message: { Text(remote.error) }
    }
}

final class ApplicationDelegate: NSObject, NSApplicationDelegate {
    var remote: RemoteModel?
    private var window: NSWindow?
    func applicationDidFinishLaunching(_ notification: Notification) {
        let arguments = CommandLine.arguments
        let model = RemoteModel(preview: arguments.contains("--preview"))
        remote = model
        let view = NSHostingView(rootView: RemoteView(remote: model))
        let main = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 450, height: 760),
            styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        main.title = "VIDAA Remote"
        main.isReleasedWhenClosed = false
        main.contentView = view
        main.center()
        window = main
        installMenus()
        main.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        if model.preview, let index = arguments.firstIndex(of: "--screenshot"), arguments.count > index + 1 {
            let path = arguments[index + 1]
            let number = main.windowNumber
            DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
                var info = stat()
                guard lstat(path, &info) != 0, errno == ENOENT else {
                    fputs("Use an unused preview capture destination.\n", stderr); return
                }
                // Capture only this app's own fictional preview window.
                DispatchQueue.global(qos: .userInitiated).async {
                    let capture = Process()
                    capture.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
                    capture.arguments = ["-x", "-l", String(number), path]
                    do { try capture.run(); capture.waitUntilExit() }
                    catch { fputs("Could not capture the preview window.\n", stderr) }
                }
            }
        }
        if let index = arguments.firstIndex(of: "--quit-after"), arguments.count > index + 1,
           let seconds = Double(arguments[index + 1]), seconds > 0 {
            DispatchQueue.main.asyncAfter(deadline: .now() + seconds) { NSApp.terminate(nil) }
        }
    }
    private func installMenus() {
        let menu = NSMenu()
        let appItem = NSMenuItem()
        appItem.title = "VIDAA Remote"
        let appMenu = NSMenu(title: "VIDAA Remote")
        appMenu.addItem(withTitle: "Quit VIDAA Remote", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        menu.addItem(appItem)
        let editItem = NSMenuItem(title: "Edit", action: nil, keyEquivalent: "")
        let editMenu = NSMenu(title: "Edit")
        for (title, selector, key) in [("Cut", #selector(NSText.cut(_:)), "x"),
            ("Copy", #selector(NSText.copy(_:)), "c"), ("Paste", #selector(NSText.paste(_:)), "v"),
            ("Select All", #selector(NSText.selectAll(_:)), "a")] {
            editMenu.addItem(withTitle: title, action: selector, keyEquivalent: key)
        }
        editItem.submenu = editMenu
        menu.addItem(editItem)
        let tvItem = NSMenuItem()
        tvItem.title = "TV"
        let tvMenu = NSMenu(title: "TV")
        for (title, selector, key) in [
            ("Reconnect", #selector(reconnect), "r"), ("Pair this Mac", #selector(pair), ""),
            ("Find TV", #selector(find), "f"), ("Import certificate…", #selector(importCertificate), "")
        ] {
            let item = NSMenuItem(title: title, action: selector, keyEquivalent: key)
            item.target = self
            tvMenu.addItem(item)
        }
        tvItem.submenu = tvMenu
        menu.addItem(tvItem)
        NSApp.mainMenu = menu
    }
    @objc private func reconnect() { remote?.action("connect") }
    @objc private func pair() { if remote?.state == "pairing" { remote?.pair() } }
    @objc private func find() { remote?.find() }
    @objc private func importCertificate() { remote?.importCertificate() }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        window?.makeKeyAndOrderFront(nil)
        return true
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationWillTerminate(_ notification: Notification) { remote?.stop() }
}

@main
enum VidaaRemoteApp {
    static func main() {
        if let index = CommandLine.arguments.firstIndex(of: "--command"), CommandLine.arguments.count > index + 1 {
            let command = CommandLine.arguments[index + 1]
            let path = support.appendingPathComponent("commands.fifo").path
            var info = stat()
            guard lstat(path, &info) == 0, info.st_mode & S_IFMT == S_IFIFO,
                  info.st_uid == getuid(), info.st_mode & 0o077 == 0 else {
                fputs("Open VIDAA Remote with --automation first.\n", stderr); exit(1)
            }
            let fd = open(path, O_WRONLY | O_NONBLOCK | O_NOFOLLOW)
            guard fd >= 0 else { fputs("VIDAA Remote is not accepting local commands.\n", stderr); exit(1) }
            guard let data = (command + "\n").data(using: .utf8), data.count <= 4096,
                  (try? JSONSerialization.jsonObject(with: data)) is [String: Any] else {
                close(fd); fputs("Expected a JSON command of at most 4096 bytes.\n", stderr); exit(1)
            }
            let sent = data.withUnsafeBytes { write(fd, $0.baseAddress, $0.count) }
            close(fd); exit(sent == data.count ? 0 : 1)
        }
        let app = NSApplication.shared
        app.setActivationPolicy(.regular)
        let delegate = ApplicationDelegate()
        app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    }
}
