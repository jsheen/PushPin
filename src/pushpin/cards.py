"""The push card server: what a tapped notification opens.

  python -m pushpin.cards        # serves on $PORT (default 8090)

Kept separate from the notifier's schedule so each can run where it fits: locally both are
containers in docker compose; in the cloud the cards are a Cloud Run service (scales with taps,
including to zero) while each slot is a Cloud Run job. Both share the database in DATABASE_URL
(Cloud SQL), or the SQLite file from configs/notifier.yml when it isn't set.

Tapping a push "pins" its article: the open is recorded (the click the model learns from) and the
reader lands on their profile with that article on top and a Read button (a plain nytimes.com
link, so iOS opens the NYT app if it's installed; the tap is recorded as a read).
The first push a phone opens, or the /start page (linked from the welcome push,
`pushpin.notifier --welcome`), sets a cookie with a random reader id, and everything the reader
does is stored under it. What they do on NYT isn't tracked.

/me is the same profile without a pinned article: the articles they pinned but haven't read yet
(tap to read, × to delete), how many articles they've read (tapped Read on) with the list (×
deletes one), which they can make public, and how many pushes a day they want (light, medium or
heavy). /u/<slug> is the profile for anyone with the link: the count and list only when public.
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

import jwt
import yaml

from pushpin import apple
from pushpin.store import DEFAULT_VOLUME, VOLUMES, Store

# the red pushpin (scripts/make_icons.py): browser tab, home screen, ntfy's notification icon
ASSETS = Path(__file__).parent / "assets"
ICONS = {"favicon.ico": "favicon.png", "favicon.png": "favicon.png", "apple-touch-icon.png": "apple-touch-icon.png",
         "icon.png": "icon.png"}

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>PushPin</title>
<link rel="icon" type="image/png" href="/favicon.png"><link rel="apple-touch-icon" href="/apple-touch-icon.png">
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
.profile header{margin-bottom:40px}.profile h1{margin:0 0 4px}.pinmark{height:1.15em;width:auto;vertical-align:-.22em;margin-left:.05em}
.pm{display:inline-flex;align-items:center;gap:7px;font:600 11px/1 -apple-system,system-ui,sans-serif;letter-spacing:.07em;
text-transform:uppercase;color:var(--muted);background:none;border:0;padding:6px 0;cursor:pointer;transition:color .25s ease}
.pm .track{position:relative;width:26px;height:15px;border-radius:8px;background:var(--line);transition:background .25s ease}
.pm .knob{position:absolute;top:2px;left:2px;width:11px;height:11px;border-radius:50%;background:#fff;
box-shadow:0 1px 2px rgba(0,0,0,.3);transition:transform .25s ease}
.pm[aria-pressed=true]{color:var(--fg)}.pm[aria-pressed=true] .track{background:var(--on)}.pm[aria-pressed=true] .knob{transform:translateX(11px)}
.papier-hint{font-size:14px;color:var(--muted);margin:0 0 14px}
.papier{opacity:0;transition:opacity .5s ease;margin-top:4px;display:grid;grid-template-columns:repeat(4,1fr);gap:10px 8px}
.papier.on{opacity:1}.papier[hidden]{display:none}
.papier-today{margin:0 0 18px}.papier-today h3{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:600;margin:0 0 6px}
.papier-today figure{margin:0 auto;width:min(100%,340px)}.papier-today canvas{display:block;width:100%}
.readpanel .today-list{margin:0 0 6px}.readpanel .past-head{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:600;margin:22px 0 2px}
.readpanel .past-head[hidden]{display:none}.papier figure{margin:0}
.papier figure canvas{display:block;width:100%}.papier .still,.papier-today .still{cursor:default}.papier .still:hover,.papier-today .still:hover{background:none}
.papier canvas{display:block;width:100%}.papier .dino,.papier-today .dino{display:block;box-sizing:border-box;width:100%;padding:4px 2px;border:0;border-radius:8px;background:none;cursor:pointer;
-webkit-tap-highlight-color:transparent;transition:transform .2s ease,background .2s ease}.papier .dino:hover,.papier-today .dino:hover{background:var(--btn)}
.papier .dino:active,.papier-today .dino:active{transform:scale(.94)}
.sharecard{border:0;padding:44px 16px 16px;border-radius:18px;background:var(--bg);color:var(--fg);width:min(92vw,420px);
box-sizing:border-box;box-shadow:0 24px 80px rgba(0,0,0,.35)}.sharecard::backdrop{background:rgba(0,0,0,.55)}
.sharecard img{display:block;max-width:100%;max-height:58vh;margin:0 auto;min-height:120px;object-fit:contain}
.sharecard .save{flex:1;text-align:center;font-weight:600;padding:12px;border-radius:10px;background:var(--btn);color:var(--fg);text-decoration:none}
.sharecard .x{position:absolute;top:6px;right:8px;font:28px/1 -apple-system,system-ui,sans-serif;border:0;background:none;color:var(--muted);padding:4px 8px;cursor:pointer}
.sharecard .row{margin-top:14px}.sharecard .go{background:var(--fg);color:var(--bg)}
.sharecard:has(.go[hidden]) .save{background:var(--fg);color:var(--bg)}.papier figcaption{font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);text-align:center;margin-top:2px}
.profile .sub{color:var(--muted);font-size:15px;margin:0}.profile header .toggle{margin:12px 0 6px}
.profile section{border-top:1px solid var(--line);padding:24px 0 8px}
.profile h2{font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:600;margin:0 0 12px}
.profile .list li{display:flex;gap:12px;align-items:flex-start}.profile .list li>div{flex:1;min-width:0}
.profile .list a{color:var(--fg)}.profile .list a:hover{color:var(--link)}
.profile .empty{color:var(--muted)}.profile .private{font-size:14px;color:var(--muted);margin:-6px 0 8px}.profile h2 .streak{margin-left:4px}.profile h2 .n{color:var(--fg);margin-left:6px;letter-spacing:0}
.profile section>.list>li:last-child{border-bottom:0}
.del{font:inherit;font-size:20px;line-height:1;padding:2px 4px;border:0;background:none;color:var(--muted);cursor:pointer}
.del:hover{color:var(--on)}
.since{font-size:15px;color:var(--muted);margin:2px 0 6px}
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
.splash .stat{font-size:20px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:#666}
.splash .stat .n{color:#121212;margin-left:6px;letter-spacing:0}
.splash .tap{margin-top:20px;font-size:14px;color:var(--muted);animation:splash-in .6s ease 1.2s both}
.splash~.profile{opacity:0;transition:opacity .6s ease}.splash.gone~.profile{opacity:1}
@keyframes splash-in{from{opacity:0;transform:scale(.96)}to{opacity:1;transform:none}}
@media (prefers-reduced-motion:reduce){.splash,.splash~.profile{transition:none}.splash>div,.splash .tap{animation:none}}
.profile .readhead{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 0 12px}
.profile .readhead h2{margin:0}
.profile .readpanel+.toggle{margin-top:4px}
.note{font-size:14px;color:var(--on);min-height:1em;margin:0 0 8px}
.pin{margin:0 0 40px}.pin .headline{font:700 26px/1.2 Georgia,"Times New Roman",serif;margin:6px 0 12px}
.profile .volume{margin:24px 0 0}
</style></head><body><main>%BODY%</main></body></html>"""

OWNER_SCRIPT = """<script>
document.querySelectorAll("button[data-volume]").forEach(b => b.addEventListener("click", async () => {
  const r = await fetch("/volume", {method: "POST", body: b.dataset.volume}).catch(() => null);
  if (!r || !r.ok) return;
  document.querySelectorAll("button[data-volume]").forEach(x => x.setAttribute("aria-pressed", x === b));
  // welcome page: show the choice for half a second, then on to the profile
  if (location.pathname === "/start") return setTimeout(() => location.assign("/me"), 500);
  const hint = document.querySelector(".volume .hint");
  if (hint) hint.textContent = "Saved. You can change it on your profile.";
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


VOLUME_LABELS = {"light": ("Light", "up to 2"), "medium": ("Medium", "up to 4"), "heavy": ("Heavy", "up to 8")}


def volume_picker(current: str | None, caps: dict | None = None, ask: bool = False) -> str:
    """Light / medium / heavy buttons, the reader's choice pressed (medium until they choose).
    `ask`: as a question, with a hint (the welcome page); nothing pressed, all three grey."""
    e = html.escape
    current = None if ask else current or DEFAULT_VOLUME
    buttons = "".join(
        f'<button data-volume="{v}" aria-pressed="{"true" if v == current else "false"}">{e(VOLUME_LABELS[v][0])}'
        f'<small>{e(f"up to {caps[v]}" if caps else VOLUME_LABELS[v][1])}</small></button>' for v in VOLUMES)
    if not ask:
        return f'<div class="volume"><p>Pushes a day</p><div class="row">{buttons}</div></div>'
    return (f'<div class="volume"><p>How many pushes a day?</p><div class="hint">Only sent when there\'s '
            f'a story worth it, so some days get fewer. You can always change it later.</div><div class="row">{buttons}</div></div>')


def start_page(reader: dict | None, caps: dict | None = None) -> str:
    """The welcome push's page: pick how many pushes a day, before the first real one."""
    return page(f"""<h1>PushPin</h1><p class="abstract">A few NYT stories a day, picked for you. Tap a
push to pin it, then read it; that's how it learns what you like.</p>
{volume_picker(None, caps, ask=True)}{OWNER_SCRIPT}""")


PROFILE_SCRIPT = """<script>
const pub = document.querySelector("#public");
if (pub) pub.addEventListener("change", async () => {
  const r = await fetch("/public", {method: "POST", body: pub.checked ? "on" : "off"}).catch(() => null);
  if (!r || !r.ok) { pub.checked = !pub.checked; return; }
  document.querySelector(".share").hidden = !pub.checked;
});
// ×: delete an article from the profile, after a confirm
document.querySelectorAll("button[data-del]").forEach(b => b.addEventListener("click", async () => {
  if (!confirm(b.dataset.confirm)) return;
  const r = await fetch(`/remove/${encodeURIComponent(b.dataset.del)}`, {method: "POST", body: "on"}).catch(() => null);
  if (r && r.ok) return location.replace("/me");
  document.querySelector(".note").textContent = "Couldn't delete that, try again.";
}));
// Read: record the tap, then follow the plain nytimes.com link (so iOS can open the NYT app)
document.querySelectorAll("a[data-read]").forEach(a => a.addEventListener("click", () => {
  const url = `/read/${encodeURIComponent(a.dataset.read)}`;
  if (!(navigator.sendBeacon && navigator.sendBeacon(url, "on")))
    fetch(url, {method: "POST", body: "on", keepalive: true}).catch(() => null);
}));
addEventListener("pageshow", ev => { if (ev.persisted) location.reload(); });  // back from NYT: fresh count
</script>"""


