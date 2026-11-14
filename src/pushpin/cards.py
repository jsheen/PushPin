"""The push card server: what a tapped notification opens.

  python -m pushpin.cards        # serves on $PORT (default 8090)

Kept separate from the notifier's schedule so each can run where it fits: locally both are
containers in docker compose; in the cloud the cards are a Cloud Run service (scales with taps,
including to zero) while each slot is a Cloud Run job. Both share the database in DATABASE_URL
(Cloud SQL), or the SQLite file from configs/notifier.yml when it isn't set.

Tapping a push "pins" its article: the open is recorded (the click the model learns from) and the
reader lands on their profile with that article on top and two choices: Read (a plain nytimes.com
link, so iOS opens the NYT app if it's installed; the tap is recorded as a read) or Put in Crème.
The first push a phone opens, or the /start page (linked from the welcome push,
`pushpin.notifier --welcome`), sets a cookie with a random reader id, and everything the reader
does is stored under it. What they do on NYT isn't tracked.

/me is the same profile without a pinned article: their top article on top (the Crème), the
articles they pinned but haven't read yet (each with Read, ☆ and × to delete it), how many
articles they've read (tapped Read on) with the list (× deletes one, from the Crème too), which they can make public, and how many
pushes a day they want (light, medium or heavy). /u/<slug> is the profile for anyone with the
link: the Crème always, the count and list only when public.
"""

