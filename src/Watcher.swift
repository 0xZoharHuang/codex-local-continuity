import AppKit
import Foundation

enum SyncTrigger: Equatable {
    case startupOffline
    case terminated(pid_t)
    case capture
}

// Called on the main queue. A later exit supersedes pending work from an
// earlier exit, including one that happened while the worker was running.
struct SyncQueue {
    private var pendingSync: SyncTrigger?
    private var pendingCapture = false
    private(set) var busy = false

    mutating func request(_ trigger: SyncTrigger) {
        if trigger == .capture {
            pendingCapture = true
        } else {
            pendingSync = trigger
            pendingCapture = false
        }
    }

    mutating func applicationLaunched() {
        pendingSync = nil
    }

    mutating func take() -> SyncTrigger? {
        guard !busy else { return nil }
        let trigger = pendingSync ?? (pendingCapture ? .capture : nil)
        guard let trigger else { return nil }
        pendingSync = nil
        pendingCapture = false
        busy = true
        return trigger
    }

    mutating func finished() {
        busy = false
    }
}

struct FileStamp: Equatable {
    let device: dev_t
    let inode: ino_t
    let size: off_t
    let modifiedSeconds: Int
    let modifiedNanos: Int
    let changedSeconds: Int
    let changedNanos: Int

    init?(_ path: String) {
        var info = stat()
        guard lstat(path, &info) == 0, info.st_uid == getuid(),
              info.st_mode & S_IFMT == S_IFREG || info.st_mode & S_IFMT == S_IFDIR else { return nil }
        device = info.st_dev
        inode = info.st_ino
        size = info.st_size
        modifiedSeconds = info.st_mtimespec.tv_sec
        modifiedNanos = info.st_mtimespec.tv_nsec
        changedSeconds = info.st_ctimespec.tv_sec
        changedNanos = info.st_ctimespec.tv_nsec
    }

    func sameFile(as other: FileStamp?) -> Bool {
        other?.device == device && other?.inode == inode
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
    private let stateURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".codex/.codex-global-state.json")
    private let workerURL: URL
    private var observers: [NSObjectProtocol] = []
    private var runningAppsObserver: NSKeyValueObservation?
    private var exitSources: [pid_t: DispatchSourceProcess] = [:]
    private var lastCompletedPID: pid_t?
    private var queue = SyncQueue()
    private var worker: Process?
    private var stateSource: DispatchSourceFileSystemObject?
    private var directorySource: DispatchSourceFileSystemObject?
    private var watchedState: FileStamp?
    private var watchedDirectory: FileStamp?
    private var lastState: FileStamp?
    private var captureDeadline: TimeInterval?
    private var captureTimer: DispatchWorkItem?
    private var captureWatchdog: DispatchWorkItem?

    init(workerURL: URL) {
        self.workerURL = workerURL
    }

    func start() {
        refreshStateWatches()
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

    private func fileSource(_ url: URL, events: DispatchSource.FileSystemEvent) -> DispatchSourceFileSystemObject? {
        let fd = open(url.path, O_EVTONLY | O_CLOEXEC | O_NOFOLLOW)
        guard fd >= 0 else { return nil }
        let source = DispatchSource.makeFileSystemObjectSource(fileDescriptor: fd, eventMask: events, queue: .main)
        source.setEventHandler { [weak self] in self?.refreshStateWatches() }
        source.setCancelHandler { close(fd) }
        source.resume()
        return source
    }

    private func refreshStateWatches() {
        let parentURL = stateURL.deletingLastPathComponent()
        // Keep a parent watch when .codex is absent or replaced, then attach
        // to the exact directory and file as soon as they appear.
        let directory = FileStamp(parentURL.path) == nil ? parentURL.deletingLastPathComponent() : parentURL
        let parent = FileStamp(directory.path)
        if parent?.sameFile(as: watchedDirectory) != true {
            directorySource?.cancel()
            directorySource = nil
            watchedDirectory = nil
            if let parent {
                directorySource = fileSource(directory, events: [.write, .rename, .delete])
                if directorySource != nil { watchedDirectory = parent }
            }
        }
        let current = FileStamp(stateURL.path)
        if current?.sameFile(as: watchedState) != true {
            stateSource?.cancel()
            stateSource = nil
            watchedState = nil
            if let current {
                stateSource = fileSource(stateURL, events: [.write, .rename, .delete, .attrib])
                if stateSource != nil { watchedState = current }
            }
        }
        guard current != lastState else { return }
        lastState = current
        if current != nil && !exitSources.isEmpty {
            scheduleCapture()
        }
    }

    private func scheduleCapture() {
        let now = ProcessInfo.processInfo.systemUptime
        if captureDeadline == nil { captureDeadline = now + 1 }
        captureTimer?.cancel()
        let delay = min(0.5, max(0, captureDeadline! - now))
        let timer = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.captureTimer = nil
            self.captureDeadline = nil
            guard !self.exitSources.isEmpty else { return }
            self.queue.request(.capture)
            self.drain()
        }
        captureTimer = timer
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: timer)
    }

    private func cancelCapture() {
        captureTimer?.cancel()
        captureTimer = nil
        captureDeadline = nil
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
        refreshStateWatches()
        if lastState != nil {
            cancelCapture()
            queue.request(.capture)
            drain()
        }
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
        cancelCapture()
        queue.request(.terminated(pid))
        drain()
    }

    private func drain() {
        guard let trigger = queue.take() else { return }
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        task.arguments = ["-I", "-B", workerURL.path, trigger == .capture ? "capture" : "sync"]
        var environment = ProcessInfo.processInfo.environment
        environment.removeValue(forKey: "CODEX_CONTINUITY_EXIT_PID")
        switch trigger {
        case .startupOffline:
            environment["CODEX_CONTINUITY_TRIGGER"] = "startup-offline"
        case .terminated(let pid):
            environment["CODEX_CONTINUITY_TRIGGER"] = "termination"
            environment["CODEX_CONTINUITY_EXIT_PID"] = String(pid)
        case .capture:
            environment["CODEX_CONTINUITY_TRIGGER"] = "native-state-changed"
        }
        task.environment = environment
        task.standardInput = FileHandle.nullDevice
        task.terminationHandler = { [weak self] finished in
            DispatchQueue.main.async {
                guard let self, self.worker === finished else { return }
                self.captureWatchdog?.cancel()
                self.captureWatchdog = nil
                self.worker = nil
                self.queue.finished()
                self.drain()
            }
        }
        worker = task
        do {
            try task.run()
            if trigger == .capture {
                // Capture writes only the private ledger using atomic files.
                // A stuck capture must not indefinitely block a later exit sync.
                // Sync itself can commit native state and has no watchdog.
                let timeout = DispatchWorkItem { [weak self, weak task] in
                    guard let self, let task, self.worker === task, task.isRunning else { return }
                    FileHandle.standardError.write(Data("Capture exceeded its 30-second deadline.\n".utf8))
                    task.terminate()
                }
                captureWatchdog = timeout
                DispatchQueue.main.asyncAfter(deadline: .now() + 30, execute: timeout)
            }
        } catch {
            captureWatchdog?.cancel()
            captureWatchdog = nil
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