# The demo's owner view: its buttons change the page only (nothing is sent, so no real reader changes)
INERT_SCRIPT = """<script>
document.querySelectorAll("button[data-volume]").forEach(b => b.addEventListener("click", () =>
  document.querySelectorAll("button[data-volume]").forEach(x => x.setAttribute("aria-pressed", x === b))));
const pub = document.querySelector("#public");
if (pub) pub.addEventListener("change", () => { document.querySelector(".share").hidden = !pub.checked; });
// ×: gone from the list, and out of its day's count (read or pinned), so the pictures redraw
document.querySelectorAll("button[data-del]").forEach(b => b.addEventListener("click", () => {
  const li = b.closest("li"), day = li.dataset.day, wasRead = !li.querySelector("a[data-read]"), title = li.querySelector("a").textContent;
  li.remove();
  const h = [...document.querySelectorAll(".profile h2")].find(x => x.textContent.startsWith(wasRead ? "Read" : "Pinned"));
  const n = h && h.querySelector(".n");
  if (n) n.textContent = Math.max(0, +n.textContent - 1);  // the header's count
  const el = document.getElementById("papier-data"), days = el && JSON.parse(el.textContent), d = days && days.find(x => x.day === day);
  if (!d) return;
  d.pinned -= 1;
  if (wasRead) { d.read -= 1; const i = d.titles.indexOf(title); if (i >= 0) { d.titles.splice(i, 1); d.abstracts.splice(i, 1); } }
  if (d.pinned <= 0) days.splice(days.indexOf(d), 1);
  el.textContent = JSON.stringify(days); dispatchEvent(new Event("papier:update"));
}));
const who = document.querySelector(".who");
if (who) who.addEventListener("submit", ev => ev.preventDefault());
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


def splash(title: str, stats: list[tuple[str, int]]) -> str:
    """A large white window in place of the page: fades in and stays until a tap, click or key,
    then fades out as the page fades in; a tap on the page (off its links) brings it back. Each stat is a line like the page's headers: READ 4.
    Without JavaScript it's skipped."""
    lines = "".join(f'<div class="stat">{html.escape(label)} <span class="n">{value}</span></div>' for label, value in stats)
    return (f'<div class="splash" role="button" tabindex="0" aria-label="Continue"><div><strong>{title}</strong>'
            f'{lines}</div><p class="tap">Tap to continue</p></div>'
            + SPLASH_SCRIPT)


SPLASH_SCRIPT = """<noscript><style>.splash{display:none}.splash~.profile{opacity:1}</style></noscript><script>
(() => {
  const s = document.querySelector(".splash");
  const go = () => s.classList.add("gone");
  s.addEventListener("click", ev => { ev.stopPropagation(); go(); });
  document.addEventListener("keydown", () => { if (!s.classList.contains("gone")) go(); });
  // once it's gone, a tap on the page (not on a link or control) brings the card back
  document.addEventListener("click", ev => {
    if (!s.classList.contains("gone") || ev.target.closest("a, button, input, label, summary, dialog")) return;
    s.classList.remove("gone");
    s.focus();
  });
  s.focus();
})();
</script>"""


def pin_panel(push: dict) -> str:
    """The article a push tap pinned, on top of the profile, with Read."""
    e = html.escape
    return (f'<section class="pin"><div class="kicker">{e(push["section"] or "")}</div>'
            f'<div class="headline">{e(push["title"] or "")}</div><p class="abstract">{e(push["abstract"] or "")}</p>'
            # a plain link to nytimes.com (not a redirect) lets iOS open it in the NYT app when installed
            f'<a class="read" href="{e(push["url"] or "#")}" data-read="{e(push["push_id"])}">Read</a>'
            f'</section>')


def profile_page(reader: dict, read: list[dict], owner: bool, share_url: str = "",
                 today: date | None = None, pinned: dict | None = None, caps: dict | None = None,
                 unread: list[dict] | None = None, days: list[dict] | None = None, inert: bool = False) -> str:
    """A reader's profile. `owner`: their own view (/me, or /p/<push_id> with that push's article
    `pinned` on top), with the articles pinned but not read yet (`unread`: tap one to read it),
    the delete buttons, the public toggle, pushes a day and always the list;
    otherwise the public view (/u/<slug>), where the count and list only show if they made them
    public. Article links go straight to NYT, so a visitor isn't counted as opening or reading.
    `days`: the pushes sent each day and which were read (Store.days), for the pictures; without
    it, each day with reads counts as finished. `inert`: the owner's view for the demo, whose buttons
    only change the page and never reach the server."""
    e = html.escape
    name = reader.get("name")
    public = bool(reader.get("history_public"))

    def item(a, delete=False):
        button = ""
        if delete:  # the owner's Pinned and Read lists: delete it from the profile
            ask = "Delete this article from your PushPin?"
            button = (f'<button class="del" data-del="{e(a["news_id"])}" data-confirm="{e(ask)}" '
                      f'aria-label="Delete">×</button>')
        read_tap = f' data-read="{e(a["push_id"])}"' if a.get("push_id") else ""  # a pinned one: tapping reads it
        day = a.get("day") or (a.get("read_at") or "")[:10]  # which day's picture it's in (the demo redraws by it)
        return (f'<li{f' data-day="{e(day)}"' if day else ""}><div><a href="{e(a["url"] or "#")}"{read_tap}>{e(a["title"] or "Untitled")}</a>'
                f'</div>{button}</li>')

    top = ""
    waiting = [a for a in (unread or []) if not pinned or a["news_id"] != pinned["news_id"]] if owner else []
    if waiting:  # only the owner sees what they pinned; the one on top isn't listed twice
        top += (f'<section><h2>Pinned <span class="n">{len(waiting)}</span></h2>'
                f'<p class="private">Always private: only you see what you’ve pinned. '
                f'Tap a headline to read it and it moves to Read.</p><ul class="list">'
                f'{"".join(item(a, True) for a in waiting)}</ul></section>')

    n = len(read)
    first = date.fromisoformat(min(a["read_at"] for a in read)[:10]) if read else None
    since = f'<p class="since">Started reading on {first:%B} {first.day}, {first.year}</p>' if first else ""
    run = streak(read, today or date.today())
    on_a_roll = f' <span class="streak">(Daily streak <span class="n">{run}</span>)</span>' if run else ""
    count = since if owner or public else ""  # the number itself is in the Read header below
    # Read n (Daily streak d), with the Papier-mâché switch on the right, then the list. A private
    # list on a public link shows only that header: no titles, so no switch and no dinosaurs.
    shown = owner or public  # Papier-mâché only uses titles the page already shows
    switch, hint, gallery, today_art = papier(read, days, today or date.today()) if shown else ("", "", "", "")
    head = f'<div class="readhead"><h2>Read <span class="n">{n}</span>{on_a_roll}</h2>{switch}</div>'
    # with the pictures available, the list is split: today's reads under Today's read, then Past reads
    on_day = (today or date.today()).isoformat()
    now = [a for a in read if (a.get("read_at") or "")[:10] == on_day] if today_art else []
    before = [a for a in read if a not in now]
    lists = (f'<ul class="list today-list">{"".join(item(a, owner) for a in now)}</ul>' if now else "")
    if today_art and before:
        lists += '<h3 class="past-head">Past reads</h3>'
    lists += f'<ul class="list">{"".join(item(a, owner) for a in before)}</ul>' if before or not today_art else ""
    articles = (f'<div class="readpanel">{head}{today_art}{hint}{lists}'
                f'{gallery}</div>' if read
                else '<h2>Read</h2><p class="empty">Nothing yet. Tap Read on a push to start.</p>')
    if owner:
        bottom = (f'{articles}<p class="note" aria-live="polite"></p>'
                  + volume_picker(reader.get("volume"), caps))  # always at the bottom
    elif public:
        bottom = articles
    else:
        bottom = f'<div class="readpanel">{head}</div>'

    whose = f"{e(name)}’s" if name else ("Your" if owner else "A reader’s")
    title = f'{whose} {PIN_MARK}'  # "Justin’s 📌", with our pin standing in for the word PushPin
    # the owner's header: who can see the page, and the switch that makes the reading public
    sub = (f'<p class="sub">Only you can see this page unless you make your reading public.</p>'
           f'<label class="toggle"><input type="checkbox" id="public"{" checked" if public else ""}> '
           f'Make my reading public</label><p class="share"{"" if public else " hidden"}>Anyone with this link '
           f'can see what you’ve read: <a href="{e(share_url)}">{e(share_url)}</a></p>') if owner else ""
    ask_name = ('<form class="who"><input name="name" maxlength="40" placeholder="Your first name (optional)" '
                'autocomplete="given-name"><button>Save</button></form>') if owner and not name else ""
    scripts = INERT_SCRIPT if inert else (OWNER_SCRIPT + PROFILE_SCRIPT) if owner else ""
    intro = ""
    if not owner:  # the public link opens with a short summary
        stats = [("Read", n), *([("Daily streak", run)] if run else [])] if public else []
        intro = splash(title, stats)
    if owner:  # the pinned push first
        intro = pin_panel(pinned) if pinned else ""
    body = (f'{intro}<div class="profile"><header><h1>{title}</h1>{count}{sub}{ask_name}</header>{top}'
            f'<section>{bottom}</section></div>{scripts}')
    return page(body).replace("<main>", '<main style="max-width:540px">', 1)