import argparse
import hashlib
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
from pushpin.store import DEFAULT_VOLUME, MAX_FAVORITES, VOLUMES, Store

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
.papier{opacity:0;transition:opacity .5s ease;margin-top:4px}.papier.on{opacity:1}.papier figure{width:80%;margin:0 0 26px}.papier figure.right{margin-left:auto}
.papier canvas{display:block;width:100%}.papier figcaption{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);text-align:center;margin-top:6px}
.profile .sub{color:var(--muted);font-size:15px;margin:0}.profile header .toggle{margin:12px 0 6px}
.profile section{border-top:1px solid var(--line);padding:24px 0 8px}
.profile h2{font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:600;margin:0 0 12px}
.profile .list li{display:flex;gap:12px;align-items:flex-start}.profile .list li>div{flex:1;min-width:0}
.profile .list a{color:var(--fg)}.profile .list a:hover{color:var(--link)}
.profile .empty{color:var(--muted)}.profile .private{font-size:14px;color:var(--muted);margin:-6px 0 8px}.profile h2 .streak{margin-left:4px}.profile h2 .n{color:var(--fg);margin-left:6px;letter-spacing:0}
.profile section>.list>li:last-child{border-bottom:0}
.star{font:inherit;font-size:20px;line-height:1;padding:2px 4px;border:0;background:none;color:var(--muted);cursor:pointer}
.star[aria-pressed=true]{color:var(--on)}
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
.pin .creme[aria-pressed=true]{color:var(--on)}.profile .volume{margin:24px 0 0}
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
push to pin it, then Read it or keep it; that's how it learns what you like.</p>
{volume_picker(None, caps, ask=True)}{OWNER_SCRIPT}""")


PROFILE_SCRIPT = """<script>
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
  if (r && r.ok) return location.reload();
  document.querySelector(".note").textContent = "Couldn't save that, try again.";
}));
// ×: delete an article from the profile (and the Crème), after a confirm
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
    if (!s.classList.contains("gone") || ev.target.closest("a, button, input, label, summary")) return;
    s.classList.remove("gone");
    s.focus();
  });
  s.focus();
})();
</script>"""


def pin_panel(push: dict, creme: set[str]) -> str:
    """The article a push tap pinned, on top of the profile: Read, and once it's read, put it in
    the Crème (only read articles go there)."""
    e = html.escape
    on = push["news_id"] in creme
    creme_row = (f'<div class="row"><button class="creme" data-fav="{e(push["news_id"])}" '
                 f'aria-pressed="{"true" if on else "false"}">{"★ In your Crème" if on else "☆ Put in Crème"}'
                 f'</button></div>') if push.get("read") else ""
    return (f'<section class="pin"><div class="kicker">{e(push["section"] or "")}</div>'
            f'<div class="headline">{e(push["title"] or "")}</div><p class="abstract">{e(push["abstract"] or "")}</p>'
            # a plain link to nytimes.com (not a redirect) lets iOS open it in the NYT app when installed
            f'<a class="read" href="{e(push["url"] or "#")}" data-read="{e(push["push_id"])}">Read</a>'
            f'{creme_row}</section>')


def profile_page(reader: dict, read: list[dict], favorites: list[dict], owner: bool, share_url: str = "",
                 today: date | None = None, pinned: dict | None = None, caps: dict | None = None,
                 unread: list[dict] | None = None) -> str:
    """A reader's profile. `owner`: their own view (/me, or /p/<push_id> with that push's article
    `pinned` on top), with the articles pinned but not read yet (`unread`: tap one to read it; no
    star until it's read), the star buttons, the public toggle, pushes a day and always the list;
    otherwise the public view (/u/<slug>), where the count and list only show if they made them
    public. Article links go straight to NYT, so a visitor isn't counted as opening or reading."""
    e = html.escape
    favs = favorites[-MAX_FAVORITES:]
    fav_ids = {a["news_id"] for a in favs}
    name = reader.get("name")
    public = bool(reader.get("history_public"))

    def item(a, star, delete=False):
        on = a["news_id"] in fav_ids
        button = (f'<button class="star" data-fav="{e(a["news_id"])}" aria-pressed="{"true" if on else "false"}" '
                  f'aria-label="Add to Crème">{"★" if on else "☆"}</button>') if star else ""
        if delete:  # the owner's Pinned and Read lists: delete it from the profile, Crème included
            ask = "Delete this article from your PushPin?" + (" It also leaves your Crème." if on else "")
            button += (f'<button class="del" data-del="{e(a["news_id"])}" data-confirm="{e(ask)}" '
                       f'aria-label="Delete">×</button>')
        read_tap = f' data-read="{e(a["push_id"])}"' if a.get("push_id") else ""  # a pinned one: tapping reads it
        return (f'<li><div><a href="{e(a["url"] or "#")}"{read_tap}>{e(a["title"] or "Untitled")}</a>'
                f'</div>{button}</li>')

    slots = "".join(item(a, owner) for a in favs)
    if owner:
        slots = slots or '<li class="empty">Nothing yet. Tap ☆ on an article.</li>'
    elif not favs:
        slots = '<li class="empty">Nothing picked yet.</li>'
    hint = '<p class="private">Put your favorite article here.</p>' if owner else ""
    top = f'<section><h2>Crème</h2>{hint}<ul class="list">{slots}</ul></section>'
    waiting = [a for a in (unread or []) if not pinned or a["news_id"] != pinned["news_id"]] if owner else []
    if waiting:  # only the owner sees what they pinned; the one on top isn't listed twice
        top += (f'<section><h2>Pinned <span class="n">{len(waiting)}</span></h2>'
                f'<p class="private">Always private: only you see what you’ve pinned. '
                f'Tap a headline to read it and it moves to Read.</p><ul class="list">'
                f'{"".join(item(a, False, True) for a in waiting)}</ul></section>')

    n = len(read)
    first = date.fromisoformat(min(a["read_at"] for a in read)[:10]) if read else None
    since = f'<p class="since">Started reading on {first:%B} {first.day}, {first.year}</p>' if first else ""
    days = streak(read, today or date.today())
    on_a_roll = f' <span class="streak">(Daily streak <span class="n">{days}</span>)</span>' if days else ""
    count = since if owner or public else ""  # the number itself is in the Read header below
    # Read n (Daily streak d), with the Papier-mâché switch on the right, then the list. A private
    # list on a public link shows only that header: no titles, so no switch and no dinosaurs.
    shown = owner or public  # Papier-mâché only uses titles the page already shows
    switch, hint, gallery = papier(read, papier_seed(reader)) if shown else ("", "", "")
    head = f'<div class="readhead"><h2>Read <span class="n">{n}</span>{on_a_roll}</h2>{switch}</div>'
    articles = (f'<div class="readpanel">{head}{hint}<ul class="list">{"".join(item(a, owner, owner) for a in read)}</ul>'
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
    scripts = (OWNER_SCRIPT + PROFILE_SCRIPT) if owner else ""
    intro = ""
    if not owner:  # the public link opens with a short summary
        stats = [("Read", n), *([("Daily streak", days)] if days else [])] if public else []
        intro = splash(title, stats)
    if owner:  # the pinned push first
        intro = pin_panel(pinned, fav_ids) if pinned else ""
    body = (f'{intro}<div class="profile"><header><h1>{title}</h1>{count}{sub}{ask_name}</header>{top}'
            f'<section>{bottom}</section></div>{scripts}')
    return page(body).replace("<main>", '<main style="max-width:540px">', 1)


# Papier-mâché: the read titles as concrete poetry. Every PAPIER_PER articles read makes a dinosaur
# out of their titles; the one still being made is filled tail-first, the rest pencilled in.
PAPIER_PER = 5
PAPIER_SCRIPT = """<script>(() => {
const sw = document.getElementById('papier-on'), box = document.getElementById('papier');
const data = document.getElementById('papier-data'), titles = JSON.parse(data.textContent), PER = %PER%;
const panel = sw.closest('.readpanel'), list = panel.querySelector('.list'), hint = panel.querySelector('.papier-hint');
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
// the order they're made in: the original six first, then the families take turns
const KINDS = (() => {
  const fams = ['sauropod', 'theropod', 'stegosaur', 'ceratopsian', 'hadrosaur', 'ankylosaur'];
  const queues = fams.map(f => SPECIES[f].map(([name, knobs]) => ({name, plan: f, knobs})));
  const out = [];
  while (queues.some(q => q.length)) for (const q of queues) if (q.length) out.push(q.shift());
  return out;
})();
// Each reader's own herd: a seed from the reader shuffles the species into their own order and
// stretches each dinosaur's proportions a little (the knobs below, 0.92-1.08x), so no two readers'
// dinosaurs match. Without a seed (the demo), the fixed order above and the knobs as drawn.
const JITTER = {sauropod: ['bulk', 'neck', 'tail', 'head'], theropod: ['bulk', 'head', 'neck', 'arm'],
  stegosaur: ['plates'], ankylosaur: ['bumps'], ceratopsian: ['frill', 'brow', 'nose', 'bulk'], hadrosaur: ['bulk']};
function rng(str) {  // a small seeded generator (string hash + mulberry32): the same seed, the same numbers
  let h = 1779033703 ^ str.length;
  for (let i = 0; i < str.length; i++) { h = Math.imul(h ^ str.charCodeAt(i), 3432918353); h = h << 13 | h >>> 19; }
  return () => {
    h = h + 0x6D2B79F5 | 0; let t = Math.imul(h ^ h >>> 15, 1 | h);
    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296;
  };
}
function herd(seed) {
  if (!seed) return k => KINDS[k % KINDS.length];
  const order = KINDS.slice(), r = rng(seed);
  for (let i = order.length - 1; i > 0; i--) { const j = Math.floor(r() * (i + 1)); [order[i], order[j]] = [order[j], order[i]]; }
  return k => {
    const kind = order[k % order.length], rk = rng(seed + ':' + k), knobs = {...kind.knobs};
    for (const key of JITTER[kind.plan]) knobs[key] = (knobs[key] ?? 1) * (0.92 + 0.16 * rk());
    return {...kind, knobs};
  };
}
// ---- end species ----
function dino(c, w, h, flip, kind) {
  c.save();
  const s = Math.min(w / 200, h / 130);
  c.translate((w - 200 * s) / 2, (h - 130 * s) / 2);
  if (flip) { c.translate(200 * s, 0); c.scale(-1, 1); }
  c.scale(s, s);
  c.fillStyle = c.strokeStyle = '#000';
  const eye = PLANS[kind.plan](c, kind.knobs);
  c.globalCompositeOperation = 'destination-out';
  c.beginPath(); c.arc(eye[0], eye[1], 2.8, 0, 7); c.fill();
  c.restore();
}
function draw(canvas, words, built, flip, animate, kind) {
  const dpr = window.devicePixelRatio || 1, w = Math.round(canvas.clientWidth), h = Math.round(w * 0.64);
  canvas.style.height = h + 'px'; canvas.width = w * dpr; canvas.height = h * dpr;
  const c = canvas.getContext('2d'); c.setTransform(dpr, 0, 0, dpr, 0, 0);
  const m = document.createElement('canvas'); m.width = w; m.height = h;
  const mc = m.getContext('2d'); dino(mc, w, h, flip, kind);
  const mask = mc.getImageData(0, 0, w, h).data, inside = (x, y) => mask[(y * w + x) * 4 + 3] > 127;
  const fs = Math.max(6, Math.round(w / 60)), lh = Math.round(fs * 1.1);
  c.font = fs + 'px Georgia, "Times New Roman", serif'; c.textBaseline = 'middle';
  const text = words.join(' · ') + ' · ', widths = {};
  const width = ch => widths[ch] ??= c.measureText(ch).width;
  const glyphs = []; let i = 0;
  for (let y = Math.round(lh / 2); y < h; y += lh) {
    let x = 0;
    while (x < w) {
      while (x < w && !inside(x, y)) x++;
      let end = x; while (end < w && inside(end, y)) end++;
      let cx = x, guard = 0;
      while (guard++ < 400) {
        const ch = text[i % text.length];
        if (cx === x && ch === ' ') { i++; continue; }
        if (cx + width(ch) > end) break;
        glyphs.push([ch, cx, y]); cx += width(ch); i++;
      }
      x = end + 1;
    }
  }
  const css = getComputedStyle(document.documentElement);
  const ink = css.getPropertyValue('--fg').trim() || '#121212', pencil = css.getPropertyValue('--line').trim() || '#e2e2e2';
  const reach = w * built;  // built from the tail: left to right, or right to left when flipped
  const isBuilt = ([, x]) => built >= 1 || (flip ? x >= w - reach : x <= reach);
  const start = performance.now(), sweep = animate ? 900 : 0, fade = animate ? 280 : 0;
  const frame = now => {
    c.clearRect(0, 0, w, h);
    let busy = false;
    for (const g of glyphs) {
      const along = flip ? 1 - g[1] / w : g[1] / w;
      const t = fade ? Math.min(1, Math.max(0, (now - start - along * sweep - (g[2] / h) * 120) / fade)) : 1;
      if (t < 1) busy = true;
      if (t <= 0) continue;
      c.globalAlpha = t * t * (3 - 2 * t);  // smoothstep
      c.fillStyle = isBuilt(g) ? ink : pencil;
      c.fillText(g[0], g[1], g[2]);
    }
    c.globalAlpha = 1;
    if (busy) requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
}
const kindOf = herd(data.dataset.seed || '');
function build(animate) {
  box.innerHTML = '';
  const n = titles.length, count = Math.max(1, Math.ceil(n / PER));
  for (let k = 0; k < count; k++) {
    const group = titles.slice(k * PER, k * PER + PER), built = group.length / PER;
    const fig = document.createElement('figure'), canvas = document.createElement('canvas');
    const cap = document.createElement('figcaption');
    canvas.setAttribute('role', 'img');
    const kind = kindOf(k);
    canvas.setAttribute('aria-label', 'A ' + kind.name + ' made of the titles: ' + group.join('; '));
    cap.textContent = (k + 1) + ' · ' + kind.name + (built >= 1 ? '' : ' · ' + group.length + ' of ' + PER + ' read');
    fig.className = k % 2 ? 'right' : 'left';  // a zig-zag down the page, each facing the middle
    fig.append(canvas, cap); box.append(fig);
    draw(canvas, group, built, k % 2 === 1, animate, kind);
  }
}
function show(animate) {
  if (!on || built) return;
  built = true; build(animate);
  if (animate) requestAnimationFrame(() => box.classList.add('on'));  // fade in
  else box.classList.add('on');  // restored on load: just there
}
function set(value, animate) {
  on = value; built = false;
  sw.setAttribute('aria-pressed', on);
  box.hidden = hint.hidden = !on; list.hidden = on;
  box.classList.remove('on');
  show(animate);
  try { localStorage.setItem('papier', on ? '1' : '0'); } catch (e) {}
}
sw.addEventListener('click', () => set(!on, true));
let off = false; try { off = localStorage.getItem('papier') === '0'; } catch (e) {}
if (!off) set(true, false);  // on unless this reader turned it off
let t; window.addEventListener('resize', () => { clearTimeout(t); t = setTimeout(() => { if (on) build(false); }, 150); });
})();</script>""".replace("%PER%", str(PAPIER_PER))


def papier_seed(reader: dict) -> str:
    """The seed for a reader's own dinosaurs: from their id, which is their cookie, so it's hashed
    (the page can be public); "" without one (the demo: the fixed order)."""
    rid = reader.get("reader_id")
    return hashlib.sha256(f"papier:{rid}".encode()).hexdigest()[:16] if rid else ""


def papier(read: list[dict], seed: str = "") -> tuple[str, str, str]:
    """Papier-mâché for a Read list the page shows: the switch for the panel's header, the
    instructions (shown when it's on), and the dinosaurs' container with their titles and script.
    `seed` picks the reader's own herd (see papier_seed)."""
    if not read:
        return "", "", ""
    titles = [a["title"] or "Untitled" for a in sorted(read, key=lambda a: a["read_at"] or "")]  # oldest first
    data = json.dumps(titles).replace("</", "<\\/")
    made = len(read) // PAPIER_PER
    note = f"{made} dinosaur{'s' if made != 1 else ''} so far" if made else f"{PAPIER_PER - len(read)} more to your first dinosaur"
    switch = ('<button type="button" class="pm" id="papier-on" aria-pressed="false">'
              '<span class="track"><span class="knob"></span></span>Papier-mâché</button>')
    hint = f'<p class="papier-hint" hidden>Every {PAPIER_PER} articles you read makes a new dinosaur · {note}</p>'
    gallery = (f'<div class="papier" id="papier" hidden></div>'
               f'<script type="application/json" id="papier-data" data-seed="{html.escape(seed)}">{data}</script>{PAPIER_SCRIPT}')
    return switch, hint, gallery


# /demo: a public profile with made-up reads, to show every kind of dinosaur (29: five and one in progress). Nothing is stored: real
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
]


def demo_page() -> str:
    # some days two, some days none, and the last six days in a row: Read 29, Daily streak 6
    days = [2, 2, 4, 5, 5, 8, 9, 9, 11, 12, 13, 15, 16, 16, 17, 18, 19, 20, 22,
            24, 24, 25, 26, 26, 27, 28, 28, 29, 29]
    read = [{"news_id": f"DEMO{i}", "title": t, "url": "https://www.nytimes.com", "section": None,
             "read_at": f"2026-10-{d:02d}T{9 + i % 2 * 9}:00"} for i, (t, d) in enumerate(zip(DEMO_TITLES, days, strict=True))]
    creme = [{"news_id": "DEMO-CREME", "url": "https://www.nytimes.com",  # made up, like the reads
              "title": "Monarch Butterflies Return to California’s Coast in Their Biggest Numbers in a Decade"}]
    return profile_page({"name": "Justin", "history_public": 1}, read, creme, owner=False, today=date(2026, 10, 29))


PIN_MARK = '<img class="pinmark" src="/icon.png" alt="PushPin">'

COOKIE = "pushpin_reader"


def serve_cards(store: Store, port: int, block: bool = False, caps: dict | None = None,
                verify_apple=None) -> ThreadingHTTPServer:
    """The phone-facing endpoints. The reader is identified by the `pushpin_reader` cookie.
    `caps` maps each volume to its daily cap (configs/notifier.yml), shown on the buttons.

      GET  /p/<push_id>        pins the push: records the open (creating the reader on first visit)
                               and shows their profile with its article on top: Read / Put in Crème
      POST /read/<push_id>     Read was tapped on it (sent as a beacon; the link itself goes to NYT)
      GET  /start              the welcome page: choose light / medium / heavy (creates the reader)
      POST /volume             body "light", "medium" or "heavy": pushes a day
      POST /name               body: the reader's first name
      GET  /me                 this reader's profile: Crème, articles read, public toggle, pushes a day
      POST /favorite/<news_id> body "on" or "off": make an article they pinned their Crème (replaces the last)
      POST /remove/<news_id>   delete an article they pinned or read from their profile (and Crème)
      POST /public             body "on" or "off": show the count and list on their public link
      GET  /u/<slug>           a reader's public profile
      GET  /stats              {"readers": n} for autoscaling (KEDA), only with header
                               X-Api-Key = $CARDS_STATS_TOKEN; 404 when that isn't set
      GET  /demo               a public profile with 29 made-up reads (5 dinosaurs and one in progress); stores nothing
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
      POST   /api/favorite/<news_id>  {"on": true | false}: the Crème
      DELETE /api/articles/<news_id>  delete from Pinned / Read (and the Crème)
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
                "creme": store.favorites(reader_id), "pinned": store.pinned_unread(reader_id), "read": read,
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
            return profile_page(store.reader(reader_id), store.articles_read(reader_id), store.favorites(reader_id),
                                owner=True, share_url=share, pinned=pinned, caps=caps, today=date.fromisoformat(store.now()[:10]),
                                unread=store.pinned_unread(reader_id))

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
            elif (method, what) == ("POST", "favorite"):
                ok = store.set_favorite(reader_id, arg, on=body.get("on", True) is not False) == "ok"
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
            if route == "favorite":
                ok = store.set_favorite(reader_id, push_id, on=body.strip() != "off") == "ok"
            elif route == "remove":
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
                              **({"on": body.strip() != "off"} if route in ("favorite", "public") else {}),
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
                self._send(200, profile_page(other, store.articles_read(rid), store.favorites(rid), owner=False,
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
