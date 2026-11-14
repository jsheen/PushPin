import SwiftUI

/// The reader's profile, like /me on the web: the Crème, what they pinned, what they read,
/// how many pushes a day, the public link, and the account.
struct ProfileView: View {
    @EnvironmentObject private var state: AppState
    @Environment(\.openURL) private var openURL
    @State private var confirmDelete = false

    private let volumes = ["light", "medium", "heavy"]

    var body: some View {
        NavigationStack {
            List {
                if let p = state.profile {
                    if p.reader.volume == nil {
                        Section {
                            Text("How many stories a day? Pick below; you can change it anytime.")
                                .foregroundStyle(.secondary)
                        }
                    }
                    Section {
                        if p.creme.isEmpty {
                            Text("Star an article to make it your Crème.").foregroundStyle(.secondary)
                        }
                        ForEach(p.creme) { a in
                            ArticleRow(article: a, starred: true)
                                .swipeActions { Button("Unstar") { Task { await state.setCreme(a.newsId, on: false) } } }
                        }
                    } header: { Label("Crème", systemImage: "star.fill") }

                    Section {
                        if p.pinned.isEmpty {
                            Text("Tap a push to pin it here.").foregroundStyle(.secondary)
                        }
                        ForEach(p.pinned) { a in
                            ArticleRow(article: a) { read(a) }  // no star: the Crème is for read articles
                            .swipeActions { Button("Delete", role: .destructive) { Task { await state.remove(a.newsId) } } }
                        }
                    } header: { Label("Pinned", systemImage: "pin.fill") } footer: {
                        if !p.pinned.isEmpty { Text("Read one to move it to Read, where you can put it in your Crème.") }
                    }

                    Section {
                        ForEach(p.read) { a in
                            ArticleRow(article: a, starred: p.creme.contains { $0.newsId == a.newsId }) {
                                if let url = a.url.flatMap(URL.init(string:)) { openURL(url) }
                            } star: {
                                Task { await state.setCreme(a.newsId) }
                            }
                            .swipeActions { Button("Delete", role: .destructive) { Task { await state.remove(a.newsId) } } }
                        }
                    } header: {
                        Text("Read \(p.read.count)") + Text(p.streak > 0 ? "  ·  Daily streak \(p.streak)" : "")
                    }

                    Section("Pushes a day") {
                        Picker("Pushes a day", selection: Binding(
                            get: { p.reader.volume ?? "" },
                            set: { v in Task { await state.setVolume(v) } })) {
                            ForEach(volumes, id: \.self) { v in
                                Text("\(v.capitalized) · \(p.caps[v] ?? 0)").tag(v)
                            }
                        }
                        .pickerStyle(.segmented)
                    }

                    Section {
                        Toggle("Show what I read on my link", isOn: Binding(
                            get: { p.reader.public }, set: { on in Task { await state.setPublic(on) } }))
                        if let url = URL(string: p.shareUrl) {
                            ShareLink("Share my profile", item: url)
                        }
                    }

                    Section {
                        Button("Sign out") { Task { await state.signOut() } }
                        Button("Delete account", role: .destructive) { confirmDelete = true }
                    }
                } else {
                    ProgressView()
                }
            }
            .navigationTitle(state.profile?.reader.name.map { "\($0)'s reading" } ?? "Your reading")
            .refreshable { await state.refresh() }
            .task { await state.refresh() }
            .confirmationDialog("Delete your account and everything you pinned and read?",
                                isPresented: $confirmDelete, titleVisibility: .visible) {
                Button("Delete account", role: .destructive) { Task { await state.deleteAccount() } }
            }
        }
    }

    private func read(_ a: Article) {
        if let pushId = a.pushId { Task { await state.markRead(pushId: pushId) } }
        if let url = a.url.flatMap(URL.init(string:)) { openURL(url) }  // the NYT app if installed
    }
}

struct ArticleRow: View {
    let article: Article
    var starred = false
    var onRead: (() -> Void)?
    var star: (() -> Void)?

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                if let section = article.section {
                    Text(section.uppercased()).font(.caption2).foregroundStyle(.secondary)
                }
                Text(article.title ?? "Untitled").font(.custom("Georgia", size: 17))
            }
            Spacer()
            if let star {
                Button(action: star) {
                    Image(systemName: starred ? "star.fill" : "star")
                }
                .buttonStyle(.borderless)
            }
            if let onRead {
                Button("Read", action: onRead).buttonStyle(.borderedProminent).controlSize(.small)
            }
        }
    }
}
