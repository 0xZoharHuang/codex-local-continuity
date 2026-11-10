import AppKit
import Foundation

enum SyncTrigger: Equatable {
    case startupOffline
    case terminated(pid_t)
}

// Called on the main queue. A later exit supersedes pending work from an
// earlier exit, including one that happened while the worker was running.
struct SyncQueue {
    private(set) var pending: SyncTrigger?
    private(set) var busy = false

    mutating func request(_ trigger: SyncTrigger) {
        pending = trigger
    }

    mutating func applicationLaunched() {
        pending = nil
    }

    mutating func take() -> SyncTrigger? {
        guard !busy, let trigger = pending else { return nil }
        pending = nil
        busy = true
        return trigger
    }

    mutating func finished() {
        busy = false
    }
}

struct AppTarget {
    let bundleID: String
    let bundles: [URL]
    let executables: Set<String>

    static let codex: AppTarget = {
        let directories = [URL(fileURLWithPath: "/Applications", isDirectory: true),
                           FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Applications")]
        let bundles = directories.flatMap { directory in
            ["ChatGPT.app", "Codex.app"].map { directory.appendingPathComponent($0) }
        }
        return AppTarget(bundleID: "com.openai.codex", bundles: bundles, executables: ["ChatGPT", "Codex"])
    }()

    func matches(_ app: NSRunningApplication) -> Bool {
        guard app.processIdentifier > 0, app.bundleIdentifier == bundleID,
              let bundle = app.bundleURL?.standardizedFileURL,
              bundles.contains(where: { $0.standardizedFileURL.path == bundle.path }),
              let executable = app.executableURL?.standardizedFileURL,
              executables.contains(executable.lastPathComponent) else { return false }
        return bundle.appendingPathComponent("Contents/MacOS")
            .appendingPathComponent(executable.lastPathComponent).standardizedFileURL.path == executable.path
    }
}

@MainActor
final class Watcher: NSObject {
    private let workspace = NSWorkspace.shared
    private let target = AppTarget.codex
    private let workerURL: URL
    private var observers: [NSObjectProtocol] = []
    private var runningAppsObserver: NSKeyValueObservation?
    private var exitSources: [pid_t: DispatchSourceProcess] = [:]
    private var lastCompletedPID: pid_t?
    private var queue = SyncQueue()
    private var worker: Process?

    init(workerURL: URL) {
        self.workerURL = workerURL
    }

    func start() {
        observe(NSWorkspace.willLaunchApplicationNotification, onTermination: false)
        observe(NSWorkspace.didTerminateApplicationNotification, onTermination: true)
        runningAppsObserver = workspace.observe(\.runningApplications, options: []) { [weak self] _, _ in
            DispatchQueue.main.async { self?.discoverRunningApps() }
        }
        discoverRunningApps()
        if exitSources.isEmpty {
            queue.request(.startupOffline)
            drain()
        }
    }

    private func observe(_ name: Notification.Name, onTermination: Bool) {
        let observer = workspace.notificationCenter.addObserver(forName: name, object: nil, queue: .main) {
            [weak self] notification in
            guard let app = notification.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication
            else { return }
            MainActor.assumeIsolated {
                if onTermination {
                    self?.applicationTerminated(app)
                } else {
                    self?.applicationLaunched(app)
                }
            }
        }
        observers.append(observer)
    }

    private func discoverRunningApps() {
        for app in NSRunningApplication.runningApplications(withBundleIdentifier: target.bundleID) where !app.isTerminated {
            applicationLaunched(app)
        }
    }

    private func applicationLaunched(_ app: NSRunningApplication) {
        guard target.matches(app) else { return }
        let pid = app.processIdentifier
        guard exitSources[pid] == nil, kill(pid, 0) == 0 else { return }
        if lastCompletedPID == pid { lastCompletedPID = nil }
        queue.applicationLaunched()
        let source = DispatchSource.makeProcessSource(identifier: pid, eventMask: .exit, queue: .main)
        source.setEventHandler { [weak self] in
            self?.processExited(pid)
        }
        exitSources[pid] = source
        source.resume()
    }

    private func applicationTerminated(_ app: NSRunningApplication) {
        let pid = app.processIdentifier
        guard exitSources[pid] != nil || target.matches(app) else { return }
        // If Launch Services announces termination before exit, the kernel
        // notification will run after the process exits.
        if exitSources[pid] != nil && kill(pid, 0) == 0 { return }
        processExited(pid)
    }

    private func processExited(_ pid: pid_t) {
        guard lastCompletedPID != pid else { return }
        lastCompletedPID = pid
        exitSources.removeValue(forKey: pid)?.cancel()
        guard exitSources.isEmpty else { return }
        queue.request(.terminated(pid))
        drain()
    }

    private func drain() {
        guard let trigger = queue.take() else { return }
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        task.arguments = ["-I", "-B", workerURL.path, "sync"]
        var environment = ProcessInfo.processInfo.environment
        environment.removeValue(forKey: "CODEX_CONTINUITY_EXIT_PID")
        switch trigger {
        case .startupOffline:
            environment["CODEX_CONTINUITY_TRIGGER"] = "startup-offline"
        case .terminated(let pid):
            environment["CODEX_CONTINUITY_TRIGGER"] = "termination"
            environment["CODEX_CONTINUITY_EXIT_PID"] = String(pid)
        }
        task.environment = environment
        task.standardInput = FileHandle.nullDevice
        task.terminationHandler = { [weak self] finished in
            DispatchQueue.main.async {
                guard let self, self.worker === finished else { return }
                self.worker = nil
                self.queue.finished()
                self.drain()
            }
        }
        worker = task
        do {
            try task.run()
        } catch {
            worker = nil
            queue.finished()
            FileHandle.standardError.write(Data("Could not start the Codex continuity worker: \(error)\n".utf8))
        }
    }
}

guard let executable = Bundle.main.executableURL else {
    FileHandle.standardError.write(Data("Could not locate the Codex continuity watcher executable.\n".utf8))
    exit(1)
}
MainActor.assumeIsolated {
    let watcher = Watcher(workerURL: executable.resolvingSymlinksInPath().deletingLastPathComponent()
        .appendingPathComponent("sync.py"))
    watcher.start()
    withExtendedLifetime(watcher) {
        RunLoop.main.run()
    }
}
