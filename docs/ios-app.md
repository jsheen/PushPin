# The iOS app: backend side

The app is a client of what already runs: the scorer and notifier choose and send pushes, and
the card server stores what readers do. Three things are added for it:

| Piece | Where | What it does |
|-------|-------|--------------|
| Sign in with Apple | `POST /api/auth/apple` (`src/pushpin/cards.py`, `src/pushpin/apple.py`) | Checks Apple's identity token (signature, issuer, bundle id), makes the reader on first sign-in (`readers.apple_sub`), answers with a session token for the Keychain. |
| JSON API | `/api/...` on the card server | The same actions as the web cards: open (pin), read, Crème, delete, volume, public, plus devices and account deletion. Listed in `serve_cards`'s docstring. |
| APNs | `Apns` in `src/pushpin/apple.py`, called by the notifier | Each sent push also goes to every registered device (`devices` table), with `push_id`, `news_id` and `url` in the payload. Dead tokens (410) are dropped. ntfy keeps working alongside. |

Account deletion (`DELETE /api/account`, an App Store rule) removes the reader, their devices,
pins, reads and Crème. Pushes stay: they're sent to everyone, not to one reader.

**Every app reader gets their own pushes.** At each slot the notifier makes one batched scorer
call with a request per audience:
- each app reader with a device: their own scorer user (`readers.scorer_user`, a cold start the
  first time), their own past pushes and taps as feedback events, their own daily cap from their
  own Light/Medium/Heavy choice, and their own "already pushed" list. Their push goes to their
  devices only, and only they can open it.
- the ntfy topic, as before: one shared pick for everyone subscribed (`scores.for_reader` is NULL).

`scores.for_reader` records which audience each row was scored for. In the `labeled` view, an app
reader is labeled on their own pushes only, never on the broadcast.

## Settings

In `.env` (local) or Secret Manager (cloud):

```
PUSHPIN_APPLE_BUNDLE_ID=com.<you>.pushpin     # the app's bundle id; also the APNs topic
PUSHPIN_SESSION_SECRET=<openssl rand -hex 32> # signs session tokens; changing it signs everyone out
APNS_KEY_ID=<10 characters>                   # from the key you create below
APNS_TEAM_ID=<10 characters>                  # Membership details in the developer portal
APNS_KEY_FILE=secrets/AuthKey_<KEY_ID>.p8     # the key file (secrets/ is gitignored, mounted in Docker)
```

The API answers 404 until the first two are set; the notifier skips APNs until all of the
APNs ones are.

## Apple Developer portal, once

1. Join the Apple Developer Program ($99/year).
2. **Identifiers → App IDs → +**: bundle id `com.<you>.pushpin`; turn on **Push Notifications**
   and **Sign in with Apple**.
3. **Keys → +**: turn on **Apple Push Notifications service (APNs)**. Download the `.p8` (only
   once) into `secrets/`, and note its Key ID.
4. In Xcode, the app target's **Signing & Capabilities**: your team, the same bundle id, and the
   **Push Notifications** and **Sign in with Apple** capabilities.

Development builds run from Xcode register as `"sandbox": true` and go through APNs' sandbox;
TestFlight and App Store builds use production. The app says which when it registers its token.

## The app (`ios/`)

SwiftUI, iOS 17+. The Xcode project is generated from `ios/project.yml`
(`brew install xcodegen; cd ios && xcodegen`), so only the spec and the sources are in git.

| File | What it is |
|------|------------|
| `PushPin/RootView.swift` | Sign in with Apple (and the server address, while it's still the tunnel) or the profile |
| `PushPin/ProfileView.swift` | The Crème, Pinned (Read, swipe to delete; no star until read), Read with the daily streak, pushes a day, public link, sign out, delete account |
| `PushPin/PinnedView.swift` | What a tapped push opens: the story with a pin dropping onto it, a haptic thud, Read (Put in Crème once it's read) |
| `PushPin/AppState.swift`, `API.swift`, `Keychain.swift` | Server calls, the session in the Keychain, device-token registration |
| `PushPin/PushPinApp.swift` | Push plumbing: the ARTICLE category with a **Pin it** button, taps, the device token |
| `PinNotification/` | Notification content extension: the long-press view of a push, with the pin dropping in |

To run it: set `DEVELOPMENT_TEAM`, `PUSHPIN_BUNDLE_ID` and `PUSHPIN_API_BASE` in
`ios/project.yml`, run `xcodegen`, open `ios/PushPin.xcodeproj`, and run on a phone. The simulator
can sign in with Apple and show the screens, but only a real phone gets pushes.
