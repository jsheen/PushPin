import Foundation

/// An article in a profile list. Which fields are set depends on the list: Pinned has pushId,
/// Read has readAt.
struct Article: Codable, Identifiable, Hashable {
    let newsId: String
    let title: String?
    let url: String?
    var pushId: String?
    var section: String?
    var readAt: String?
    var pinnedAt: String?
    var id: String { newsId }
}

/// A sent push, as returned when it's opened (pinned).
struct Push: Codable, Identifiable, Hashable {
    let pushId: String
    let newsId: String
    let title: String?
    let section: String?
    let url: String?
    let abstract: String?
    let read: Bool
    var id: String { pushId }
}

struct Profile: Codable {
    struct Reader: Codable {
        let name: String?
        let volume: String?
    }
    let reader: Reader
    let caps: [String: Int]
    let pinned: [Article]
    let read: [Article]
    let streak: Int
}

struct SignInReply: Codable {
    let session: String
    let new: Bool
    let profile: Profile
}

struct OpenReply: Codable {
    let push: Push
    let profile: Profile
}

struct OkReply: Codable {
    let ok: Bool
}
