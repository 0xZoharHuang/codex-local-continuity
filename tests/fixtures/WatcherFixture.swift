import AppKit
import Foundation

// A temporary accessory app with no window. Only this test process is asked to quit.
@MainActor final class FixtureDelegate: NSObject, NSApplicationDelegate {
    let root = Bundle.main.bundleURL.deletingLastPathComponent()
    let pid = ProcessInfo.processInfo.processIdentifier
    let deadline = Date().addingTimeInterval(25)

    func mark(_ name: String) {
        let file = root.appendingPathComponent("\(name)-\(pid)")
        try! String(Date().timeIntervalSince1970).write(to: file, atomically: true, encoding: .utf8)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        mark("launched")
        waitForTestToRequestQuit()
    }

    func waitForTestToRequestQuit() {
        let quit = root.appendingPathComponent("quit-\(pid)")
        if FileManager.default.fileExists(atPath: quit.path) || Date() >= deadline {
            NSApp.terminate(nil)
        } else {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) {
                self.waitForTestToRequestQuit()
            }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        mark("quitting")
    }
}

let app = NSApplication.shared
let delegate = FixtureDelegate()
app.delegate = delegate
app.run()