# Papier-mâché: the read titles as concrete poetry. Each day's pushes make one small picture (a common
# symbol or a dinosaur) out of the headlines read that day, filled in as they're read and finished
# when all of them are; tapping a finished one shares it as text.
PAPIER_SCRIPT = """<script>(() => {
const sw = document.getElementById('papier-on'), box = document.getElementById('papier');
const data = document.getElementById('papier-data');
let days = JSON.parse(data.textContent);
const panel = sw.closest('.readpanel'), lists = panel.querySelectorAll('.list, .past-head'), hint = panel.querySelector('.papier-hint');
let on = false, built = false;
const E = (c, x, y, rx, ry, r = 0) => { c.beginPath(); c.ellipse(x, y, rx, ry, r, 0, 7); c.fill(); };
const R = (c, x, y, w, h, r) => { c.beginPath(); c.roundRect(x, y, w, h, r); c.fill(); };
const P = (c, ...p) => { c.beginPath(); c.moveTo(p[0], p[1]); for (let i = 2; i < p.length; i += 2) c.lineTo(p[i], p[i + 1]); c.closePath(); c.fill(); };
const L = (c, w, ...p) => {  // a thick round stroke: a line, or a curve through a control point
  c.lineWidth = w; c.lineCap = c.lineJoin = 'round'; c.beginPath(); c.moveTo(p[0], p[1]);
  if (p.length === 6) c.quadraticCurveTo(p[2], p[3], p[4], p[5]); else c.lineTo(p[2], p[3]);
  c.stroke();
};
// ---- species ----
// Six body plans, each drawn facing right on a 200 x 130 grid with its feet on the ground, and
// knobs for the differences (neck, tail, bulk, horns, frills, plates, crests...). Each plan
// returns where the eye goes. With no knobs set, a plan is the original hand-drawn animal.
const PLANS = {
  sauropod(c, p) {
    const b = p.bulk ?? 1, n = p.neck ?? 1, t = p.tail ?? 1, hs = p.head ?? 1, up = p.upright ? 1 : 0;
    const by = 78;
    E(c, 100, by, 50 * b, 30 * b);
    const tip = Math.max(3, 60 - 58 * t);
    c.beginPath(); c.moveTo(62, by - 18); c.quadraticCurveTo(30, by - 12, tip, by + 28);
    c.quadraticCurveTo(36, by + 22, 70, by + 22); c.closePath(); c.fill();
    const hx = Math.min(166, 128 + 38 * Math.min(n, 1.1) * (up ? 0.55 : 1)), hy = Math.max(15, by - 54 * n * (up ? 1.15 : 1));
    L(c, 24 * Math.min(b, 1.05), 128, by - 6, (128 + hx) / 2 + 8 - up * 12, (by + hy) / 2 - 8, hx, hy + 3);
    E(c, hx + 6 * hs, hy, 20 * hs, 13 * hs, -0.2);
    if (p.spines) for (let i = 1; i <= 4; i++) {
      const f = i / 5, x = 128 + (hx - 128) * f, y = by - 6 + (hy + 3 - by + 6) * f;
      P(c, x - 5, y, x - 1, y - 18, x + 5, y);
    }
    if (p.club) E(c, tip + 6, by + 26, 10, 8);
    for (const x of [70, 88, 112, 130]) R(c, x, by + 14, 14, 32, 6);
    return [hx + 12 * hs, hy - 4 * hs];
  },
  theropod(c, p) {
    const b = p.bulk ?? 1, hs = p.head ?? 1, n = p.neck ?? 1, a = p.arm ?? 1, w = 0.6 + 0.4 * b;
    E(c, 92, 64, 46 * b, 26 * b, -0.3);
    P(c, 92 - 36 * b, 50 + (1 - b) * 8, 2 + (1 - b) * 24, 88, 92 - 28 * b, 80);   // tail
    if (p.sail) P(c, 92 - 34 * b, 52, 80, 52 - 44 * p.sail, 108, 48 - 42 * p.sail, 120 * b + 4, 50);
    const nx = 92 + 24 * b + 26 * n - 2, ny = 64 - 12 * b - 20 * n + 20 - 20;
    L(c, 26 * w, 92 + 24 * b, 64 - 12 * b, nx, ny);
    const hx = nx - 14 * hs, hy = ny - 22 * hs;
    if (p.snout) {  // spinosaurids: a long, low, crocodile-like head
      R(c, hx, hy + 8 * hs, 68 * hs, 18 * hs, 8 * hs);
    } else {
      R(c, hx, hy, 62 * hs, 30 * hs, 12 * hs);
      P(c, hx + 12 * hs, hy + 22 * hs, hx + 64 * hs, hy + 24 * hs, hx + 58 * hs, hy + 38 * hs, hx + 14 * hs, hy + 38 * hs);
    }
    if (p.horn) P(c, hx + 18 * hs, hy + 4, hx + 24 * hs, hy - 12 * hs * p.horn, hx + 32 * hs, hy + 4);
    if (p.crests) P(c, hx + 6 * hs, hy + 4, hx + 18 * hs, hy - 16 * hs * p.crests, hx + 44 * hs, hy + 4);
    R(c, 92 - 14 * b, 74, 22 * w, 50, 9 * w); R(c, 92 + 8 * b, 80, 18 * w, 44, 8 * w);   // legs
    const ax = 92 + 38 * b, ay = 64 - 4 * b;
    if (p.claws) {  // Therizinosaurus: long arms with long claws
      L(c, 8, ax, ay, ax + 16, ay + 30);
      for (const d of [-5, 0, 5]) L(c, 3, ax + 16 + d, ay + 30, ax + 24 + d * 1.5, ay + 30 + 16 * p.claws);
    } else if (a > 0) L(c, 7, ax, ay, ax + 12 * a, ay + 10 * a);
    return [hx + 46 * hs, hy + (p.snout ? 14 : 13) * hs];
  },
  stegosaur(c, p) {
    const ps = p.plates ?? 1, k = p.count ?? 5, long = p.longneck ? 1 : 0;
    E(c, 100, 82, 54, 28);
    const head = long ? [178, 62] : [180, 98];
    L(c, 16, 150, 86, long ? 168 : 176, long ? 66 : 98); E(c, head[0], head[1], 13, 9);
    L(c, 16, 50, 86, 30, 76, 8, 66); P(c, 2, 56, 14, 52, 16, 72); P(c, 8, 80, 14, 64, 22, 74);  // tail, spikes
    for (let i = 0; i < k; i++) {
      const x = 58 + (i * 92) / (k - 1), top = 82 - 28 * Math.sqrt(Math.max(0, 1 - ((x - 100) / 54) ** 2));
      const spike = p.spiky && i >= k / 2, half = spike ? 4 : 12 * Math.min(ps, 1.1);
      P(c, x - half, top + 10, x, top - 26 * ps * (spike ? 1.2 : 1), x + half, top + 10);
    }
    if (p.shoulder) P(c, 128, 84, 150, 62, 142, 92);
    for (const x of [66, 84, 116, 134]) R(c, x, 96, 15, 28, 6);
    return [head[0] + 4, head[1] - 4];
  },
  ankylosaur(c, p) {
    const bump = p.bumps ?? 1, club = p.club ?? 1;
    E(c, 98, 88, 62, 26); for (const x of [56, 76, 96, 116, 136]) E(c, x, 64, 10 * bump, 8 * bump);
    E(c, 172, 96, 19, 13); L(c, 10, 40, 94, 16, 92);
    if (club) E(c, 12, 92, 13, 11); else P(c, 2, 94, 20, 86, 20, 100);
    if (p.shoulder) { P(c, 142, 76, 166, 66 - 8 * p.shoulder, 156, 88); P(c, 124, 72, 140, 56 - 6 * p.shoulder, 140, 74); }
    for (const x of [58, 76, 112, 130]) R(c, x, 100, 15, 24, 6);
    return [184, 92];
  },
  ceratopsian(c, p) {
    const f = p.frill ?? 1, hb = p.brow ?? 1, hn = p.nose ?? 1, b = p.bulk ?? 1;
    E(c, 86, 80, 52 * b, 32 * b); P(c, 86 - 44 * b, 68, 2 + (1 - b) * 30, 92, 46, 96);
    E(c, 144, 60 - 4 * f, 26 * f, 32 * f, 0.35);
    if (p.spikes) for (const a of [3.5, 3.95, 4.4, 4.85]) {
      const x = 144 + 26 * f * Math.cos(a), y = 60 - 4 * f + 32 * f * Math.sin(a), ox = Math.cos(a), oy = Math.sin(a);
      P(c, x - oy * 5, y + ox * 5, x + ox * 18 * p.spikes, y + oy * 18 * p.spikes, x + oy * 5, y - ox * 5);
    }
    E(c, 128, 84, 20, 18);                                                // neck, so a small frill stays attached
    P(c, 144, 60, 196, 86, 182, 100, 138, 94);
    if (hb > 0) P(c, 158, 50, 163 + 35 * hb, 56 - 22 * hb, 168, 62);
    if (hn > 0) P(c, 180, 80, 185 + 9 * hn, 84 - 20 * hn, 190, 88);
    for (const x of [52, 70, 102, 120]) R(c, x, 98, 16, 26, 7);
    return [164, 64];
  },
  hadrosaur(c, p) {
    const b = p.bulk ?? 1, crest = p.crest ?? 'tube';
    E(c, 96, 74, 46 * b, 28 * b, -0.15); P(c, 96 - 40 * b, 62, 2 + (1 - b) * 24, 90, 62, 88 + (1 - b) * 4);
    if (p.sail) P(c, 70, 56, 84, 56 - 30 * p.sail, 118, 52 - 30 * p.sail, 126, 56);
    L(c, 18, 126, 62, 150, 36); E(c, 160, 31, 19, 11, 0.15);
    if (crest === 'tube') L(c, 11, 154, 24, 112, 6);
    if (crest === 'helmet') E(c, 156, 20, 13, 11);
    if (crest === 'hatchet') P(c, 148, 26, 136, 4, 160, 10, 164, 24);
    if (crest === 'spike') L(c, 7, 154, 24, 138, 10);
    if (crest === 'unicorn') L(c, 7, 162, 24, 168, 4);
    if (crest === 'bump') E(c, 162, 22, 9, 6);
    if (crest === 'dome') E(c, 158, 24, 17, 14);
    if (crest === 'spikes') { E(c, 158, 25, 15, 11); for (const x of [146, 156, 166]) P(c, x - 3, 18, x, 4, x + 3, 18); }
    R(c, 82, 86, 18, 38, 8); R(c, 102, 88, 16, 36, 8); L(c, 7, 134, 76, 146, 88);
    if (p.thumb) L(c, 4, 146, 88, 154, 80);
    return [167, 27];
  },
};
// Common symbols, on the same 200 x 130 grid. Those with eyes return them; the rest return null.
const SYMBOLS = {
  snowman(c) {
    E(c, 100, 103, 27, 25); E(c, 100, 63, 20, 19); E(c, 100, 30, 14, 14);
    R(c, 89, 4, 22, 14, 2); R(c, 82, 15, 36, 4, 2);                       // hat
    L(c, 4, 82, 60, 54, 44); L(c, 4, 118, 60, 146, 44);                    // arms
    return [[95, 28], [105, 28]];
  },
  heart(c) { E(c, 74, 46, 32, 30); E(c, 126, 46, 32, 30); P(c, 44, 58, 100, 40, 156, 58, 100, 124); return null; },
  star(c) {
    const pts = [];
    for (let i = 0; i < 10; i++) { const a = -Math.PI / 2 + i * Math.PI / 5, r = i % 2 ? 25 : 64; pts.push(100 + r * Math.cos(a), 70 + r * Math.sin(a)); }
    P(c, ...pts); return null;
  },
  sun(c) {
    E(c, 100, 65, 32, 32);
    for (let i = 0; i < 12; i++) { const a = i * Math.PI / 6; L(c, 7, 100 + 42 * Math.cos(a), 65 + 42 * Math.sin(a), 100 + 60 * Math.cos(a), 65 + 60 * Math.sin(a)); }
    return null;
  },
  moon(c) {
    E(c, 100, 65, 58, 58);
    c.globalCompositeOperation = 'destination-out'; E(c, 128, 48, 50, 50); c.globalCompositeOperation = 'source-over';
    return null;
  },
  tree(c) { P(c, 100, 2, 140, 46, 60, 46); P(c, 100, 26, 152, 80, 48, 80); P(c, 100, 56, 166, 114, 34, 114); R(c, 90, 110, 20, 18, 2); return null; },
  umbrella(c) {
    c.beginPath(); c.arc(100, 66, 72, Math.PI, 0); c.closePath(); c.fill();
    L(c, 7, 100, 62, 100, 112); c.lineWidth = 7; c.beginPath(); c.arc(88, 112, 12, 0, Math.PI); c.stroke();
    return null;
  },
  house(c, p) {
    R(c, 130, 14, 16, 34, 1); P(c, 100, 4, 172, 62, 28, 62); R(c, 44, 58, 112, 68, 2);
    if (p.solid) return null;  // the sticker's die-cut: the whole house, door and windows on white
    c.globalCompositeOperation = 'destination-out'; R(c, 90, 90, 22, 36, 2); R(c, 56, 72, 22, 20, 2); R(c, 124, 72, 22, 20, 2);
    c.globalCompositeOperation = 'source-over'; return null;
  },
  cat(c) {
    E(c, 92, 92, 34, 32); E(c, 100, 48, 24, 21);
    P(c, 79, 38, 81, 12, 97, 30); P(c, 103, 30, 119, 12, 121, 38);       // ears
    L(c, 8, 122, 112, 160, 110, 152, 74);                                 // tail
    E(c, 80, 122, 13, 7); E(c, 108, 122, 13, 7);
    return [[92, 46], [108, 46]];
  },
  fish(c) { E(c, 90, 66, 60, 32); P(c, 138, 66, 188, 28, 188, 104); P(c, 76, 38, 98, 16, 114, 40); return [62, 58]; },
  apple(c) { E(c, 84, 78, 40, 44); E(c, 116, 78, 40, 44); L(c, 5, 100, 38, 106, 14); E(c, 124, 20, 17, 7, -0.5); return null; },
  cloud(c) { E(c, 68, 82, 34, 28); E(c, 104, 62, 40, 36); E(c, 140, 80, 34, 30); R(c, 36, 80, 136, 32, 16); return null; },
  bolt(c) { P(c, 120, 2, 50, 74, 94, 74, 70, 128, 152, 50, 106, 50, 134, 2); return null; },
  cup(c) {
    R(c, 50, 50, 82, 66, 14); R(c, 34, 114, 118, 9, 4);
    c.lineWidth = 11; c.beginPath(); c.arc(132, 80, 17, -Math.PI / 2, Math.PI / 2); c.stroke();
    L(c, 5, 76, 42, 88, 28, 78, 10); L(c, 5, 104, 42, 116, 28, 106, 10);  // steam
    return null;
  },
  flower(c) {
    for (let i = 0; i < 6; i++) { const a = i * Math.PI / 3; E(c, 100 + 24 * Math.cos(a), 44 + 24 * Math.sin(a), 17, 12, a); }
    L(c, 6, 100, 64, 100, 128); E(c, 118, 100, 17, 7, -0.5); E(c, 82, 112, 15, 6, 0.5);
    return null;
  },
  note(c) { E(c, 72, 106, 24, 17, -0.35); L(c, 8, 92, 102, 92, 10); L(c, 8, 92, 12, 134, 38, 126, 76); return null; },
  balloon(c) { E(c, 100, 52, 40, 48); P(c, 93, 98, 107, 98, 100, 108); L(c, 3, 100, 106, 86, 118, 104, 128); return null; },
};
const SYMBOL_NAMES = {snowman: 'Snowman', heart: 'Heart', star: 'Star', sun: 'Sun', moon: 'Moon', tree: 'Pine tree',
  umbrella: 'Umbrella', house: 'House', cat: 'Cat', fish: 'Fish', apple: 'Apple', cloud: 'Cloud', bolt: 'Lightning bolt',
  cup: 'Coffee cup', flower: 'Flower', note: 'Music note', balloon: 'Balloon'};
Object.assign(PLANS, SYMBOLS);
const SPECIES = {  // name: [plan, knobs], by family; the first of the first six families open the order
  sauropod: [['Brontosaurus', {}], ['Apatosaurus', {bulk: 1.05, neck: 0.95}], ['Diplodocus', {bulk: 0.9, neck: 1.1, head: 0.85}],
    ['Brachiosaurus', {upright: 1, neck: 1.2, tail: 0.6}], ['Camarasaurus', {neck: 0.8, tail: 0.7, head: 1.1}],
    ['Argentinosaurus', {bulk: 1.1, neck: 1.05}], ['Mamenchisaurus', {neck: 1.3, head: 0.8}], ['Supersaurus', {neck: 1.2, bulk: 0.95}],
    ['Giraffatitan', {upright: 1, neck: 1.3, tail: 0.55, head: 0.9}], ['Patagotitan', {bulk: 1.1, neck: 1.1}],
    ['Dreadnoughtus', {bulk: 1.12, neck: 0.9}], ['Saltasaurus', {bulk: 0.9, neck: 0.75, tail: 0.8}],
    ['Amargasaurus', {neck: 0.85, spines: 1}], ['Nigersaurus', {neck: 0.7, head: 1.15, tail: 0.8}],
    ['Barosaurus', {neck: 1.3, tail: 0.9}], ['Shunosaurus', {neck: 0.8, club: 1}], ['Plateosaurus', {neck: 0.75, tail: 0.9, bulk: 0.8, head: 0.9}]],
  theropod: [['T. rex', {}], ['Allosaurus', {head: 0.85, horn: 0.7, arm: 1.1}], ['Giganotosaurus', {head: 0.95, bulk: 1.05, arm: 0.8}],
    ['Carcharodontosaurus', {head: 0.95, arm: 0.8}], ['Albertosaurus', {head: 0.85, bulk: 0.9}], ['Gorgosaurus', {head: 0.85, bulk: 0.88}],
    ['Daspletosaurus', {head: 0.95}], ['Tarbosaurus', {head: 0.92}], ['Ceratosaurus', {head: 0.85, horn: 1, arm: 1}],
    ['Carnotaurus', {head: 0.7, horn: 1.2, arm: 0}], ['Majungasaurus', {head: 0.75, horn: 0.6, arm: 0}],
    ['Torvosaurus', {head: 0.9, arm: 1}], ['Megalosaurus', {head: 0.85, arm: 1.1}], ['Acrocanthosaurus', {head: 0.85, sail: 0.35, arm: 1}],
    ['Yutyrannus', {head: 0.8, arm: 1.1, bulk: 0.95}], ['Spinosaurus', {snout: 1, sail: 1, arm: 1.2}],
    ['Baryonyx', {snout: 1, head: 0.9, arm: 1.3}], ['Suchomimus', {snout: 1, sail: 0.3, arm: 1.2}],
    ['Irritator', {snout: 1, head: 0.8, bulk: 0.85, arm: 1}], ['Velociraptor', {bulk: 0.7, head: 0.6, neck: 1.2, arm: 1.4}],
    ['Deinonychus', {bulk: 0.75, head: 0.65, neck: 1.15, arm: 1.4}], ['Utahraptor', {bulk: 0.85, head: 0.7, neck: 1.1, arm: 1.3}],
    ['Microraptor', {bulk: 0.6, head: 0.5, neck: 1.2, arm: 1.8}], ['Troodon', {bulk: 0.65, head: 0.55, neck: 1.3, arm: 1.3}],
    ['Compsognathus', {bulk: 0.55, head: 0.5, neck: 1.3, arm: 1}], ['Coelophysis', {bulk: 0.6, head: 0.55, neck: 1.45, arm: 1.2}],
    ['Dilophosaurus', {head: 0.75, crests: 1, arm: 1.1, bulk: 0.85}], ['Oviraptor', {bulk: 0.7, head: 0.6, neck: 1.35, crests: 0.8, arm: 1.4}],
    ['Gallimimus', {bulk: 0.75, head: 0.45, neck: 1.6, arm: 1.2}], ['Struthiomimus', {bulk: 0.72, head: 0.45, neck: 1.55, arm: 1.3}],
    ['Ornithomimus', {bulk: 0.7, head: 0.45, neck: 1.5, arm: 1.2}], ['Therizinosaurus', {bulk: 1, head: 0.45, neck: 1.5, claws: 1}],
    ['Deinocheirus', {bulk: 1.05, head: 0.55, neck: 1.4, claws: 0.7, sail: 0.3}]],
  stegosaur: [['Stegosaurus', {}], ['Kentrosaurus', {plates: 0.75, spiky: 1}], ['Tuojiangosaurus', {plates: 0.85, count: 7}],
    ['Miragaia', {longneck: 1, plates: 0.7}], ['Huayangosaurus', {plates: 0.6, spiky: 1, count: 6}],
    ['Gigantspinosaurus', {plates: 0.5, shoulder: 1}]],
  ceratopsian: [['Triceratops', {}], ['Styracosaurus', {brow: 0, nose: 1.6, spikes: 1}], ['Centrosaurus', {brow: 0.3, nose: 1.3, frill: 0.85}],
    ['Chasmosaurus', {frill: 1.3, brow: 0.7, nose: 0.6}], ['Pentaceratops', {frill: 1.25, brow: 1.1}], ['Torosaurus', {frill: 1.3}],
    ['Protoceratops', {frill: 0.8, brow: 0, nose: 0, bulk: 0.85}], ['Psittacosaurus', {frill: 0.45, brow: 0, nose: 0, bulk: 0.8}],
    ['Pachyrhinosaurus', {brow: 0, nose: 0.4, frill: 0.9, spikes: 0.6}], ['Einiosaurus', {brow: 0, nose: 1.2, spikes: 0.7}],
    ['Kosmoceratops', {brow: 0.9, nose: 0.6, spikes: 1, frill: 1.05}], ['Zuniceratops', {brow: 1.1, nose: 0, frill: 0.75}],
    ['Diabloceratops', {brow: 0.6, nose: 0.8, spikes: 0.8}]],
  hadrosaur: [['Parasaurolophus', {}], ['Corythosaurus', {crest: 'helmet'}], ['Lambeosaurus', {crest: 'hatchet'}],
    ['Edmontosaurus', {crest: 'none'}], ['Maiasaura', {crest: 'bump'}], ['Hadrosaurus', {crest: 'none', bulk: 0.95}],
    ['Saurolophus', {crest: 'spike'}], ['Olorotitan', {crest: 'hatchet', bulk: 1.05}], ['Shantungosaurus', {crest: 'none', bulk: 1.1}],
    ['Tsintaosaurus', {crest: 'unicorn'}], ['Hypacrosaurus', {crest: 'helmet', sail: 0.3}], ['Iguanodon', {crest: 'none', thumb: 1}],
    ['Hypsilophodon', {crest: 'none', bulk: 0.75}], ['Tenontosaurus', {crest: 'none', bulk: 0.9}],
    ['Ouranosaurus', {crest: 'none', sail: 1, thumb: 1}], ['Muttaburrasaurus', {crest: 'bump', thumb: 1}],
    ['Heterodontosaurus', {crest: 'none', bulk: 0.7}], ['Leaellynasaura', {crest: 'none', bulk: 0.7}],
    ['Pachycephalosaurus', {crest: 'dome'}], ['Stegoceras', {crest: 'dome', bulk: 0.8}], ['Dracorex', {crest: 'spikes'}],
    ['Stygimoloch', {crest: 'spikes', bulk: 0.85}], ['Prenocephale', {crest: 'dome', bulk: 0.85}]],
  ankylosaur: [['Ankylosaurus', {}], ['Euoplocephalus', {bumps: 1.1}], ['Nodosaurus', {club: 0, bumps: 0.8}],
    ['Sauropelta', {club: 0, shoulder: 1}], ['Edmontonia', {club: 0, shoulder: 1.2}], ['Borealopelta', {club: 0, bumps: 1.2, shoulder: 0.8}],
    ['Scelidosaurus', {club: 0, bumps: 0.7}], ['Pinacosaurus', {bumps: 0.9}]],
};
// the order they're made in: a symbol, then a dinosaur, taking turns (the snowman first; the
// dinosaur families take turns among themselves)
const KINDS = (() => {
  const fams = ['sauropod', 'theropod', 'stegosaur', 'ceratopsian', 'hadrosaur', 'ankylosaur'];
  const queues = fams.map(f => SPECIES[f].map(([name, knobs]) => ({name, plan: f, knobs})));
  const dinos = [];
  while (queues.some(q => q.length)) for (const q of queues) if (q.length) dinos.push(q.shift());
  const symbols = Object.keys(SYMBOLS).map(k => ({name: SYMBOL_NAMES[k], plan: k, knobs: {}}));
  const out = [];
  for (let i = 0; i < Math.max(symbols.length, dinos.length); i++) {
    if (i < symbols.length) out.push(symbols[i]);
    if (i < dinos.length) out.push(dinos[i]);
  }
  return out;
})();
// The same picture for everyone on a given day: the date picks the shape, from one fixed order
// that starts with the snowman and a Brontosaurus on Oct 12, 2026, then the rest shuffled once, so
// every shape comes up before any repeats. What differs between readers is the words.
function rng(str) {  // a small seeded generator (string hash + mulberry32): the same seed, the same numbers
  let h = 1779033703 ^ str.length;
  for (let i = 0; i < str.length; i++) { h = Math.imul(h ^ str.charCodeAt(i), 3432918353); h = h << 13 | h >>> 19; }
  return () => {
    h = h + 0x6D2B79F5 | 0; let t = Math.imul(h ^ h >>> 15, 1 | h);
    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296;
  };
}
const ORDER = (() => {
  const first = ['Snowman', 'Brontosaurus'].map(n => KINDS.find(k => k.name === n));
  const rest = KINDS.filter(k => !first.includes(k)), r = rng('pushpin:order');
  for (let i = rest.length - 1; i > 0; i--) { const j = Math.floor(r() * (i + 1)); [rest[i], rest[j]] = [rest[j], rest[i]]; }
  return [...first, ...rest];
})();
const EPOCH = Date.UTC(2026, 9, 12);
const kindFor = day => {  // day: YYYY-MM-DD
  const n = Math.round((Date.UTC(+day.slice(0, 4), +day.slice(5, 7) - 1, +day.slice(8, 10)) - EPOCH) / 864e5);
  return ORDER[((n % ORDER.length) + ORDER.length) % ORDER.length];
};
// ---- end species ----
function dino(c, w, h, flip, kind, solid = false) {  // solid: no holes (eyes, door, windows), for a die-cut
  c.save();
  const s = Math.min(w / 200, h / 130);
  c.translate((w - 200 * s) / 2, (h - 130 * s) / 2);
  if (flip) { c.translate(200 * s, 0); c.scale(-1, 1); }
  c.scale(s, s);
  c.fillStyle = c.strokeStyle = '#000';
  const eye = solid ? (PLANS[kind.plan](c, {...kind.knobs, solid}), null) : PLANS[kind.plan](c, kind.knobs);  // [x, y], a list, or null
  c.globalCompositeOperation = 'destination-out';
  for (const [x, y] of eye ? (Array.isArray(eye[0]) ? eye : [eye]) : []) { c.beginPath(); c.arc(x, y, 2.8, 0, 7); c.fill(); }
  c.restore();
}
// Lays the words out inside the dinosaur's silhouette, w x h: [char, x, y] for each glyph.
// Unfinished: only part of the shape is revealed, from one side, as far as the day's pushes have
// been read. Returns the test for the revealed part and where it ends (x), or null when it's whole.
function revealed(shape, W, H, built, flip) {
  if (built >= 1) return {inside: shape, edge: null};
  let x0 = W, x1 = 0;
  for (let y = 0; y < H; y += 2) for (let x = 0; x < W; x++) if (shape(x, y)) { x0 = Math.min(x0, x); x1 = Math.max(x1, x); }
  const edge = flip ? x1 - (x1 - x0) * built : x0 + (x1 - x0) * built;
  return {inside: (x, y) => shape(x, y) && (flip ? x >= edge : x <= edge), edge};
}
// Where the shape is, w x h: a test for whether a pixel is inside it.
function silhouette(w, h, flip, kind) {
  const m = document.createElement('canvas'); m.width = w; m.height = h;
  const mc = m.getContext('2d'); dino(mc, w, h, flip, kind);
  const mask = mc.getImageData(0, 0, w, h).data;
  return (x, y) => mask[(y * w + x) * 4 + 3] > 127;
}
// Lays the text out once inside the shape, w x h, in type of size fs with lines `spread` times the
// usual spacing: [char, x, y] for each glyph, and whether
// all of the text fit. Nothing repeats; what doesn't fit is left out. Thin parts still get letters:
// a stretch narrower than a letter (but at least half as wide) takes one, centered, and a line
// thinner than the row height counts if it's near the row.
function layout(w, h, text, inside, fs, spread = 1, track = 0) {
  const lh = fs * 1.1 * spread, mc = document.createElement('canvas').getContext('2d');
  mc.font = fs + 'px Georgia, "Times New Roman", serif';
  const widths = {}, width = ch => widths[ch] ??= mc.measureText(ch).width + track;
  const near = Math.round(fs * 0.4), glyphs = []; let i = 0;
  for (let row = lh / 2; row < h && i < text.length; row += lh) {
    const y = Math.round(row), ya = Math.max(0, y - near), yb = Math.min(h - 1, y + near);
    const at = x => inside(x, y) || inside(x, ya) || inside(x, yb);
    let x = 0;
    while (x < w && i < text.length) {
      while (x < w && !at(x)) x++;
      let end = x; while (end < w && at(end)) end++;
      let cx = x;
      while (i < text.length && text[i] === ' ') i++;  // a stretch starts with a letter
      if (i < text.length && end - x < width(text[i]) && end - x >= width(text[i]) / 2) {
        glyphs.push([text[i], x + (end - x - width(text[i])) / 2, y]); i++;  // a thin part: one letter
      } else while (i < text.length) {
        const ch = text[i];
        if (cx + width(ch) > end) break;
        glyphs.push([ch, cx, y]); cx += width(ch); i++;
      }
      x = end + 1;
    }
  }
  return {glyphs, fit: i >= text.length};
}
// The largest type at which all of the text fits in the shape once: the word art is scaled to
// its text, so the text never runs out early or runs over. Then the slack is spread
// out, so the text reaches the bottom (where handles, stems and legs often are) instead of
// stopping short: the lines are spaced out, leaving only the last line a little unfinished.
function fitted(w, h, text, inside, top) {
  let fs = top, l = null;
  for (; fs > 2; fs *= 0.97) { l = layout(w, h, text, inside, fs); if (l.fit) break; }
  if (!l || !l.fit) return {...layout(w, h, text, inside, 2), fs: 2};
  let big = fs / 0.97;  // then the exact size between that and the step above, where it only just fits
  for (let k = 0; k < 8 && fs < top; k++) { const m = (fs + big) / 2; if (layout(w, h, text, inside, m).fit) fs = m; else big = m; }
  let lo = 1, hi = 1.6;  // line spacing: the most that still fits
  for (let k = 0; k < 8; k++) { const m = (lo + hi) / 2; if (layout(w, h, text, inside, fs, m).fit) lo = m; else hi = m; }
  // then a touch more room between letters (a few percent at most), so the words reach the very end
  // of the shape, a tree's trunk or a snowman's base, and only the last line ends short
  let a = 0, b = fs * 0.25;
  for (let k = 0; k < 10; k++) { const m = (a + b) / 2; if (layout(w, h, text, inside, fs, lo, m).fit) a = m; else b = m; }
  return {...layout(w, h, text, inside, fs, lo, a), fs};
}
function draw(canvas, text, built, flip, animate, kind, delay, past) {
  const dpr = window.devicePixelRatio || 1; let w = Math.round(canvas.clientWidth), h = Math.round(w * 0.64);
  canvas.style.height = h + 'px'; canvas.width = w * dpr; canvas.height = h * dpr;
  const c = canvas.getContext('2d');
  // laid out in device pixels, so the type can be smaller than a CSS pixel allows: the shape reads
  // at a glance, the headlines up close
  const W = w * dpr, H = h * dpr, shape = silhouette(W, H, flip, kind);
  [w, h] = [W, H];
  const css = getComputedStyle(document.documentElement), ink = css.getPropertyValue('--fg').trim() || '#121212';
  // a day that's over but not finished: the grey outline of what it could have been, under
  // whatever was read (today's stays hidden, so it's a guess until it's done)
  let ghost = null;
  if (past && built < 1) {
    ghost = document.createElement('canvas'); ghost.width = W; ghost.height = H;
    const gc = ghost.getContext('2d'); dino(gc, W, H, flip, kind);
    gc.globalCompositeOperation = 'source-in'; gc.fillStyle = ink; gc.fillRect(0, 0, W, H);
  }
  if (built <= 0 && ghost) { c.globalAlpha = 0.15; c.drawImage(ghost, 0, 0); c.globalAlpha = 1; return; }
  if (built <= 0) {  // today, nothing read yet: a nudge in its place, no hint of the shape
    const size = Math.max(9 * dpr, Math.round(H * 0.12)), lines = [];
    c.font = size + 'px -apple-system, system-ui, sans-serif'; c.textAlign = 'center'; c.textBaseline = 'middle';
    c.fillStyle = css.getPropertyValue('--muted').trim() || '#666';
    for (const word of 'Start reading to make today’s word art'.split(' ')) {  // wrapped to the cell
      const line = lines.length ? lines[lines.length - 1] + ' ' + word : word;
      if (lines.length && c.measureText(line).width <= W * 0.9) lines[lines.length - 1] = line; else lines.push(word);
    }
    lines.forEach((line, k) => c.fillText(line, W / 2, H / 2 + (k - (lines.length - 1) / 2) * size * 1.25));
    return;
  }
  // Unfinished: only part of the shape is revealed (see revealed), and the read text fills just that part.
  const inside = revealed(shape, W, H, built, flip).inside;
  const {glyphs, fs} = fitted(W, H, text, inside, Math.round(W / 4));
  c.font = fs + 'px Georgia, "Times New Roman", serif'; c.textBaseline = 'middle'; c.fillStyle = ink;
  const start = performance.now() + delay, sweep = animate ? 600 : 0, fade = animate ? 240 : 0;
  const frame = now => {
    c.clearRect(0, 0, w, h);
    let busy = false;
    if (ghost) { c.globalAlpha = 0.15; c.drawImage(ghost, 0, 0); }
    for (const g of glyphs) {
      const along = flip ? 1 - g[1] / w : g[1] / w;
      const t = fade ? Math.min(1, Math.max(0, (now - start - along * sweep - (g[2] / h) * 120) / fade)) : 1;
      if (t < 1) busy = true;
      if (t <= 0) continue;
      c.globalAlpha = t * t * (3 - 2 * t);  // smoothstep
      c.fillText(g[0], g[1], g[2]);
    }
    c.globalAlpha = 1;
    if (busy) requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
}
// ---- share card ----
// The sticker: the word art as an image, so it looks the same in any chat. The day's words in
// serif type on a white die-cut of the shape (the outline grown by a border, like a printed
// sticker), with a soft shadow, the PushPin pin in the top left corner and a transparent
// background, as a PNG. In Photos on iOS 17+, long-press it and Add Sticker to use it in Messages.
const STICKER_INK = '#1a1714';
let pinImage = null;
const pinIcon = () => pinImage ??= new Promise(ok => {
  const im = new Image(); im.onload = () => ok(im); im.onerror = () => ok(null); im.src = '/icon.png';
});
// Squared distance from every pixel to the nearest pixel of the shape (alpha over half), w x h: the
// exact Euclidean distance transform, one dimension at a time (Felzenszwalb and Huttenlocher).
function distances(rgba, w, h) {
  const INF = 1e20, d = new Float64Array(w * h);
  for (let i = 0; i < w * h; i++) d[i] = rgba[4 * i + 3] > 127 ? 0 : INF;
  const n = Math.max(w, h), f = new Float64Array(n), out = new Float64Array(n), v = new Int32Array(n), z = new Float64Array(n + 1);
  const pass = len => {  // the lower envelope of parabolas over f[0..len)
    let k = 0; v[0] = 0; z[0] = -INF; z[1] = INF;
    for (let q = 1; q < len; q++) {
      let s = ((f[q] + q * q) - (f[v[k]] + v[k] * v[k])) / (2 * q - 2 * v[k]);
      while (s <= z[k]) { k--; s = ((f[q] + q * q) - (f[v[k]] + v[k] * v[k])) / (2 * q - 2 * v[k]); }
      k++; v[k] = q; z[k] = s; z[k + 1] = INF;
    }
    k = 0;
    for (let q = 0; q < len; q++) { while (z[k + 1] < q) k++; out[q] = (q - v[k]) * (q - v[k]) + f[v[k]]; }
  };
  for (let x = 0; x < w; x++) { for (let y = 0; y < h; y++) f[y] = d[y * w + x]; pass(h); for (let y = 0; y < h; y++) d[y * w + x] = out[y]; }
  for (let y = 0; y < h; y++) { for (let x = 0; x < w; x++) f[x] = d[y * w + x]; pass(w); for (let x = 0; x < w; x++) d[y * w + x] = out[x]; }
  return d;
}
// An unfinished day: today, just the part revealed so far; a day that's over, the whole shape's
// die-cut, blank white where it was missed and the words in the part that was read (a missed day:
// just the white die-cut).
async function sticker(day, kind, built = 1, past = false) {
  const W = 1200, H = 768, whole = silhouette(W, H, day.flip, kind), missed = past && built < 1;
  const {inside: shape, edge} = revealed(whole, W, H, built, day.flip), outline = missed ? whole : shape;
  const {glyphs, fs} = built > 0 ? fitted(W, H, day.text, shape, Math.round(W / 4)) : {glyphs: [], fs: 10};
  let x0 = W, x1 = 0, y0 = H, y1 = 0;
  for (let y = 0; y < H; y += 2) for (let x = 0; x < W; x += 2) if (outline(x, y)) {
    x0 = Math.min(x0, x); x1 = Math.max(x1, x); y0 = Math.min(y0, y); y1 = Math.max(y1, y);
  }
  // border, room for the shadow, and the pin's own corner (top left): the die-cut's edge always
  // starts a clear GAP beyond the pin, whatever the shape
  const B = 26, P = B + 34, PIN = 72, AT = 6, GAP = 20, room = AT + PIN + GAP - (P - B), ox = P + room - x0, oy = P + room - y0;
  const out = document.createElement('canvas'); out.width = x1 - x0 + 2 * P + room; out.height = y1 - y0 + 2 * P + room;
  const mask = document.createElement('canvas'); mask.width = W; mask.height = H;
  const mc = mask.getContext('2d'); dino(mc, W, H, day.flip, kind, true);  // traced around the solid shape
  if (edge !== null && !missed) day.flip ? mc.clearRect(0, 0, edge, H) : mc.clearRect(edge, 0, W - edge, H);
  const cut = document.createElement('canvas'); cut.width = out.width; cut.height = out.height;
  const cc = cut.getContext('2d');
  // the border: every pixel within B of the shape, by true distance, with a soft edge, so thin
  // tips get a smooth, even border instead of the ridges of stamped copies
  cc.drawImage(mask, ox, oy);
  const px = cc.getImageData(0, 0, cut.width, cut.height), d = distances(px.data, cut.width, cut.height);
  for (let i = 0; i < d.length; i++) {
    const a = Math.max(0, Math.min(1, B + 0.5 - Math.sqrt(d[i])));
    px.data[4 * i] = px.data[4 * i + 1] = px.data[4 * i + 2] = 255; px.data[4 * i + 3] = Math.round(255 * a);
  }
  cc.putImageData(px, 0, 0);
  const c = out.getContext('2d');
  c.shadowColor = 'rgba(0,0,0,.28)'; c.shadowBlur = 24; c.shadowOffsetY = 8; c.drawImage(cut, 0, 0);
  c.shadowColor = 'transparent';
  c.font = fs + 'px Georgia, "Times New Roman", serif'; c.textBaseline = 'middle'; c.fillStyle = STICKER_INK;
  for (const g of glyphs) c.fillText(g[0], ox + g[1], oy + g[2]);
  const im = await pinIcon();
  if (im) c.drawImage(im, AT, AT, PIN, PIN);
  return out;
}
let sheet = null;
function share(day, kind, built = 1, past = false) {
  if (!sheet) {
    sheet = document.createElement('dialog'); sheet.className = 'sharecard';
    sheet.innerHTML = '<button type="button" class="x" aria-label="Close">×</button><img alt="">'
      + '<div class="row"><button type="button" class="go">Share</button><button type="button" class="copy">Copy</button>'
      + '<a class="save" download>Save</a></div>';
    sheet.querySelector('.x').addEventListener('click', () => sheet.close());
    sheet.addEventListener('click', ev => { if (ev.target === sheet) sheet.close(); });  // the backdrop
    document.body.append(sheet);
  }
  const img = sheet.querySelector('img'), go = sheet.querySelector('.go'), save = sheet.querySelector('.save'), copy = sheet.querySelector('.copy');
  img.removeAttribute('src'); img.alt = (built >= 1 ? 'A ' + kind.name + ' sticker made of the day’s articles' : past ? 'A ' + kind.name + ' sticker, blank where the day’s pushes went unread' : 'Part of a sticker made of the day’s articles');
  go.hidden = copy.hidden = true; save.removeAttribute('href'); copy.textContent = 'Copy';
  sheet.showModal();
  sticker(day, kind, built, past).then(cv => {
    const url = cv.toDataURL('image/png'), name = 'pushpin-' + day.seed.split(':').pop() + '-' + kind.name.toLowerCase().replace(/[^a-z]+/g, '-') + '.png';
    img.src = url; save.href = url; save.download = name;
    cv.toBlob(b => {  // made before the tap on Share, which iOS needs to share a file
      const payload = {files: [new File([b], name, {type: 'image/png'})]};
      go.hidden = !(navigator.canShare && navigator.canShare(payload));
      go.onclick = () => navigator.share(payload).catch(() => null);
      // a web page can't save to Photos itself: on a phone, Save opens the share sheet, whose
      // Save Image puts it there (a download would land in Files); elsewhere it downloads
      save.onclick = ev => {
        if (!(matchMedia('(pointer: coarse)').matches && navigator.canShare && navigator.canShare(payload))) return;
        ev.preventDefault(); navigator.share(payload).catch(() => null);
      };
      // Copy puts the image itself on the clipboard, to paste straight into a message
      copy.hidden = !(navigator.clipboard && navigator.clipboard.write && window.ClipboardItem);
      copy.onclick = () => navigator.clipboard.write([new ClipboardItem({'image/png': b})])
        .then(() => { copy.textContent = 'Copied'; }, () => { copy.textContent = 'Couldn’t copy'; });
    }, 'image/png');
  });
}
// ---- end share card ----
// A day's text: every read title, each followed by its abstract, and which way the shape faces.
// The randomness (the facing here, which abstract words the shared text keeps) is seeded by the
// reader and the day, so a picture is the same each time it's drawn and differs between days and
// between readers who read the same articles the same day.
function dayText(d) {
  const seed = 'pushpin:' + d.day, r = rng(seed);  // the same for everyone that day
  const flip = r() < 0.5;
  const parts = d.titles.map((title, i) => ({title, words: ((d.abstracts || [])[i] || '').split(/\\s+/).filter(Boolean)}));
  const last = parts[parts.length - 1];  // it ends with a period
  if (last && last.words.length) { if (!/[.!?]["”’]?$/.test(last.words[last.words.length - 1])) last.words[last.words.length - 1] += '.'; }
  else if (last && !/[.!?]["”’]?$/.test(last.title)) last.title += '.';
  // the picture on the profile: every title with its whole abstract (the shared text drops words to fit)
  const text = parts.length ? parts.map(p => p.words.length ? p.title + ' — ' + p.words.join(' ') : p.title).join(' ') : '';
  return {text, flip, parts, seed};
}
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
// One day's picture: a figure with the canvas (tap to share it) and, in the grid, its date caption.
function picture(d, k, animate, caption = true) {
  const kind = kindFor(d.day), day = dayText(d), done = d.pinned > 0 && d.read >= d.pinned, built = d.pinned ? Math.min(1, d.read / d.pinned) : 0;
  const past = d.day < (data.dataset.today || '');
  const when = MONTHS[+d.day.slice(5, 7) - 1] + ' ' + +d.day.slice(8, 10);
  const fig = document.createElement('figure'), canvas = document.createElement('canvas');
  canvas.setAttribute('role', 'img');
  canvas.setAttribute('aria-label', 'A ' + kind.name + ' for ' + when + ', ' + d.read + ' of ' + d.pinned + ' pinned pushes read'
                      + (d.titles.length ? ': ' + d.titles.join('; ') : ''));
  if (built > 0 || past) {  // tap to share it (unfinished: today, the part revealed; a past day, the whole die-cut, blank where missed)
    const tap = document.createElement('button'), named = done || past;
    tap.type = 'button'; tap.className = 'dino'; tap.title = (named ? kind.name + ' · ' : '') + when;
    tap.setAttribute('aria-label', 'Share the ' + (named ? kind.name : 'picture') + ' for ' + when);
    tap.append(canvas); tap.addEventListener('click', () => share(day, kind, built, past));
    fig.append(tap);
  } else {
    const still = document.createElement('div');  // today, nothing read: nothing to share yet
    still.className = 'dino still'; still.title = 'Read ' + when + '’s pushes to make its word art';
    still.append(canvas); fig.append(still);
  }
  if (caption) {
    const cap = document.createElement('figcaption');
    cap.textContent = when + (done ? '' : ' · ' + d.read + '/' + d.pinned);
    fig.append(cap);
  }
  return [fig, () => draw(canvas, day.text, built, day.flip, animate, kind, animate ? Math.min(k, 40) * 45 : 0, past)];
}
const TODAY = data.dataset.today || '';
function build(animate) {  // the grid: every day but today, which has its own place on top
  box.innerHTML = '';
  days.filter(d => d.day !== TODAY).forEach((d, k) => {
    const [fig, paint] = picture(d, k, animate);
    box.append(fig); paint();
  });
}
// Today's read, on top of the panel in both modes (the list and the pictures): today's word art so far.
const top = document.getElementById('papier-today');
function today() {
  if (!top) return;
  const d = days.find(x => x.day === TODAY) || {day: TODAY, pinned: 0, read: 0, titles: [], abstracts: []};
  top.innerHTML = '';
  const head = document.createElement('h3');
  head.textContent = 'Today’s read' + (d.pinned ? ' · ' + d.read + ' of ' + d.pinned + ' pinned today' : '');  // older pins count toward their own days
  const [fig, paint] = picture(d, 0, false, false);
  top.append(head, fig); paint();
}
today();
function show(animate) {
  if (!on || built) return;
  built = true; build(animate);
  if (animate) requestAnimationFrame(() => box.classList.add('on'));  // fade in
  else box.classList.add('on');  // restored on load: just there
}
function set(value, animate) {
  on = value; built = false;
  sw.setAttribute('aria-pressed', on);
  box.hidden = hint.hidden = !on; lists.forEach(x => { x.hidden = on; });
  box.classList.remove('on');
  show(animate);
  try { localStorage.setItem('papier', on ? '1' : '0'); } catch (e) {}
}
sw.addEventListener('click', () => set(!on, true));
let off = false; try { off = localStorage.getItem('papier') === '0'; } catch (e) {}
if (!off) set(true, false);  // on unless this reader turned it off
let t; window.addEventListener('resize', () => { clearTimeout(t); t = setTimeout(() => { today(); if (on) build(false); }, 150); });
// the days changed on the page (the demo's ×): redraw today's and the grid from the new counts
addEventListener('papier:update', () => { days = JSON.parse(data.textContent); today(); if (on) build(false); });
})();</script>"""


