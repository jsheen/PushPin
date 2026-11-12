"""The push card server: what a tapped notification opens.

  python -m pushnyt.cards        # serves on $PORT (default 8090)

Kept separate from the notifier's schedule so each can run where it fits: locally both are
containers in docker compose; in the cloud the cards are a Cloud Run service (scales with taps,
including to zero) while each slot is a Cloud Run job. Both share the database in DATABASE_URL
(Cloud SQL), or the SQLite file from configs/notifier.yml when it isn't set.

Each card shows the headline and abstract, Read on NYT (a plain nytimes.com link, so iOS opens the
NYT app if it's installed), ♥ and 🔖 Save. The first card a phone opens sets a cookie with a random
reader id, and everything the reader does is stored under it.
"""

import argparse
import html
import json
import os
import secrets
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from pushnyt.store import REACTIONS, Store

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>pushNYT</title>
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
.list .meta{font-size:13px;color:var(--muted);margin-top:2px}.list .done a{color:var(--muted)}
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
document.querySelector(".read").addEventListener("click", () => navigator.sendBeacon(`/read/${id}`));
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


def card_page(push: dict, reader: dict | None = None) -> str:
    """The landing card a notification tap opens: Read on NYT, ♥, Save, and who's reading."""
    e = html.escape

    def toggle(kind, on, off, pressed):
        return (f'<button data-kind="{kind}" data-on="{e(on)}" data-off="{e(off)}" '
                f'aria-pressed="{"true" if pressed else "false"}">{e(on if pressed else off)}</button>')

    if reader and reader.get("name"):
        who = f'Reading as {e(reader["name"])} · <a href="/saved">Reading list →</a>'
    else:
        who = ('<a href="/saved">Reading list →</a><form class="who"><input name="name" maxlength="40" '
               'placeholder="Your first name (optional)" autocomplete="given-name"><button>Save</button></form>')
    # a plain link to nytimes.com (not a redirect) lets iOS open it in the NYT app when installed
    return page(f"""<div class="kicker">{e(push["section"] or "")}</div>
<h1>{e(push["title"])}</h1>
<p class="abstract">{e(push["abstract"] or "")}</p>
<a class="read" href="{e(push["url"])}">Read on NYT</a>
<div class="row">{toggle("heart", "♥ Loved", "♡ Love", push["hearted"])}{toggle("save", "🔖 Saved", "🔖 Save", push["saved"])}</div>
<div class="foot">{who}</div>
{CARD_SCRIPT.replace("%ID%", json.dumps(push["push_id"]).replace("<", "\\u003c"))}""")


def reading_list_page(items: list[dict]) -> str:
    if not items:
        return page("<h1>Reading list</h1><p>Nothing saved yet. Tap 🔖 Save on a push.</p>")
    e = html.escape
    rows = "".join(
        f'<li class="{"done" if it["read"] else ""}"><a href="/p/{e(it["push_id"])}">{e(it["title"])}</a>'
        f'<div class="meta">{e(it["section"] or "")} · saved {e(it["saved_at"][:16].replace("T", " "))}'
        f'{" · read" if it["read"] else ""}</div></li>' for it in items)
    return page(f'<h1>Reading list</h1><ul class="list">{rows}</ul>')


COOKIE = "pushnyt_reader"


def serve_cards(store: Store, port: int, block: bool = False) -> ThreadingHTTPServer:
    """The phone-facing endpoints. The reader is identified by the `pushnyt_reader` cookie.

      GET  /p/<push_id>        the push's card; records the open (and creates the reader on first visit)
      POST /read/<push_id>     'Read on NYT' was tapped (sent by the card as a beacon)
      POST /heart/<push_id>    body "on" or "off": set or clear the ♥
      POST /save/<push_id>     body "on" or "off": save for later, or unsave
      POST /name               body: the reader's first name
      GET  /saved              this reader's reading list; its links open the cards
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
            if route == "name":
                ok = store.set_name(reader_id, body)
            elif route == "read" or route in REACTIONS:
                ok = store.interact(push_id, reader_id, route, on=body.strip() != "off")
            else:
                ok = False
            if not ok:
                self._send(404, "unknown push", "text/plain")
                return
            print(json.dumps({"event": route, "push_id": push_id or None, "reader": reader_id,
                              **({"on": body.strip() != "off"} if route in REACTIONS else {})}))
            self._send(200, "ok", "text/plain")

        def do_GET(self):
            route, push_id = self._route()
            if route == "health":
                self._send(200, "ok", "text/plain")
            elif route == "stats" and stats_token and secrets.compare_digest(
                    self.headers.get("x-api-key") or "", stats_token):
                self._send(200, json.dumps({"readers": store.reader_count()}), "application/json")
            elif route == "saved":
                self._send(200, reading_list_page(store.reading_list(self._reader_id())))
            elif route == "p" and (push := store.push(push_id)) is not None:
                reader_id, new = self._reader_id(), None
                if reader_id is None:
                    reader_id = new = store.new_reader(push["sent_ts"])
                store.interact(push_id, reader_id, "open")
                print(json.dumps({"event": "open", "push_id": push_id, "reader": reader_id, "new_reader": bool(new)}))
                self._send(200, card_page(store.push(push_id, reader_id), store.reader(reader_id)), set_reader=new)
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
    parser = argparse.ArgumentParser(description="Serve pushNYT push cards")
    parser.add_argument("--config", type=Path, default=Path("configs/notifier.yml"))
    args = parser.parse_args()
    config = yaml.safe_load(args.config.read_text())
    port = int(os.environ.get("PORT", config["click_port"]))
    print(f"serving cards on :{port}", flush=True)
    serve_cards(open_store(config), port, block=True)


if __name__ == "__main__":
    main()
