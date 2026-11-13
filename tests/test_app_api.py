"""The iOS app's side: Sign in with Apple, sessions, the JSON API, APNs sending, account deletion."""
import json
import time
import urllib.error
import urllib.request

import httpx
import jwt
import pandas as pd
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from pushpin import apple, cards
from tests.test_notifier import setup  # noqa: F401 -- the notifier + store fixture (SQLite, Postgres)

BUNDLE = "com.example.pushpin"


@pytest.fixture
def api(setup, monkeypatch):  # noqa: F811
    """A card server with the API on; Apple sign-in faked: identity token "apple:<sub>" -> sub."""
    n, store, sent, _db = setup
    monkeypatch.setenv("PUSHPIN_SESSION_SECRET", "s3cret")
    monkeypatch.setenv("PUSHPIN_APPLE_BUNDLE_ID", BUNDLE)

    def verify(token):
        if not token.startswith("apple:"):
            raise jwt.InvalidTokenError("not from Apple")
        return token.removeprefix("apple:")

    server = cards.serve_cards(store, 0, verify_apple=verify, caps={"light": 1, "medium": 2, "heavy": 4})
    base = f"http://127.0.0.1:{server.server_address[1]}/api"

    def call(method, path, body=None, session=None):
        req = urllib.request.Request(base + path, method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"content-type": "application/json",
                                              **({"authorization": f"Bearer {session}"} if session else {})})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    yield n, store, sent, call
    server.shutdown()


def test_sign_in_creates_a_reader_once(api):
    _n, store, _sent, call = api
    status, first = call("POST", "/auth/apple", {"identity_token": "apple:001", "name": "Jo"})
    assert status == 200 and first["new"] and first["profile"]["reader"]["name"] == "Jo"
    status, again = call("POST", "/auth/apple", {"identity_token": "apple:001"})
    assert status == 200 and not again["new"] and store.reader_count() == 1
    assert call("POST", "/auth/apple", {"identity_token": "forged"})[0] == 401
    assert call("GET", "/me")[0] == 401
    forged = again["session"].rsplit(".", 1)[0] + ".00"
    assert call("GET", "/me", session=forged)[0] == 401
    status, me = call("GET", "/me", session=again["session"])
    assert status == 200 and me["streak"] == 0 and me["share_url"].endswith(store.slug(me_id(store)))


def me_id(store):
    return store._rows("SELECT reader_id FROM readers")[0]["reader_id"]


def test_api_is_off_without_its_settings(setup, monkeypatch):  # noqa: F811
    _n, store, _sent, _db = setup
    monkeypatch.delenv("PUSHPIN_SESSION_SECRET", raising=False)
    server = cards.serve_cards(store, 0)
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/api/me")
        assert e.value.code == 404
    finally:
        server.shutdown()


def test_app_pins_reads_and_deletes_its_account(api):
    n, store, sent, call = api
    session = call("POST", "/auth/apple", {"identity_token": "apple:002"})[1]["session"]
    assert call("POST", "/devices", {"token": "AB" * 32, "sandbox": True}, session)[0] == 200
    assert call("POST", "/devices", {"token": "not hex!"}, session)[0] == 404
    assert store.devices() == [{"token": "ab" * 32, "reader_id": me_id(store), "sandbox": 1}]

    n.run_slot(pd.Timestamp("2026-11-12 12:00"))
    push_id = sent[0][4].rsplit("/", 1)[1]
    status, opened = call("POST", f"/open/{push_id}", session=session)
    assert status == 200 and opened["push"]["push_id"] == push_id and opened["push"]["read"] is False
    assert [a["push_id"] for a in opened["profile"]["pinned"]] == [push_id]
    news_id = opened["push"]["news_id"]
    assert call("POST", f"/favorite/{news_id}", {"on": True}, session)[1]["creme"][0]["news_id"] == news_id
    profile = call("POST", f"/read/{push_id}", session=session)[1]
    assert profile["pinned"] == [] and [a["news_id"] for a in profile["read"]] == [news_id] and profile["streak"] == 1
    assert call("POST", "/volume", {"volume": "heavy"}, session)[1]["reader"]["volume"] == "heavy"
    assert call("POST", "/volume", {"volume": "max"}, session)[0] == 404
    assert call("POST", "/public", {"public": True}, session)[1]["reader"]["public"] is True
    assert call("POST", "/open/nope", session=session)[0] == 404

    assert call("DELETE", "/account", session=session) == (200, {"ok": True})
    assert store.reader_count() == 0 and store.devices() == []
    assert store._rows("SELECT COUNT(*) AS n FROM interactions")[0]["n"] == 0
    assert call("GET", "/me", session=session)[0] == 401  # the old session is dead too
    status, back = call("POST", "/auth/apple", {"identity_token": "apple:002"})
    assert back["new"] and back["profile"]["read"] == []  # signing in again starts fresh


