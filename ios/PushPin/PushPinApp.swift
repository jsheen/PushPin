import SwiftUI
import UIKit
import UserNotifications

@main
struct PushPinApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var state = AppState.shared

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(state)
                .tint(.pinRed)
        }
    }
}

/// Push plumbing: the device token, the ARTICLE category (long-press view + Pin it), and taps.
final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    static let pinAction = "PIN"

    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        let pin = UNNotificationAction(identifier: Self.pinAction, title: "Pin it", options: [],
                                       icon: UNNotificationActionIcon(systemImageName: "pin.fill"))
        center.setNotificationCategories([UNNotificationCategory(identifier: "ARTICLE", actions: [pin],
                                                                 intentIdentifiers: [])])
        if AppState.shared.session != nil { application.registerForRemoteNotifications() }
        return true
    }

    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        Task { await AppState.shared.didRegister(deviceToken: deviceToken) }
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        print("push registration failed: \(error)")  // the simulator can't get a real token
    }

    // a push arriving while the app is open still shows as a banner
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification)
        async -> UNNotificationPresentationOptions {
        [.banner, .sound]
    }

    // tapped (opens the app on the pin drop) or Pin it from the long-press view (pins in the background)
    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse) async {
        guard let pushId = response.notification.request.content.userInfo["push_id"] as? String else { return }
        let quietly = response.actionIdentifier == Self.pinAction
        await AppState.shared.open(pushId: pushId, quietly: quietly)
    }
}

extension Color {
    static let pinRed = Color(red: 0.82, green: 0.01, blue: 0.11)
}
