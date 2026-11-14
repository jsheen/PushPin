import SwiftUI
import UIKit

/// What a tapped push opens: the story, with a pin dropping onto it. Read opens NYT.
struct PinnedView: View {
    let push: Push
    @EnvironmentObject private var state: AppState
    @Environment(\.openURL) private var openURL
    @Environment(\.dismiss) private var dismiss
    @State private var dropped = false

    var body: some View {
        VStack(spacing: 28) {
            HStack {
                Spacer()
                Button("Done") { dismiss() }
            }
            Spacer()
            ZStack(alignment: .topTrailing) {
                VStack(alignment: .leading, spacing: 10) {
                    if let section = push.section {
                        Text(section.uppercased()).font(.caption).foregroundStyle(.secondary)
                    }
                    Text(push.title ?? "")
                        .font(.custom("Georgia-Bold", size: 26))
                    if let abstract = push.abstract {
                        Text(abstract).foregroundStyle(.secondary)
                    }
                }
                .padding(22)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 14).fill(Color(.secondarySystemBackground)))
                .scaleEffect(dropped ? 1 : 1.015)   // the card gives a little when the pin lands

                PinShape(dropped: dropped)
                    .offset(x: -18, y: -26)
            }
            Spacer()
            Button {
                Task { await state.markRead(pushId: push.pushId) }
                if let url = push.url.flatMap(URL.init(string:)) { openURL(url) }
                dismiss()
            } label: {
                Text("Read").frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
        }
        .padding(24)
        .onAppear {
            withAnimation(.interpolatingSpring(stiffness: 260, damping: 14).delay(0.15)) { dropped = true }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) {
                UIImpactFeedbackGenerator(style: .heavy).impactOccurred()
            }
        }
    }
}

/// A push pin that falls from above and sticks in at an angle, with its shadow tightening as it lands.
struct PinShape: View {
    let dropped: Bool

    var body: some View {
        Image(systemName: "pin.fill")
            .font(.system(size: 44))
            .foregroundStyle(Color.pinRed)
            .rotationEffect(.degrees(dropped ? 28 : -10))
            .offset(y: dropped ? 0 : -420)
            .shadow(color: .black.opacity(dropped ? 0.35 : 0.05), radius: dropped ? 2 : 16, x: 3, y: dropped ? 3 : 20)
    }
}
