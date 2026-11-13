import Foundation

enum APIError: LocalizedError {
    case signedOut
    case server(Int, String)

    var errorDescription: String? {
        switch self {
        case .signedOut: return "Please sign in again."
        case let .server(code, message): return "Server error \(code): \(message)"
        }
    }
}

/// The card server's JSON API (/api/..., see serve_cards in src/pushpin/cards.py).
struct APIClient {
    let base: URL
    let session: String?

    private static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    func call<T: Decodable>(_ method: String, _ path: String, _ body: [String: Any]? = nil) async throws -> T {
        var request = URLRequest(url: base.appendingPathComponent("api").appendingPathComponent(path))
        request.httpMethod = method
        request.timeoutInterval = 30
        request.setValue("application/json", forHTTPHeaderField: "content-type")
        if let session { request.setValue("Bearer \(session)", forHTTPHeaderField: "authorization") }
        if let body { request.httpBody = try JSONSerialization.data(withJSONObject: body) }
        let (data, response) = try await URLSession.shared.data(for: request)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        if status == 401 { throw APIError.signedOut }
        guard status == 200 else {
            let message = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["error"] as? String
            throw APIError.server(status, message ?? "")
        }
        return try Self.decoder.decode(T.self, from: data)
    }
}