class FakeApns:
    def __init__(self, replies):
        self.replies, self.sent = replies, []

    def send(self, token, title, body, data=None, sandbox=False):
        self.sent.append((token, title, data, sandbox))
        return self.replies.get(token, (200, ""))

    dead = staticmethod(apple.Apns.dead)


def test_notifier_pushes_to_app_devices_and_drops_dead_ones(setup):  # noqa: F811
    n, store, sent, _db = setup
    reader = store.new_reader(0)
    store.add_device(reader, "aa" * 32, sandbox=True)
    store.add_device(reader, "bb" * 32)
    n.apns = FakeApns({"bb" * 32: (410, "Unregistered")})
    assert n.run_slot(pd.Timestamp("2026-11-12 12:00"))["sent"]
    assert len(sent) == 1  # ntfy still gets it
    (tok_a, title, data, sandbox_a), (tok_b, *_rest, sandbox_b) = n.apns.sent
    push_id = sent[0][4].rsplit("/", 1)[1]
    assert data["push_id"] == push_id and data["url"].startswith("http") and title == sent[0][2]
    assert (tok_a, sandbox_a, tok_b, sandbox_b) == ("aa" * 32, True, "bb" * 32, False)
    assert [d["token"] for d in store.devices()] == ["aa" * 32]


def test_verify_identity_token_checks_signature_audience_and_issuer():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class Keys:
        def get_signing_key_from_jwt(self, token):
            return jwt.PyJWK.from_dict({**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())),
                                        "alg": "RS256"})

    now = int(time.time())
    claims = {"iss": apple.APPLE_ISSUER, "aud": BUNDLE, "sub": "000123.abc", "iat": now, "exp": now + 600}
    good = jwt.encode(claims, key, algorithm="RS256")
    assert apple.verify_identity_token(good, BUNDLE, Keys()) == "000123.abc"
    for bad in ({**claims, "aud": "com.other.app"}, {**claims, "iss": "https://evil.example"},
                {**claims, "exp": now - 10}):
        with pytest.raises(jwt.InvalidTokenError):
            apple.verify_identity_token(jwt.encode(bad, key, algorithm="RS256"), BUNDLE, Keys())
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(jwt.InvalidTokenError):
        apple.verify_identity_token(jwt.encode(claims, other, algorithm="RS256"), BUNDLE, Keys())


def test_apns_send_posts_a_signed_alert():
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/dead"):
            return httpx.Response(410, json={"reason": "Unregistered"})
        return httpx.Response(200)

    apns = apple.Apns(pem, "KEY123", "TEAM456", BUNDLE, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert apns.send("abc", "Title", "Body", {"push_id": "p1"}, sandbox=True) == (200, "")
    req = seen[0]
    assert str(req.url) == "https://api.sandbox.push.apple.com/3/device/abc"
    assert req.headers["apns-topic"] == BUNDLE and req.headers["apns-push-type"] == "alert"
    payload = json.loads(req.content)
    assert payload["aps"]["alert"] == {"title": "Title", "body": "Body"} and payload["push_id"] == "p1"
    assert payload["aps"]["category"] == "ARTICLE" and payload["aps"]["mutable-content"] == 1
    token = req.headers["authorization"].removeprefix("bearer ")
    assert jwt.get_unverified_header(token)["kid"] == "KEY123"
    assert jwt.decode(token, key.public_key(), algorithms=["ES256"])["iss"] == "TEAM456"
    status, reason = apns.send("dead", "t", "b")
    assert str(seen[1].url).startswith("https://api.push.apple.com/") and apple.Apns.dead(status, reason)
