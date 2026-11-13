import AuthenticationServices
import SwiftUI

struct RootView: View {
    @EnvironmentObject private var state: AppState

    var body: some View {
        Group {
            if state.session == nil {
                SignInView()
            } else {
                ProfileView()
            }
        }
        .fullScreenCover(item: $state.justPinned) { push in
            PinnedView(push: push)
        }
        .alert("PushPin", isPresented: Binding(get: { state.error != nil }, set: { if !$0 { state.error = nil } })) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(state.error ?? "")
        }
    }
}

struct SignInView: View {
    @EnvironmentObject private var state: AppState
    @State private var showServer = false

    var body: some View {
        VStack(spacing: 24) {
            Spacer()
            Image(systemName: "pin.fill")
                .font(.system(size: 64))
                .foregroundStyle(Color.pinRed)
                .rotationEffect(.degrees(30))
            Text("PushPin")
                .font(.custom("Georgia-Bold", size: 40))
            Text("A few stories a day, picked for you.\nTap one to pin it.")
                .multilineTextAlignment(.center)
                .foregroundStyle(.secondary)
            Spacer()
            SignInWithAppleButton(.signIn) { request in
                request.requestedScopes = [.fullName]
            } onCompletion: { result in
                switch result {
                case let .success(auth):
                    guard let credential = auth.credential as? ASAuthorizationAppleIDCredential,
                          let data = credential.identityToken, let token = String(data: data, encoding: .utf8) else {
                        state.error = "Apple didn't return a sign-in token."
                        return
                    }
                    Task { await state.signIn(identityToken: token, name: credential.fullName?.givenName) }
                case let .failure(error):
                    if (error as? ASAuthorizationError)?.code != .canceled { state.error = error.localizedDescription }
                }
            }
            .signInWithAppleButtonStyle(.black)
            .frame(height: 50)
            DisclosureGroup("Server", isExpanded: $showServer) {
                TextField("https://…", text: $state.server)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
                    .font(.footnote.monospaced())
            }
            .font(.footnote)
            .foregroundStyle(.secondary)
        }
        .padding(24)
    }
}