def papier_days(read: list[dict]) -> list[dict]:
    """Days from the reads alone, when what was pinned isn't known: every day with reads, finished."""
    out: dict[str, dict] = {}
    for a in sorted(read, key=lambda a: a["read_at"] or ""):
        d = out.setdefault(a["read_at"][:10], {"day": a["read_at"][:10], "pinned": 0, "read": 0, "titles": [], "abstracts": []})
        d["pinned"] += 1
        d["read"] += 1
        d["titles"].append(a["title"] or "Untitled")
        d["abstracts"].append(a.get("abstract") or "")
    return list(out.values())


def papier(read: list[dict], days: list[dict] | None = None, today: date | None = None) -> tuple[str, str, str, str]:
    """Papier-mâché for a Read list the page shows: the switch for the panel's header, the
    instructions (shown when it's on), the pictures' container with their days and script, and the
    place for today's read (today's word art so far), on top of the panel in both modes.
    One picture a day the reader pinned pushes, made of the ones they read and filled in as they
    read them: reading everything pinned that day finishes it (3 read and 2 still pinned: 3/5). The
    date picks the shape, the same for everyone that day."""
    if not read:
        return "", "", "", ""
    days = days if days is not None else papier_days(read)
    data = json.dumps([{"day": d["day"], "pinned": d["pinned"], "read": d["read"], "titles": d["titles"],
                        "abstracts": d.get("abstracts") or []}
                       for d in days]).replace("</", "<\\/")
    done = sum(1 for d in days if d["pinned"] and d["read"] >= d["pinned"])
    switch = ('<button type="button" class="pm" id="papier-on" aria-pressed="false">'
              '<span class="track"><span class="knob"></span></span>Papier-mâché</button>')
    hint = (f'<p class="papier-hint" hidden>Each day’s pinned pushes make a picture: read them all to finish it · '
            f'{done} finished · Tap one to share it</p>')
    top = '<div class="papier-today" id="papier-today"></div>'  # today's read, filled in by the script
    gallery = (f'<div class="papier" id="papier" hidden></div>'
               f'<script type="application/json" id="papier-data" '
               f'data-today="{(today or date.today()).isoformat()}">{data}</script>{PAPIER_SCRIPT}')
    return switch, hint, gallery, top


