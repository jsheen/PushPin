"""The push card server: what a tapped notification opens.

  python -m pushpin.cards        # serves on $PORT (default 8090)

Kept separate from the notifier's schedule so each can run where it fits: locally both are
containers in docker compose; in the cloud the cards are a Cloud Run service (scales with taps,
including to zero) while each slot is a Cloud Run job. Both share the database in DATABASE_URL
(Cloud SQL), or the SQLite file from configs/notifier.yml when it isn't set.

Each card shows the headline and abstract, Read on NYT (a plain nytimes.com link, so iOS opens the
NYT app if it's installed), 🔖 Save, and how many pushes a day the reader wants (light,
medium or heavy). The first card a phone opens, or the /start page (linked from the welcome push,
`pushpin.notifier --welcome`), sets a cookie with a random reader id, and everything the reader
does is stored under it. Opening the card is the click; what they do on NYT isn't tracked.

/me is the reader's profile: up to three favorite articles on top, and below, how many articles
they've read (opened a card for) with the list, which they can make public. /u/<slug> is the
same page for anyone with the link: favorites always, the count and list only when public.
"""

import argparse
import html
import json
import os
import secrets
import threading
import time
from datetime import date, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from pushpin.store import MAX_FAVORITES, REACTIONS, VOLUMES, Store

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>PushPin</title>
<style>
:root{--bg:#fff;--fg:#121212;--muted:#666;--line:#e2e2e2;--btn:#f1f1f1;--on:#d0021b;--link:#326891}
@media (prefers-color-scheme:dark){:root{--bg:#121212;--fg:#eee;--muted:#999;--line:#333;--btn:#262626;
--on:#ff5a6e;--link:#8ab4f8}}
body{font:17px/1.45 -apple-system,system-ui,sans-serif;margin:0;padding:24px 16px;background:var(--bg);color:var(--fg)}
main{max-width:600px;margin:0 auto}
.kicker{font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
h1{font:700 26px/1.2 Georgia,"Times New Roman",serif;margin:6px 0 12px}
.abstract{color:var(--muted);margin:0 0 24px}
.read{display:block;text-align:center;background:var(--fg);color:var(--bg);text-decoration:none;
font-weight:600;padding:14px;border-radius:10px}
.row{display:flex;gap:10px;margin-top:10px}
.row button{flex:1;font:inherit;font-weight:600;padding:12px;border:0;border-radius:10px;
background:var(--btn);color:var(--fg)}
.row button[aria-pressed=true]{color:var(--on)}
.foot{margin-top:28px;font-size:15px;color:var(--muted)}.foot a,.list a{color:var(--link);text-decoration:none}
.who{display:flex;gap:8px;margin-top:8px}.who input{flex:1;font:inherit;padding:8px 10px;border:1px solid var(--line);
border-radius:8px;background:var(--bg);color:var(--fg)}.who button{font:inherit;padding:8px 12px;border:0;
border-radius:8px;background:var(--btn);color:var(--fg)}
.list{padding:0;margin:0}.list li{list-style:none;padding:12px 0;border-bottom:1px solid var(--line)}
.list .meta{font-size:13px;color:var(--muted);margin-top:2px}
.volume{margin:0 0 20px;padding:14px;border:1px solid var(--line);border-radius:12px}
.volume p{margin:0 0 4px;font-weight:600}.volume .hint{font-weight:400;font-size:14px;color:var(--muted)}
.volume .row button[aria-pressed=true]{background:var(--fg);color:var(--bg)}
.volume .row button small{display:block;font-weight:400;font-size:13px;opacity:.75}
.profile header{margin-bottom:40px}.profile h1{margin:0 0 4px}
.profile .sub{color:var(--muted);font-size:15px;margin:0}
.profile section{border-top:1px solid var(--line);padding:24px 0 8px}
.profile h2{font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:600;margin:0 0 12px}
.profile .list li{display:flex;gap:12px;align-items:flex-start}.profile .list li>div{flex:1;min-width:0}
.profile .list a{color:var(--fg)}.profile .list a:hover{color:var(--link)}
.profile .empty{color:var(--muted)}
.star{font:inherit;font-size:20px;line-height:1;padding:2px 4px;border:0;background:none;color:var(--muted);cursor:pointer}
.star[aria-pressed=true]{color:var(--on)}
.total{font-size:17px;margin:2px 0 2px}.since{font-size:15px;color:var(--muted);margin:0 0 6px}.total strong{font-weight:700}
.toggle{display:flex;align-items:center;gap:8px;font-size:15px;margin:0 0 6px;cursor:pointer}
.toggle input{width:18px;height:18px;margin:0}
.share{font-size:14px;color:var(--muted);margin:0 0 12px;word-break:break-all}.share a{color:var(--link)}
.splash{position:fixed;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;
padding:16px;background:var(--bg);z-index:10;cursor:pointer;outline:none;transition:opacity .6s ease,visibility .6s}
.splash.gone{opacity:0;visibility:hidden}
.splash>div{background:#fff;color:#121212;border-radius:20px;padding:48px 32px;text-align:center;
width:min(100%,460px);box-sizing:border-box;box-shadow:0 16px 60px rgba(0,0,0,.3);font-size:24px;line-height:1.5;
animation:splash-in .6s ease both}
.splash strong{display:block;font:700 36px/1.2 Georgia,"Times New Roman",serif;margin-bottom:16px}
.splash .tap{margin-top:20px;font-size:14px;color:var(--muted);animation:splash-in .6s ease 1.2s both}
.splash~.profile{opacity:0;transition:opacity .6s ease}.splash.gone~.profile{opacity:1}
@keyframes splash-in{from{opacity:0;transform:scale(.96)}to{opacity:1;transform:none}}
@media (prefers-reduced-motion:reduce){.splash,.splash~.profile{transition:none}.splash>div,.splash .tap{animation:none}}
.profile summary{display:flex;align-items:center;justify-content:space-between;cursor:pointer;list-style:none;
margin:0 0 12px}.profile summary::-webkit-details-marker{display:none}.profile summary h2{margin:0}
.profile summary .n{color:var(--fg);margin-left:6px;letter-spacing:0}
.plus{font-size:24px;line-height:1;color:var(--muted);transition:transform .2s ease}details[open] .plus{transform:rotate(45deg)}
.profile details+.toggle{margin-top:4px}
.note{font-size:14px;color:var(--on);min-height:1em;margin:0 0 8px}
</style></head><body><main>%BODY%</main></body></html>"""

CARD_SCRIPT = """<script>
const id = %ID%;
document.querySelectorAll("button[data-kind]").forEach(b => b.addEventListener("click", async () => {
  const on = b.getAttribute("aria-pressed") !== "true";
  b.setAttribute("aria-pressed", on);
  b.textContent = b.dataset[on ? "on" : "off"];
  const r = await fetch(`/${b.dataset.kind}/${id}`, {method: "POST", body: on ? "on" : "off"}).catch(() => null);
  if (!r || !r.ok) { b.setAttribute("aria-pressed", !on); b.textContent = b.dataset[on ? "off" : "on"]; }
}));
document.querySelectorAll("button[data-volume]").forEach(b => b.addEventListener("click", async () => {
  const r = await fetch("/volume", {method: "POST", body: b.dataset.volume}).catch(() => null);
  if (!r || !r.ok) return;
  document.querySelectorAll("button[data-volume]").forEach(x => x.setAttribute("aria-pressed", x === b));
  const hint = document.querySelector(".volume .hint");
  if (hint) hint.textContent = "Saved. You can change it on any push.";
}));
const who = document.querySelector(".who");
if (who) who.addEventListener("submit", async ev => {
  ev.preventDefault();
  const name = who.querySelector("input").value.trim();
  if (name && (await fetch("/name", {method: "POST", body: name}).catch(() => null))?.ok)
    who.outerHTML = `<span>Thanks, ${name.replace(/[<>&"]/g, "")}.</span>`;
});
</script>"""


def page(body: str) -> str:
    return PAGE.replace("%BODY%", body)


VOLUME_LABELS = {"light": ("Light", "1 a day"), "medium": ("Medium", "up to 2"), "heavy": ("Heavy", "up to 4")}


def volume_picker(current: str | None, caps: dict | None = None) -> str:
    """Light / medium / heavy buttons. Before the reader has chosen, with a prompt."""
    e = html.escape
    buttons = "".join(
        f'<button data-volume="{v}" aria-pressed="{"true" if v == current else "false"}">{e(VOLUME_LABELS[v][0])}'
        f'<small>{e(f"up to {caps[v]}" if caps else VOLUME_LABELS[v][1])}</small></button>' for v in VOLUMES)
    if current:
        return f'<div class="volume"><p>Pushes a day</p><div class="row">{buttons}</div></div>'
    return (f'<div class="volume"><p>How many pushes a day?</p><div class="hint">Only sent when there\'s '
            f'a story worth it, so some days get fewer.</div><div class="row">{buttons}</div></div>')


def card_page(push: dict, reader: dict | None = None, caps: dict | None = None) -> str:
    """The landing card a notification tap opens: Read on NYT, Save, push volume, and who's reading."""
    e = html.escape

    def toggle(kind, on, off, pressed):
        return (f'<button data-kind="{kind}" data-on="{e(on)}" data-off="{e(off)}" '
                f'aria-pressed="{"true" if pressed else "false"}">{e(on if pressed else off)}</button>')

    if reader and reader.get("name"):
        who = f'Reading as {e(reader["name"])} · <a href="/saved">Reading list</a> · <a href="/me">Profile →</a>'
    else:
        who = ('<a href="/saved">Reading list</a> · <a href="/me">Profile →</a><form class="who"><input name="name" maxlength="40" '
               'placeholder="Your first name (optional)" autocomplete="given-name"><button>Save</button></form>')
    volume = (reader or {}).get("volume")
    # a plain link to nytimes.com (not a redirect) lets iOS open it in the NYT app when installed
    return page(f"""{"" if volume else volume_picker(None, caps)}<div class="kicker">{e(push["section"] or "")}</div>
<h1>{e(push["title"])}</h1>
<p class="abstract">{e(push["abstract"] or "")}</p>
<a class="read" href="{e(push["url"])}">Read on NYT</a>
<div class="row">{toggle("save", "🔖 Saved", "🔖 Save", push["saved"])}</div>
<div class="foot">{volume_picker(volume, caps) if volume else ""}{who}</div>
{CARD_SCRIPT.replace("%ID%", json.dumps(push["push_id"]).replace("<", "\\u003c"))}""")


def start_page(reader: dict | None, caps: dict | None = None) -> str:
    """The welcome push's page: pick how many pushes a day, before the first real one."""
    return page(f"""<h1>PushPin</h1><p class="abstract">A few NYT stories a day, picked for you. Tap a
push to open its card; that's how it learns what you like.</p>{volume_picker((reader or {}).get("volume"), caps)}
{CARD_SCRIPT.replace("%ID%", "null")}""")


def reading_list_page(items: list[dict]) -> str:
    if not items:
        return page("<h1>Reading list</h1><p>Nothing saved yet. Tap 🔖 Save on a push.</p>")
    e = html.escape
    rows = "".join(
        f'<li><a href="/p/{e(it["push_id"])}">{e(it["title"])}</a>'
        f'<div class="meta">{e(it["section"] or "")} · saved {e(it["saved_at"][:16].replace("T", " "))}'
        f'</div></li>' for it in items)
    return page(f'<h1>Reading list</h1><ul class="list">{rows}</ul>')


PROFILE_SCRIPT = """<script>
const folded = document.querySelector("details");
if (folded && location.hash === "#read") folded.open = true;
const pub = document.querySelector("#public");
if (pub) pub.addEventListener("change", async () => {
  const r = await fetch("/public", {method: "POST", body: pub.checked ? "on" : "off"}).catch(() => null);
  if (!r || !r.ok) { pub.checked = !pub.checked; return; }
  document.querySelector(".share").hidden = !pub.checked;
});
document.querySelectorAll("button[data-fav]").forEach(b => b.addEventListener("click", async () => {
  const on = b.getAttribute("aria-pressed") !== "true";
  const r = await fetch(`/favorite/${encodeURIComponent(b.dataset.fav)}`,
                        {method: "POST", body: on ? "on" : "off"}).catch(() => null);
  if (r && r.ok) { history.replaceState(null, "", "#read"); return location.reload(); }  // reopen the list
  document.querySelector(".note").textContent = r && r.status === 409
    ? "You already have three favorites. Remove one first." : "Couldn't save that, try again.";
}));
</script>"""


def streak(read: list[dict], today: date) -> int:
    """Days in a row, up to today, with at least one article read. Still alive if the last read was
    yesterday: today isn't over."""
    days = {date.fromisoformat(a["read_at"][:10]) for a in read if a.get("read_at")}
    day = today if today in days else today - timedelta(days=1)
    n = 0
    while day in days:
        n, day = n + 1, day - timedelta(days=1)
    return n


def splash(title: str, lines: list[str]) -> str:
    """A large white window in place of the page: fades in and stays until a tap, click or key,
    then fades out as the page fades in. Without JavaScript it's skipped."""
    return (f'<div class="splash" role="button" tabindex="0" aria-label="Continue"><div><strong>{title}</strong>'
            f'{"".join(f"<div>{html.escape(x)}</div>" for x in lines)}</div><p class="tap">Tap to continue</p></div>'
            + SPLASH_SCRIPT)


SPLASH_SCRIPT = """<noscript><style>.splash{display:none}.splash~.profile{opacity:1}</style></noscript><script>
(() => {
  const s = document.querySelector(".splash");
  const go = () => s.classList.add("gone");
  s.addEventListener("click", go, {once: true});
  document.addEventListener("keydown", go, {once: true});
  s.focus();
})();
</script>"""


def profile_page(reader: dict, read: list[dict], favorites: list[str], owner: bool, share_url: str = "",
                 today: date | None = None) -> str:
    """A reader's profile. `owner`: their own view (/me), with the star buttons, the public toggle
    and always the list; otherwise the public view (/u/<slug>), where the count and list only show
    if they made them public. Article links go straight to NYT, so a visitor isn't counted as opening."""
    e = html.escape
    by_id = {a["news_id"]: a for a in read}
    favs = [by_id[n] for n in favorites if n in by_id]
    fav_ids = {a["news_id"] for a in favs}
    name = reader.get("name")
    public = bool(reader.get("history_public"))

    def item(a, star):
        on = a["news_id"] in fav_ids
        button = (f'<button class="star" data-fav="{e(a["news_id"])}" aria-pressed="{"true" if on else "false"}" '
                  f'aria-label="Favorite">{"★" if on else "☆"}</button>') if star else ""
        return (f'<li><div><a href="{e(a["url"] or "#")}">{e(a["title"] or "Untitled")}</a>'
                f'</div>{button}</li>')

    slots = "".join(item(a, owner) for a in favs)
    if owner:
        slots += "".join(f'<li class="empty">{"Star an article you’ve read below." if i == len(favs) else "—"}</li>'
                         for i in range(len(favs), MAX_FAVORITES))
    elif not favs:
        slots = '<li class="empty">No favorites yet.</li>'
    top = f'<section><h2>Favorites</h2><ul class="list">{slots}</ul></section>'

    n = len(read)
    first = date.fromisoformat(min(a["read_at"] for a in read)[:10]) if read else None
    since = f'<p class="since">Started reading on {first:%B} {first.day}, {first.year}</p>' if first else ""
    count = (f'<p class="total"><strong>{n}</strong> article{"" if n == 1 else "s"} read</p>{since}'
             if owner or public else "")
    # the list is folded under "Read  n  +"; tapping + opens it
    articles = (f'<details><summary><h2>Read <span class="n">{n}</span></h2><span class="plus" aria-hidden="true">+</span>'
                f'</summary><ul class="list">{"".join(item(a, owner) for a in read)}</ul></details>' if read
                else '<h2>Read</h2><p class="empty">Nothing yet. Open a push to start.</p>')
    if owner:
        bottom = (f'{articles}<label class="toggle"><input type="checkbox" id="public"{" checked" if public else ""}> '
                  f'Make my reading public</label><p class="share"{"" if public else " hidden"}>Anyone with this link '
                  f'can see what you’ve read: <a href="{e(share_url)}">{e(share_url)}</a></p>'
                  f'<p class="note" aria-live="polite"></p>')
    elif public:
        bottom = articles
    else:
        bottom = '<h2>Read</h2><p class="empty">Reading history is private.</p>'

    title = f"{e(name)}’s PushPin" if name else ("Your PushPin" if owner else "A reader’s PushPin")
    sub = '<p class="sub">Only you see this page. Favorites show on your public link.</p>' if owner else ""
    ask_name = ('<form class="who"><input name="name" maxlength="40" placeholder="Your first name (optional)" '
                'autocomplete="given-name"><button>Save</button></form>') if owner and not name else ""
    scripts = (CARD_SCRIPT.replace("%ID%", "null") + PROFILE_SCRIPT) if owner else ""
    intro = ""
    if not owner:  # the public link opens with a short summary
        days = streak(read, today or date.today())
        lines = [f'{n} article{"" if n == 1 else "s"} read', *([f"{days} day streak"] if days else [])] if public else []
        intro = splash(title, lines)
    body = (f'{intro}<div class="profile"><header><h1>{title}</h1>{count}{sub}{ask_name}</header>{top}'
            f'<section>{bottom}</section></div>{scripts}')
    return page(body).replace("<main>", '<main style="max-width:540px">', 1)


COOKIE = "pushpin_reader"


def serve_cards(store: Store, port: int, block: bool = False, caps: dict | None = None) -> ThreadingHTTPServer:
    """The phone-facing endpoints. The reader is identified by the `pushpin_reader` cookie.
    `caps` maps each volume to its daily cap (configs/notifier.yml), shown on the buttons.

      GET  /p/<push_id>        the push's card; records the open (and creates the reader on first visit)
      GET  /start              the welcome page: choose light / medium / heavy (creates the reader)
      POST /volume             body "light", "medium" or "heavy": pushes a day
      POST /save/<push_id>     body "on" or "off": save for later, or unsave
      POST /name               body: the reader's first name
      GET  /saved              this reader's reading list; its links open the cards
      GET  /me                 this reader's profile: favorites, articles read, public toggle
      POST /favorite/<news_id> body "on" or "off": star an article they've read (409 past three)
      POST /public             body "on" or "off": show the count and list on their public link
      GET  /u/<slug>           a reader's public profile
      GET  /stats              {"readers": n} for autoscaling (KEDA), only with header
                               X-Api-Key = $CARDS_STATS_TOKEN; 404 when that isn't set
      GET  /health             liveness
    """

    stats_token = os.environ.get("CARDS_STATS_TOKEN")

    class Handler(BaseHTTPRequestHandler):
        def _reader_id(self) -> str | None:
            jar = SimpleCookie(self.headers.get("cookie") or "")
            rid = jar[COOKIE].value if COOKIE in jar else None
            return rid if store.reader(rid) else None

        def _send(self, status: int, body: str = "", content_type: str = "text/html; charset=utf-8",
                  set_reader: str | None = None) -> None:
            data = body.encode()
            self.send_response(status)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(data)))
            self.send_header("cache-control", "no-store")
            if set_reader:
                secure = "; Secure" if self.headers.get("x-forwarded-proto") == "https" else ""
                self.send_header("set-cookie", f"{COOKIE}={set_reader}; Max-Age=63072000; Path=/; "
                                               f"HttpOnly; SameSite=Lax{secure}")
            self.end_headers()
            self.wfile.write(data)

        def _route(self) -> tuple[str, str]:
            route, _, push_id = self.path.split("?")[0].lstrip("/").partition("/")
            return route, push_id

        def do_POST(self):
            route, push_id = self._route()
            body = self.rfile.read(min(int(self.headers.get("content-length") or 0), 200)).decode(errors="replace")
            reader_id = self._reader_id()
            if reader_id is None:
                self._send(403, "open a push first", "text/plain")
                return
            if route == "favorite":
                result = store.set_favorite(reader_id, push_id, on=body.strip() != "off")
                if result == "full":
                    self._send(409, "three favorites already", "text/plain")
                    return
                ok = result == "ok"
            elif route == "public":
                ok = store.set_history_public(reader_id, body.strip() == "on")
            elif route == "name":
                ok = store.set_name(reader_id, body)
            elif route == "volume":
                ok = store.set_volume(reader_id, body.strip())
            elif route in REACTIONS:
                ok = store.interact(push_id, reader_id, route, on=body.strip() != "off")
            else:
                ok = False
            if not ok:
                self._send(404, "unknown push", "text/plain")
                return
            print(json.dumps({"event": route, "push_id": push_id or None, "reader": reader_id,
                              **({"on": body.strip() != "off"} if route in (*REACTIONS, "favorite", "public") else {}),
                              **({"volume": body.strip()} if route == "volume" else {})}))
            self._send(200, "ok", "text/plain")

        def do_GET(self):
            route, push_id = self._route()
            if route == "health":
                self._send(200, "ok", "text/plain")
            elif route == "stats" and stats_token and secrets.compare_digest(
                    self.headers.get("x-api-key") or "", stats_token):
                self._send(200, json.dumps({"readers": store.reader_count()}), "application/json")
            elif route == "start":
                reader_id, new = self._reader_id(), None
                if reader_id is None:  # subscribed now: pushes from here on count for them
                    reader_id = new = store.new_reader(int(time.time()))
                self._send(200, start_page(store.reader(reader_id), caps), set_reader=new)
            elif route == "saved":
                self._send(200, reading_list_page(store.reading_list(self._reader_id())))
            elif route == "me":
                reader_id = self._reader_id()
                if reader_id is None:
                    self._send(200, page("<h1>Your reading</h1><p>Open a push first; your profile starts there.</p>"))
                    return
                proto = self.headers.get("x-forwarded-proto") or "http"
                share = f"{proto}://{self.headers.get('host') or 'localhost'}/u/{store.slug(reader_id)}"
                self._send(200, profile_page(store.reader(reader_id), store.articles_read(reader_id),
                                             store.favorites(reader_id), owner=True, share_url=share))
            elif route == "u" and (other := store.reader_by_slug(push_id)) is not None:
                rid = other["reader_id"]
                self._send(200, profile_page(other, store.articles_read(rid), store.favorites(rid), owner=False,
                                             today=date.fromisoformat(store.now()[:10])))
            elif route == "p" and (push := store.push(push_id)) is not None:
                reader_id, new = self._reader_id(), None
                if reader_id is None:
                    reader_id = new = store.new_reader(push["sent_ts"])
                store.interact(push_id, reader_id, "open")
                print(json.dumps({"event": "open", "push_id": push_id, "reader": reader_id, "new_reader": bool(new)}))
                self._send(200, card_page(store.push(push_id, reader_id), store.reader(reader_id), caps),
                           set_reader=new)
            else:
                self._send(404, page("<p>Unknown push.</p>"))

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    if block:
        server.serve_forever()
    else:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def open_store(config: dict) -> Store:
    """DATABASE_URL (Postgres in the cloud) if set, else the SQLite file from the config."""
    return Store(os.environ.get("DATABASE_URL") or config["db"], config["click_window_hours"],
                 ZoneInfo(config["timezone"]))


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve PushPin push cards")
    parser.add_argument("--config", type=Path, default=Path("configs/notifier.yml"))
    args = parser.parse_args()
    config = yaml.safe_load(args.config.read_text())
    port = int(os.environ.get("PORT", config["click_port"]))
    print(f"serving cards on :{port}", flush=True)
    serve_cards(open_store(config), port, block=True, caps=config.get("volume_caps"))


if __name__ == "__main__":
    main()
