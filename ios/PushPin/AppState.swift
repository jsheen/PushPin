import Foundation
import UIKit
import UserNotifications

/// Everything the screens show, and every call to the server.
@MainActor
final class AppState: ObservableObject {
    static let shared = AppState()

    @Published var session: String? = Keychain.load()
    @Published var profile: Profile?
    @Published var justPinned: Push?   // a push that was just tapped: shown with the pin drop
    @Published var error: String?
    @Published var server: String {
        didSet { UserDefaults.standard.set(server, forKey: "server") }
    }
    private var deviceToken: String?

    init() {
        server = UserDefaults.standard.string(forKey: "server")
            ?? (Bundle.main.object(forInfoDictionaryKey: "PushPinAPIBase") as? String ?? "")
    }

    private var api: APIClient? {
        guard let url = URL(string: server.trimmingCharacters(in: .whitespaces)), url.scheme != nil else {
            error = "Set the server address first."
            return nil
        }
        return APIClient(base: url, session: session)
    }

    /// Runs a call, keeps the profile it returns, and turns failures into a message.
    private func run<T: Decodable>(_ method: String, _ path: String, _ body: [String: Any]? = nil) async -> T? {
        guard let api else { return nil }
        do {
            let reply: T = try await api.call(method, path, body)
            if let profile = reply as? Profile { self.profile = profile }
            return reply
        } catch APIError.signedOut where session != nil {
            signOutLocally()
            error = APIError.signedOut.errorDescription
        } catch {
            self.error = error.localizedDescription
        }
        return nil
    }

    // MARK: account

    func signIn(identityToken: String, name: String?) async {
        var body: [String: Any] = ["identity_token": identityToken]
        if let name, !name.isEmpty { body["name"] = name }
        guard let reply: SignInReply = await run("POST", "auth/apple", body) else { return }
        Keychain.save(reply.session)
        session = reply.session
        profile = reply.profile
        await askForPushes()
    }

    func signOut() async {
        if let deviceToken { let _: Profile? = await run("DELETE", "devices/\(deviceToken)") }
        signOutLocally()
    }

    func deleteAccount() async {
        guard let reply: OkReply = await run("DELETE", "account"), reply.ok else { return }
        signOutLocally()
    }

    private func signOutLocally() {
        Keychain.delete()
        session = nil
        profile = nil
        justPinned = nil
    }

    // MARK: pushes

    func askForPushes() async {
        let granted = (try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge])) ?? false
        if granted { UIApplication.shared.registerForRemoteNotifications() }
    }

    func didRegister(deviceToken data: Data) async {
        deviceToken = data.map { String(format: "%02x", $0) }.joined()
        await registerDevice()
    }

    func registerDevice() async {
        guard session != nil, let deviceToken else { return }
        #if DEBUG
        let sandbox = true   // run from Xcode: APNs sandbox
        #else
        let sandbox = false  // TestFlight / App Store
        #endif
        let _: Profile? = await run("POST", "devices", ["token": deviceToken, "sandbox": sandbox])
    }

    /// A push was tapped (or Pin it was pressed): pin it, and show it unless `quietly`.
    func open(pushId: String, quietly: Bool = false) async {
        guard session != nil, let reply: OpenReply = await run("POST", "open/\(pushId)") else { return }
        profile = reply.profile
        if !quietly { justPinned = reply.push }
    }

    // MARK: profile

    func refresh() async {
        guard session != nil else { return }
        let _: Profile? = await run("GET", "me")
    }

    /// Records the Read tap; the caller opens the NYT link.
    func markRead(pushId: String) async {
        let _: Profile? = await run("POST", "read/\(pushId)")
    }

    func remove(_ newsId: String) async {
        let _: Profile? = await run("DELETE", "articles/\(newsId)")
    }

    func setVolume(_ volume: String) async {
        let _: Profile? = await run("POST", "volume", ["volume": volume])
    }
}
