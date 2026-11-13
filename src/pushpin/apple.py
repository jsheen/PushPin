"""Apple pieces for the iOS app: Sign in with Apple, app sessions, and push through APNs.

Sign in with Apple: the app gets an identity token (a JWT Apple signs) and posts it to the card
server's /api/auth/apple. `verify_identity_token` checks it against Apple's public keys, the
issuer and the app's bundle id, and returns the stable user id Apple gives this app (`sub`).

Sessions: the server answers with its own token, `<reader_id>.<HMAC>` keyed by
PUSHPIN_SESSION_SECRET. The app keeps it in the Keychain and sends it as `Authorization: Bearer`.
Deleting the account deletes the reader, which invalidates every token for it.

APNs: `Apns` sends alerts over HTTP/2 with a provider token (an ES256 JWT signed with the .p8
key from the Apple Developer portal, reused for 50 minutes; Apple accepts it for an hour).
Configured from the environment:
  APNS_KEY (the .p8 contents) or APNS_KEY_FILE, APNS_KEY_ID, APNS_TEAM_ID,
  PUSHPIN_APPLE_BUNDLE_ID (the topic). Each device says whether it's a development build (Xcode,
  APNs sandbox) or TestFlight/App Store (production) when it registers.
"""

import hashlib
import hmac
import os
import time
from pathlib import Path
from typing import ClassVar

import httpx
import jwt

APPLE_ISSUER = "https://appleid.apple.com"
APPLE_KEYS_URL = "https://appleid.apple.com/auth/keys"
_jwks: jwt.PyJWKClient | None = None


def verify_identity_token(token: str, bundle_id: str, jwks: jwt.PyJWKClient | None = None) -> str:
    """Apple's user id (`sub`) for a valid identity token; raises jwt.InvalidTokenError otherwise."""
    global _jwks
    if jwks is None:
        _jwks = _jwks or jwt.PyJWKClient(APPLE_KEYS_URL, cache_keys=True)
        jwks = _jwks
    key = jwks.get_signing_key_from_jwt(token).key
    claims = jwt.decode(token, key, algorithms=["RS256"], audience=bundle_id, issuer=APPLE_ISSUER,
                        options={"require": ["sub", "exp", "iat"]})
    return claims["sub"]


def _mac(reader_id: str, secret: str) -> str:
    return hmac.new(secret.encode(), reader_id.encode(), hashlib.sha256).hexdigest()


def make_session(reader_id: str, secret: str) -> str:
    return f"{reader_id}.{_mac(reader_id, secret)}"


def read_session(token: str | None, secret: str) -> str | None:
    """The reader id a session token was made for, or None if it's missing or forged."""
    if not token or "." not in token:
        return None
    reader_id, _, mac = token.rpartition(".")
    return reader_id if hmac.compare_digest(mac, _mac(reader_id, secret)) else None


class Apns:
    """Sends alerts to device tokens. `send` returns Apple's HTTP status (200 = accepted;
    410, or 400 with BadDeviceToken, means the token is dead and should be dropped)."""

    HOSTS: ClassVar[dict[bool, str]] = {False: "https://api.push.apple.com", True: "https://api.sandbox.push.apple.com"}

    def __init__(self, key_pem: str, key_id: str, team_id: str, topic: str, client: httpx.Client | None = None):
        self.key, self.key_id, self.team_id, self.topic = key_pem, key_id, team_id, topic
        self.client = client or httpx.Client(http2=True, timeout=10)
        self._token, self._token_at = "", 0.0

    @classmethod
    def from_env(cls) -> "Apns | None":
        key = os.environ.get("APNS_KEY") or (Path(os.environ["APNS_KEY_FILE"]).read_text()
                                             if os.environ.get("APNS_KEY_FILE") else None)
        key_id, team_id = os.environ.get("APNS_KEY_ID"), os.environ.get("APNS_TEAM_ID")
        topic = os.environ.get("PUSHPIN_APPLE_BUNDLE_ID")
        if not (key and key_id and team_id and topic):
            return None
        return cls(key, key_id, team_id, topic)

    def provider_token(self) -> str:
        if time.time() - self._token_at > 50 * 60:
            self._token_at = time.time()
            self._token = jwt.encode({"iss": self.team_id, "iat": int(self._token_at)}, self.key,
                                     algorithm="ES256", headers={"kid": self.key_id})
        return self._token

    def send(self, device_token: str, title: str, body: str, data: dict | None = None,
             sandbox: bool = False) -> tuple[int, str]:
        """(status, reason). `data` rides along in the payload for the app (push_id, news_id, url).
        `mutable-content` lets the app's notification extension add the pin before it's shown;
        the category gives it the long-press view and the Pin it button."""
        payload = {"aps": {"alert": {"title": title, "body": body}, "sound": "default",
                           "mutable-content": 1, "category": "ARTICLE"}, **(data or {})}
        resp = self.client.post(f"{self.HOSTS[bool(sandbox)]}/3/device/{device_token}", json=payload, headers={
            "authorization": f"bearer {self.provider_token()}", "apns-topic": self.topic,
            "apns-push-type": "alert", "apns-priority": "10"})
        reason = resp.json().get("reason", "") if resp.content else ""
        return resp.status_code, reason

    @staticmethod
    def dead(status: int, reason: str) -> bool:
        return status == 410 or reason in ("BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic")