# /demo: a public profile with made-up pushes and reads, to show the daily pictures (most finished, a few not). Nothing is stored: real
# opens and reads are the model's labels, so fake ones must never reach the database.
DEMO_TITLES = [
    "The Quiet Return of the Night Train", "What the Tide Leaves Behind", "A City Learns to Love Its Rain",
    "The Last Typewriter Repairman in Ohio", "Inside the Race to Map the Ocean Floor", "Why Everyone Is Baking Bread Again",
    "The Secret Life of Subway Pigeons", "How a Small Town Saved Its Library", "The Astronomers Who Count Dark Skies",
    "A Violin Made From a Fallen Tree", "The Long Walk Home Through the Snow", "Notes From a Lighthouse Keeper",
    "The Orchard That Outlived Three Owners", "Learning to Swim at Seventy", "A Museum for Lost Mittens",
    "The Beekeepers of the Rooftops", "What Your Houseplants Are Telling You", "The Comeback of the Corner Bookstore",
    "A Ferry Captain's Last Crossing", "The Mathematics of a Perfect Pancake", "Where the Fireflies Went",
    "A Choir of Strangers Sings at Dawn", "The Map That Fits in a Pocket", "The Night Market Under the Bridge",
    "A Glacier's Slow Goodbye", "The Puzzle Makers of Vermont", "How Owls See in the Dark",
    "A Garden on the Fortieth Floor", "The Postman Who Knew Everyone",
    'The Last Drive-In Theater on Route 9',
    'A Lighthouse Gets a New Lamp',
    'The Ferry Cats of the Harbor',
    'Why the Town Clock Runs Five Minutes Fast',
    'A Kite Festival Fills the Winter Sky',
    'The Seed Library in the Old Bank',
    'Inside a Workshop That Builds Canoes by Hand',
    'The Diner That Never Closes',
    "A Choir for People Who Can't Sing",
    'The Mapmaker Who Walks Every Street',
    'When the Orchestra Played in a Parking Garage',
    'The Return of the Neighborhood Milkman',
    'A Hundred-Year-Old Carousel Turns Again',
    'The Quiet Art of Fixing Old Clocks',
    'Where the Tulips Bloom in November',
    "A Bus Driver's Book of Poems",
    'The Island Where Everyone Knows the Weather',
    'How a Tiny Bakery Feeds a Whole Block',
    'The Rescue Dogs of the Mountain Pass',
    'A Village Builds a Bridge Without Nails',
    'The Night Shift at the Planetarium',
    "A Grandmother's Recipe Goes Viral",
    'The Painters Restoring Faded Murals',
    'The Stationery Store That Refuses to Close',
    'A Lake That Freezes Into Singing Ice',
    'The Students Who Planted a Forest',
    'A Train Station Turned Into a Library',
    'The Snow Sculptors of the North',
    'How One Street Learned to Share Its Tools',
    'A Harbor Seal Becomes a Local Celebrity',
    'The Ice Cream Shop With Seasonal Flavors',
    'A Cartographer Maps the Night Sky by Hand',
    'The Gardeners of the Abandoned Rail Line',
    'A Radio Station Run by Teenagers',
]


