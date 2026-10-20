// Flippy.app's executable: starts the Python daemon as a child and waits for it.
//
// The daemon runs as a child (not exec'd) so macOS treats Flippy.app as the
// "responsible" process: the Screen Recording permission is granted to Flippy,
// not to whatever terminal installed it.
//
// Two kinds of app:
// - Installed from a checkout or download (scripts/install_mac.sh): Info.plist's FlippyRoot is that folder, and
//   the daemon runs from its .venv.
// - Self-contained (scripts/build_app.sh, the Flippy.dmg you drag to Applications): FlippyBundled. Python and the
//   packages are inside the app; the code runs from ~/Library/Application Support/Flippy, where updates install
//   new copies, so the app itself never changes and macOS keeps its permissions. The layout and the rules for
//   it are in flippy/bundle.py; this side only compares strings and moves folders.
import Foundation

let info = Bundle.main.infoDictionary ?? [:]
let bundled = info["FlippyBundled"] as? Bool ?? false
let resources = Bundle.main.resourcePath ?? ""
// Where Application Support and Logs are: the user's home, unless the app says otherwise (tests use a scratch one).
let home = (info["FlippyHome"] as? String).map { URL(fileURLWithPath: $0) }
    ?? FileManager.default.homeDirectoryForCurrentUser

