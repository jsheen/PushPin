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

Pushes are still chosen once per slot for one cold-start user and sent to every reader, as with
the single ntfy topic. Scoring each reader separately is the next step once there are several.

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