def demo_data() -> tuple[list[dict], list[dict]]:
    """The demo reader: a Medium reader (4 pushes a day) from Oct 12 to today, Oct 29: most days all
    read, a few not finished, nothing read on Oct 23, and the last six days in a row: Read 63, Daily
    streak 6. Returns the reads and the days (Store.days' shape)."""
    read, by_day, k = [], {}, 0
    for day, pinned, n in DEMO_DAYS:
        d = {"day": f"2026-10-{day:02d}", "pinned": pinned, "read": n, "titles": [], "abstracts": []}
        for j in range(n):
            read.append({"news_id": f"DEMO{k}", "title": DEMO_TITLES[k], "abstract": DEMO_ABSTRACTS[k],
                         "url": "https://www.nytimes.com", "section": None, "read_at": f"{d['day']}T{8 + 4 * j:02d}:00"})
            d["titles"].append(DEMO_TITLES[k]); d["abstracts"].append(DEMO_ABSTRACTS[k]); k += 1
        by_day[d["day"]] = d
    return read, list(by_day.values())


def demo_page() -> str:
    """The demo reader's public link: what a visitor sees."""
    read, days = demo_data()
    return profile_page({"name": "Justin", "history_public": 1}, read, owner=False, today=date(2026, 10, 29), days=days)