func read(_ path: String) -> String? {
    (try? String(contentsOfFile: path, encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines)
}

func versionParts(_ path: String) -> [Int] {
    (read(path + "/VERSION") ?? "").split(separator: ".").map { Int($0) ?? 0 }
}

func newer(_ a: [Int], than b: [Int]) -> Bool {
    for i in 0..<max(a.count, b.count) {
        let x = i < a.count ? a[i] : 0, y = i < b.count ? b[i] : 0
        if x != y { return x > y }
    }
    return false
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(("Flippy.app: " + message + "\n").data(using: .utf8)!)
    exit(1)
}

/// The shared library behind a venv's python (a framework build's Python, or lib/libpython3.X.dylib), or nil for a
/// static build, which can't be hosted.
func pythonLibrary(_ venvPython: String) -> String? {
    let real = URL(fileURLWithPath: venvPython).resolvingSymlinksInPath()
    let prefix = real.deletingLastPathComponent().deletingLastPathComponent()
    let candidates = [prefix.appendingPathComponent("Python").path,  // Homebrew's and python.org's framework builds
                      prefix.appendingPathComponent("lib/lib" + real.lastPathComponent + ".dylib").path]
    return candidates.first { FileManager.default.fileExists(atPath: $0) }
}

// The daemon's process, from a checkout's .venv: Flippy.app runs this executable again with FLIPPY_PYTHON_HOST naming
// that venv's python, and it runs Python in this process. Started as .venv/bin/python, macOS files the daemon under
// Python.app, so while the app window is open the Dock calls it "Python"; as Flippy.app's executable it's Flippy.
if let venvPython = ProcessInfo.processInfo.environment["FLIPPY_PYTHON_HOST"] {
    unsetenv("FLIPPY_PYTHON_HOST")
    guard let lib = pythonLibrary(venvPython), let handle = dlopen(lib, RTLD_NOW | RTLD_GLOBAL),
          let pyMain = dlsym(handle, "Py_BytesMain") else { fail("couldn't load the Python behind \(venvPython)") }
    setenv("__PYVENV_LAUNCHER__", venvPython, 1)  // Python finds the venv as if started as venvPython, then unsets it
    typealias PyBytesMain = @convention(c) (Int32, UnsafeMutablePointer<UnsafeMutablePointer<CChar>?>?) -> Int32
    var argv = ([venvPython] + CommandLine.arguments.dropFirst()).map { strdup($0) } + [nil]  // -m flippy.daemon ...
    exit(unsafeBitCast(pyMain, to: PyBytesMain.self)(Int32(argv.count - 1), &argv))
}

/// The copy of the code the daemon runs (flippy/bundle.py has the layout). The app's own copy is installed when
/// there's no usable copy, when the current one was checked against a different runtime than this app's (an older
/// or newer Flippy.dmg), or when this app is newer; otherwise the current one, which updates may have moved past
/// this app's, is kept. Returns its real path, so the running daemon never follows a later swap.
func bundledRoot(_ support: String) -> String {
    guard let runtime = info["FlippyRuntime"] as? String, !runtime.isEmpty else { fail("FlippyRuntime missing") }
    let fm = FileManager.default
    let versions = support + "/versions", link = support + "/code", shipped = resources + "/code"
    do {
        try fm.createDirectory(atPath: versions, withIntermediateDirectories: true)
    } catch {
        fail("couldn't set up \(support): \(error)")
    }
    // One launcher or updater at a time changes the layout.
    let lock = open(support + "/.lock", O_CREAT | O_RDWR, 0o644)
    if lock >= 0 { flock(lock, LOCK_EX) }
    defer { if lock >= 0 { close(lock) } }

    // The first self-contained app kept a plain folder at code: keep it as an old copy.
    var isDir: ObjCBool = false
    if (try? fm.destinationOfSymbolicLink(atPath: link)) == nil, fm.fileExists(atPath: link, isDirectory: &isDir) {
        if isDir.boolValue {  // moved aside, never deleted: it may be the only copy of the code
            var dest = versions + "/0-legacy", n = 1
            while fm.fileExists(atPath: dest) { dest = versions + "/0-legacy-\(n)"; n += 1 }
            do { try fm.moveItem(atPath: link, toPath: dest) } catch { fail("couldn't move \(link) aside: \(error)") }
        } else {
            try? fm.removeItem(atPath: link)
        }
    }

    var current: String? = nil
    if let target = try? fm.destinationOfSymbolicLink(atPath: link) {
        let path = target.hasPrefix("/") ? target : support + "/" + target
        if fm.fileExists(atPath: path + "/flippy/daemon.py") { current = path }
    }
    if current == nil || read(current! + "/RUNTIME") != runtime || newer(versionParts(shipped), than: versionParts(current!)) {
        // '<ms>-<version>', always after every copy already there (flippy/bundle.py next_name)
        let taken = ((try? fm.contentsOfDirectory(atPath: versions)) ?? []).compactMap { Int64($0.split(separator: "-").first ?? "") }
        let stamp = max(Int64(Date().timeIntervalSince1970 * 1000), (taken.max() ?? -1) + 1)
        let name = "\(stamp)-\(read(shipped + "/VERSION") ?? "0")"
        let staging = versions + "/.staging-" + name, slot = versions + "/" + name, tmp = link + ".new"
        do {
            try? fm.removeItem(atPath: staging)
            try fm.copyItem(atPath: shipped, toPath: staging)
            try fm.moveItem(atPath: staging, toPath: slot)
            try? fm.removeItem(atPath: tmp)
            try fm.createSymbolicLink(atPath: tmp, withDestinationPath: "versions/" + name)
        } catch {
            fail("couldn't install the code in \(versions): \(error)")
        }
        guard rename(tmp, link) == 0 else { fail("couldn't switch \(link) to \(name)") }  // atomic
        current = slot
    }

    // Keep the current copy and the newest other one (a Flippy still running may be using it, and it's there to go
    // back to by hand); drop older ones and any half-copied folder.
    let keep = (current! as NSString).lastPathComponent
    let others = ((try? fm.contentsOfDirectory(atPath: versions)) ?? []).filter { $0 != keep }.sorted { a, b in
        (Int64(a.split(separator: "-").first ?? "") ?? -1) > (Int64(b.split(separator: "-").first ?? "") ?? -1)
    }
    let rollback = others.first { !$0.hasPrefix(".") }
    for name in others where name != rollback {
        try? fm.removeItem(atPath: versions + "/" + name)
    }
    return current!
}

let profile = info["FlippyProfile"] as? String ?? "default"
guard profile == "default" || profile == "demo" else { exit(1) }
let appName = profile == "demo" ? "Flippy Demo" : "Flippy"
let support = home.appendingPathComponent("Library/Application Support/" + appName).path
let root: String
if bundled {
    root = bundledRoot(support)
} else {
    guard let r = info["FlippyRoot"] as? String else { fail("FlippyRoot missing from Info.plist") }
    root = r
}
let logName = profile == "demo" ? "flippy-demo.log" : "flippy.log"
let logURL = home.appendingPathComponent("Library/Logs/" + logName)
try? FileManager.default.createDirectory(at: logURL.deletingLastPathComponent(), withIntermediateDirectories: true)
FileManager.default.createFile(atPath: logURL.path, contents: nil, attributes: nil)  // no-op if it exists
let log = try? FileHandle(forWritingTo: logURL)
log?.seekToEndOfFile()

let daemon = Process()
var env = ProcessInfo.processInfo.environment
env.removeValue(forKey: "ANTHROPIC_API_KEY")  // bill the Pro/Max subscription, never the API
env["FLIPPY_APP"] = Bundle.main.bundlePath
env["FLIPPY_PROFILE"] = profile
if bundled {  // the app's own Python, nothing from the user's
    daemon.executableURL = URL(fileURLWithPath: resources + "/python/bin/python3")
    daemon.arguments = ["-m", "flippy.daemon"] + Array(CommandLine.arguments.dropFirst())
    daemon.currentDirectoryURL = URL(fileURLWithPath: root)
    env["FLIPPY_BUNDLED"] = "1"
    env["FLIPPY_RUNTIME"] = info["FlippyRuntime"] as? String  // which releases this app can run (flippy/bundle.py)
    env["FLIPPY_CODE_HOME"] = support  // where updates install new copies
    env["PYTHONNOUSERSITE"] = "1"
    env.removeValue(forKey: "PYTHONPATH")
    env.removeValue(forKey: "PYTHONHOME")
    env["SSL_CERT_FILE"] = resources + "/cacert.pem"  // this Python has no access to the system's certificates
} else if let me = Bundle.main.executablePath, pythonLibrary(root + "/.venv/bin/python") != nil {
    daemon.executableURL = URL(fileURLWithPath: me)  // hosts Python itself (FLIPPY_PYTHON_HOST, above)
    daemon.arguments = ["-m", "flippy.daemon"] + Array(CommandLine.arguments.dropFirst())
    daemon.currentDirectoryURL = URL(fileURLWithPath: root)
    env["FLIPPY_PYTHON_HOST"] = root + "/.venv/bin/python"
} else {
    daemon.executableURL = URL(fileURLWithPath: root + "/bin/flippy-daemon")
    daemon.arguments = Array(CommandLine.arguments.dropFirst())
}
daemon.environment = env
if let log = log {
    daemon.standardOutput = log
    daemon.standardError = log
}

var signalSources: [DispatchSourceSignal] = []

// Quitting Flippy.app (logout, Activity Monitor) stops the daemon too.
for sig in [SIGTERM, SIGINT, SIGHUP] {
    signal(sig, SIG_IGN)
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    src.setEventHandler { daemon.terminate() }
    src.resume()
    signalSources.append(src)
}

do {
    try daemon.run()
} catch {
    fail("couldn't start the daemon in \(root): \(error)")
}
daemon.terminationHandler = { p in exit(p.terminationStatus) }
dispatchMain()