def demo_owner_page(share_url: str, caps: dict | None = None) -> str:
    """The demo reader's own view, as if they'd just tapped today's third push: it pinned on top,
    then the privacy switch, Pinned, Read with the pictures and pushes a day. The buttons only
    change the page."""
    read, days = demo_data()
    return profile_page({"name": "Justin", "history_public": 0, "volume": "medium"}, read, owner=True,
                        share_url=share_url, today=date(2026, 10, 29), pinned=DEMO_PIN, unread=DEMO_PINNED,
                        days=days, caps=caps, inert=True)


# today's third push, just tapped (pinned, not read yet: reading it would finish Oct 29's picture);
# DEMO_PINNED adds the ones pinned on earlier days and never read (Oct 15, 19, 23); made up
DEMO_PIN = {"push_id": "DEMO-PIN", "news_id": "DEMO-PIN", "section": "Arts", "url": "https://www.nytimes.com", "read": False,
            "title": "The Lantern Makers of the River Festival",
            "abstract": "Each autumn, families fold paper lanterns by hand and set them floating downstream at dusk."}
DEMO_PINNED = [  # most recently pinned first: today's, then two from Oct 23, two from Oct 19, one from Oct 15
    {"news_id": "DEMO-PIN", "push_id": "DEMO-PIN", "url": "https://www.nytimes.com", "title": DEMO_PIN["title"], "day": "2026-10-29"},
    *({"news_id": f"DEMO-P{k}", "push_id": f"DEMO-P{k}", "url": "https://www.nytimes.com", "title": t, "day": day}
      for k, (t, day) in enumerate([
          ("The Ferry That Carries Pianos Across the Lake", "2026-10-23"), ("A Town Votes on Its Favorite Tree", "2026-10-23"),
          ("Why Cities Are Planting Tiny Forests", "2026-10-19"), ("The Last Glassblower on Main Street", "2026-10-19"),
          ("A Night Bus Driver's Favorite Stops", "2026-10-15")]))]


# (day of October, pushes pinned, read): 18 days, 14 finished; the unread pins are DEMO_PINNED
DEMO_DAYS = [(12, 4, 4), (13, 4, 4), (14, 4, 4), (15, 4, 3), (16, 4, 4), (17, 4, 4), (18, 4, 4), (19, 4, 2),
             (20, 4, 4), (21, 4, 4), (22, 4, 4), (23, 2, 0), (24, 4, 4), (25, 4, 4), (26, 4, 4), (27, 4, 4),
             (28, 4, 4), (29, 3, 2)]


# made up, like the titles: one line each, in the same order
DEMO_ABSTRACTS = [
    "Sleeper cars are rolling again as travelers trade airports for a slower way across the country.",
    "Beachcombers catalog the shells, glass and driftwood that every storm brings ashore.",
    "After years of floods, one city built parks that soak up storms, and now rainy days bring people outside.",
    "He has fixed thousands of machines and still keeps a drawer of ribbons nobody makes anymore.",
    "Scientists and sonar ships are charting the vast seabed that remains less known than the moon.",
    "Sourdough starters are passed hand to hand as home bakers rediscover a slow, patient craft.",
    "The birds that ride the trains know the schedules better than many commuters do.",
    "When the county cut its budget, residents held bake sales and raised enough to keep the doors open.",
    "Volunteers stay up late to measure how much light is stealing the stars from small towns.",
    "A storm toppled an old maple, and a luthier turned its wood into an instrument with a warm voice.",
    "A father and daughter trek through a blizzard and find the road home longer and kinder than expected.",
    "A keeper's logbook records ships, storms and the slow seasons on a rocky point of the coast.",
    "Generations of families have tended the same trees and harvested the same sweet apples.",
    "Retirees fill the community pool at dawn, learning strokes they never had time for.",
    "A small museum displays hundreds of single mittens and the stories of how they were lost.",
    "City beekeepers keep hives on rooftops, and their honey tastes of the neighborhood's flowers.",
    "Drooping leaves and yellow tips are signals; here is how to read them and help your plants thrive.",
    "Independent bookshops are opening again, drawing readers with events, cats and hand-written notes.",
    "After forty years on the water, a captain steers her ferry one last time across the bay.",
    "A physicist explains the batter, the heat and the timing behind a golden, fluffy stack.",
    "Light pollution and lost meadows are dimming summer nights, but some towns are bringing them back.",
    "Strangers gather on a hill each morning to sing together as the sun comes up.",
    "A folding map designer explains how to fit a whole city onto a single sheet of paper.",
    "Food stalls light up the space beneath an old bridge, serving dishes from a dozen countries.",
    "Researchers return each year to measure how far the ice has retreated up the valley.",
    "A family of puzzle makers cuts wooden pieces by hand in a barn in the Green Mountains.",
    "Huge eyes and sensitive feathers let owls hunt in near-total darkness.",
    "Residents of a high-rise grow tomatoes, herbs and flowers on a terrace high above the street.",
    "For decades, the town's mail carrier knew every name, dog and birthday on his route.",
    'Families still park under the stars on summer nights, and the owner runs the projector himself.',
    'Volunteers carried the new lens up a hundred steps so the light could shine again over the bay.',
    'A dozen cats ride the morning boats, and commuters bring them treats. Nobody remembers who adopted the first one.',
    'No one wants to fix it, because the whole town has learned to arrive early.',
    'Hundreds of kites rose over the frozen field as families braved the cold for a bright afternoon.',
    'Gardeners borrow seeds in spring and return new ones in the fall, filling old safe-deposit drawers.',
    'Two brothers shape cedar strips into boats that last for generations. Each canoe takes a full winter.',
    'Truckers, nurses and night owls share the counter, and the coffee has never stopped pouring.',
    'The director turns no one away, and the result is joyful, loud and surprisingly moving.',
    'He has walked every block in the city with a notebook, drawing what the satellites miss.',
    'Musicians filled the concrete levels with sound, and drivers stopped to listen on the ramps.',
    'Glass bottles are back on doorsteps as a young farmer revives a delivery route from the 1950s.',
    'Craftsmen repainted every horse, and children lined up for the first ride in decades.',
    'In a small shop, a repairer coaxes century-old movements back to life, one gear at a time.',
    "A mild autumn confused the bulbs, and the town's gardens burst into color weeks early.",
    'Between stops, she writes about her passengers. Her first collection sold out in a week.',
    'Fishermen read the clouds before dawn, and their forecasts rarely miss.',
    'The ovens start at three in the morning, and by seven the line stretches down the street.',
    'Trained dogs guide lost hikers through blizzards, and their handlers say the dogs know the way better.',
    'Carpenters used old joinery methods, fitting each timber so tightly that no metal was needed.',
    "Astronomers keep the dome open late for school groups who have never seen Saturn's rings.",
    'A video of her dumpling recipe reached millions, and now strangers mail her thank-you notes.',
    'Artists climb scaffolds to bring back colors that have faded under decades of sun.',
    'The owner still sells fountain pens and wax seals, and letter writers travel to find her shop.',
    "When the temperature drops just right, the ice hums and chirps under skaters' blades.",
    'A class planted a thousand saplings on an empty lot. Twenty years later, it is a forest.',
    'Commuters now borrow novels while they wait, and the old ticket booth is the checkout desk.',
    'Teams carve towering figures from snow blocks during a festival that melts away by spring.',
    'Neighbors built a shed of shared ladders, drills and saws, and borrowing is free.',
    'Visitors line the pier to watch the seal nap on the rocks, and it has its own fan club.',
    'The flavors change with the harvest, from rhubarb in spring to roasted pumpkin in the fall.',
    'With a pencil and a telescope, she charts the stars the way sailors once did.',
    'Volunteers turned a forgotten track into a green path lined with flowers and benches.',
    'Students host the morning show, pick the music and interview the mayor once a month.',
]


PIN_MARK = '<img class="pinmark" src="/icon.png" alt="PushPin">'

COOKIE = "pushpin_reader"


def serve_cards(store: Store, port: int, block: bool = False, caps: dict | None = None,
                verify_apple=None) -> ThreadingHTTPServer:
    """The phone-facing endpoints. The reader is identified by the `pushpin_reader` cookie.
    `caps` maps each volume to its daily cap (configs/notifier.yml), shown on the buttons.

      GET  /p/<push_id>        pins the push: records the open (creating the reader on first visit)
                               and shows their profile with its article on top, with Read
      POST /read/<push_id>     Read was tapped on it (sent as a beacon; the link itself goes to NYT)
      GET  /start              the welcome page: choose light / medium / heavy (creates the reader)
      POST /volume             body "light", "medium" or "heavy": pushes a day
      POST /name               body: the reader's first name
      GET  /me                 this reader's profile: pinned, articles read, public toggle, pushes a day
      POST /remove/<news_id>   delete an article they pinned or read from their profile
      POST /public             body "on" or "off": show the count and list on their public link
      GET  /u/<slug>           a reader's public profile
      GET  /stats              {"readers": n} for autoscaling (KEDA), only with header
                               X-Api-Key = $CARDS_STATS_TOKEN; 404 when that isn't set
      GET  /demo               a public profile with 63 made-up reads over 18 days of pictures; stores nothing
      GET  /demo/me            the same reader's own view (pinned push, privacy switch, Pinned, pushes a day);
                               its buttons only change the page; stores nothing
      GET  /health             liveness

    The iOS app's JSON API, on when PUSHPIN_SESSION_SECRET and PUSHPIN_APPLE_BUNDLE_ID are set.
    Every call but sign-in sends `Authorization: Bearer <session>`:
      POST   /api/auth/apple       {"identity_token", "name"?} -> {"session", "new", "profile"}
      GET    /api/me               the profile (same data as /me)
      POST   /api/devices          {"token", "sandbox"?}: where to push (APNs device token)
      DELETE /api/devices/<token>  stop pushing to a device (signing out)
      POST   /api/open/<push_id>   a push was tapped: pins it -> {"push", "profile"}
      POST   /api/read/<push_id>   Read was tapped
      POST   /api/volume           {"volume": "light" | "medium" | "heavy"}
      POST   /api/name             {"name"}
      POST   /api/public           {"public": true | false}
      DELETE /api/articles/<news_id>  delete from Pinned / Read
      DELETE /api/account          delete the account and its data
    """

    stats_token = os.environ.get("CARDS_STATS_TOKEN")
    session_secret = os.environ.get("PUSHPIN_SESSION_SECRET")
    bundle_id = os.environ.get("PUSHPIN_APPLE_BUNDLE_ID")
    verify_apple = verify_apple or (lambda token: apple.verify_identity_token(token, bundle_id))

    def profile_json(reader_id: str, base: str) -> dict:
        reader, read = store.reader(reader_id), store.articles_read(reader_id)
        return {"reader": {"name": reader["name"], "volume": reader["volume"] or DEFAULT_VOLUME, "public": bool(reader["history_public"])},
                "share_url": f"{base}/u/{store.slug(reader_id)}", "caps": caps or {},
                "pinned": store.pinned_unread(reader_id), "read": read,
                "streak": streak(read, date.fromisoformat(store.now()[:10]))}

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

        def _own_profile(self, reader_id: str, pinned: dict | None = None) -> str:
            proto = self.headers.get("x-forwarded-proto") or "http"
            share = f"{proto}://{self.headers.get('host') or 'localhost'}/u/{store.slug(reader_id)}"
            return profile_page(store.reader(reader_id), store.articles_read(reader_id),
                                owner=True, share_url=share, pinned=pinned, caps=caps, today=date.fromisoformat(store.now()[:10]),
                                unread=store.pinned_unread(reader_id), days=store.days(reader_id))

        def _base(self) -> str:
            return f"{self.headers.get('x-forwarded-proto') or 'http'}://{self.headers.get('host') or 'localhost'}"

        def _api(self, method: str, path: str) -> None:
            """The iOS app's JSON API (see serve_cards)."""
            if not (session_secret and bundle_id):
                self._send(404, json.dumps({"error": "api off"}), "application/json")
                return
            reply = lambda status, obj: self._send(status, json.dumps(obj), "application/json")
            n = int(self.headers.get("content-length") or 0)
            try:
                body = json.loads(self.rfile.read(min(n, 16384)) or b"{}") if n else {}
                assert isinstance(body, dict)
            except (ValueError, AssertionError):
                reply(400, {"error": "bad json"})
                return
            what, _, arg = path.partition("/")
            if (method, what, arg) == ("POST", "auth", "apple"):
                try:
                    sub = verify_apple(str(body.get("identity_token") or ""))
                except (jwt.InvalidTokenError, jwt.PyJWKClientError) as e:
                    reply(401, {"error": f"sign-in failed: {e}"})
                    return
                reader_id, new = store.reader_for_apple(sub, int(time.time()))
                if new and body.get("name"):
                    store.set_name(reader_id, str(body["name"]))
                print(json.dumps({"event": "sign_in", "reader": reader_id, "new": new}))
                reply(200, {"session": apple.make_session(reader_id, session_secret), "new": new,
                            "profile": profile_json(reader_id, self._base())})
                return
            auth = self.headers.get("authorization") or ""
            reader_id = apple.read_session(auth.removeprefix("Bearer ").strip(), session_secret)
            if store.reader(reader_id) is None:
                reply(401, {"error": "sign in"})
                return
            ok = True
            if (method, what) == ("GET", "me"):
                reply(200, profile_json(reader_id, self._base()))
                return
            elif (method, what) == ("POST", "open") and store.interact(arg, reader_id, "open"):
                print(json.dumps({"event": "open", "push_id": arg, "reader": reader_id, "app": True}))
                push = store.push(arg, reader_id)
                push.pop("for_reader")
                push["read"] = bool(push["read"])  # 0/1 from SQLite
                reply(200, {"push": push, "profile": profile_json(reader_id, self._base())})
                return
            elif (method, what) == ("POST", "devices"):
                ok = store.add_device(reader_id, str(body.get("token") or ""), bool(body.get("sandbox")))
            elif (method, what) == ("DELETE", "devices"):
                ok = store.remove_device(arg, reader_id)
            elif (method, what) == ("POST", "read"):
                ok = store.interact(arg, reader_id, "read")
            elif (method, what) == ("POST", "volume"):
                ok = store.set_volume(reader_id, str(body.get("volume")))
            elif (method, what) == ("POST", "name"):
                ok = store.set_name(reader_id, str(body.get("name") or ""))
            elif (method, what) == ("POST", "public"):
                ok = store.set_history_public(reader_id, bool(body.get("public")))
            elif (method, what) == ("DELETE", "articles"):
                ok = store.remove(reader_id, arg)
            elif (method, what) == ("DELETE", "account"):
                ok = store.delete_reader(reader_id)
                print(json.dumps({"event": "delete_account", "reader": reader_id}))
                reply(200 if ok else 404, {"ok": ok})
                return
            else:
                ok = None
            if ok is None:
                reply(404, {"error": "unknown"})
            elif not ok:
                reply(404, {"error": "not found"})
            else:
                print(json.dumps({"event": what, "arg": arg or None, "reader": reader_id, "app": True}))
                reply(200, profile_json(reader_id, self._base()))

        def do_DELETE(self):
            route, rest = self._route()
            if route == "api":
                self._api("DELETE", rest)
            else:
                self._send(404, "", "text/plain")

        def _route(self) -> tuple[str, str]:
            route, _, push_id = self.path.split("?")[0].lstrip("/").partition("/")
            return route, push_id

        def do_POST(self):
            route, push_id = self._route()
            if route == "api":
                self._api("POST", push_id)
                return
            body = self.rfile.read(min(int(self.headers.get("content-length") or 0), 200)).decode(errors="replace")
            reader_id = self._reader_id()
            if reader_id is None:
                self._send(403, "open a push first", "text/plain")
                return
            if route == "remove":
                ok = store.remove(reader_id, push_id)
            elif route == "public":
                ok = store.set_history_public(reader_id, body.strip() == "on")
            elif route == "name":
                ok = store.set_name(reader_id, body)
            elif route == "volume":
                ok = store.set_volume(reader_id, body.strip())
            elif route == "read":
                ok = store.interact(push_id, reader_id, "read")
            else:
                ok = False
            if not ok:
                self._send(404, "unknown push", "text/plain")
                return
            print(json.dumps({"event": route, "push_id": push_id or None, "reader": reader_id,
                              **({"on": body.strip() != "off"} if route == "public" else {}),
                              **({"volume": body.strip()} if route == "volume" else {})}))
            self._send(200, "ok", "text/plain")

        def do_GET(self):
            route, push_id = self._route()
            if route in ICONS and not push_id:
                data = (ASSETS / ICONS[route]).read_bytes()
                self.send_response(200)
                self.send_header("content-type", "image/png")
                self.send_header("content-length", str(len(data)))
                self.send_header("cache-control", "public, max-age=604800")
                self.end_headers()
                self.wfile.write(data)
            elif route == "health":
                self._send(200, "ok", "text/plain")
            elif route == "demo" and push_id == "me":
                proto = self.headers.get("x-forwarded-proto") or "http"
                self._send(200, demo_owner_page(f"{proto}://{self.headers.get('host') or 'localhost'}/demo", caps))
            elif route == "demo":
                self._send(200, demo_page())
            elif route == "api":
                self._api("GET", push_id)
            elif route == "stats" and stats_token and secrets.compare_digest(
                    self.headers.get("x-api-key") or "", stats_token):
                self._send(200, json.dumps({"readers": store.reader_count()}), "application/json")
            elif route == "start":
                reader_id, new = self._reader_id(), None
                if reader_id is None:  # subscribed now: pushes from here on count for them
                    reader_id = new = store.new_reader(int(time.time()))
                self._send(200, start_page(store.reader(reader_id), caps), set_reader=new)
            elif route == "me":
                reader_id = self._reader_id()
                if reader_id is None:
                    self._send(200, page("<h1>Your reading</h1><p>Open a push first; your profile starts there.</p>"))
                    return
                self._send(200, self._own_profile(reader_id))
            elif route == "u" and (other := store.reader_by_slug(push_id)) is not None:
                rid = other["reader_id"]
                self._send(200, profile_page(other, store.articles_read(rid), owner=False, days=store.days(rid),
                                             today=date.fromisoformat(store.now()[:10])))
            elif route == "p" and (push := store.push(push_id)) is not None:
                reader_id, new = self._reader_id(), None
                if reader_id is None:
                    reader_id = new = store.new_reader(push["sent_ts"])
                store.interact(push_id, reader_id, "open")
                print(json.dumps({"event": "open", "push_id": push_id, "reader": reader_id, "new_reader": bool(new)}))
                self._send(200, self._own_profile(reader_id, store.push(push_id, reader_id)), set_reader=new)
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
    caps = config.get("volume_caps") or {v: len(h) for v, h in (config.get("volume_slots") or {}).items()}
    serve_cards(open_store(config), port, block=True, caps=caps or None)


if __name__ == "__main__":
    main()
