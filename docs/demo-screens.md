# What the phone shows

Screens from the web version of PushPin (ntfy push → card server pages) at iPhone width. The pages
are real screenshots of the card server; the lock screen is a mockup built from a real push sent on
November 13, 2026.

## 1. A push arrives

The PushPin icon, the headline as the title and the abstract as the body.

<img src="screens/push-notification.png" width="300" alt="Lock screen with a PushPin notification">

*Mockup: how iOS draws a notification depends on the ntfy app and the phone's settings.*

## 2. Pinning the push

Tapping a push opens `/p/<push_id>`, which records the open (the click the model learns from) and
shows the article pinned on top of the reader's profile. **Read** opens it on nytimes.com. Below
are **Pinned** (tapped but not read yet, always private), **Read** with the count, daily streak and
the day's pictures (section 3), and pushes a day. This sample reader, set up on a copy of the
database, has read 6 of the 10 pushes they opened that day.

<img src="screens/push-tapped.png" width="300" alt="Pinning a push: the pinned article with a Read button, then the reader's profile">

## 3. The demo profile

`/demo` is a made-up Medium reader (4 pushes a day): 63 reads over 18 days, with invented titles
and abstracts. It stores nothing.

<img src="screens/demo-splash.png" width="300" alt="Demo title card: Justin's, Read 63, Daily streak 6, Tap to continue">
<img src="screens/demo-profile.png" width="300" alt="Demo profile: Read 63, a grid of small papier-mâché pictures, one a day: a snowman, dinosaurs, a heart, a star, a moon, a tree and more, a few unfinished">

Each day's pushes make one papier-mâché picture, a common symbol (snowman, heart, star, moon...)
or a dinosaur, the same shape for everyone that day, written in the titles and abstracts read that
day, each used once. The type is sized so the text fills the whole shape, thin parts included. A
picture is revealed from one side as the day's pushes are read; while the day is on, nothing shows
of the rest, so it's a guess what it will be. Once the day is over, an unfinished picture gets the
grey outline of what it could have been under the part that was read (Oct 15 · 3/4), and a day
with nothing read is just the outline (Oct 23 · 0/4). Today is still a guess: Oct 29 has 2 of the
3 pushes sent so far read (Oct 29 · 2/3). A picture counts only pushes already sent, so it's
finished once everything sent that day is read; if another push goes out later, it's unfinished
again until that one is read. Today, with nothing read yet, says "Start reading to make today's
word art".

## 4. Sharing a picture

Tapping a picture makes it a sticker (an unfinished one from today shows just the part revealed so
far; one from a day that's over is the whole shape's die-cut, blank white where it was missed):
everything read that day, every title with its whole abstract, written into the shape on a white
die-cut with a soft shadow and the PushPin pin in the corner, as a PNG with a transparent
background. It looks the same in any chat, in light or dark mode, and the words make each one the
reader's own. **Share** opens the phone's share sheet with the image, **Copy** puts it on the
clipboard to paste into a message, and **Save** saves it (on a phone it opens the share sheet,
where Save Image puts it in Photos; a web page can't save to Photos directly).

<img src="screens/share-sheet.png" width="300" alt="Share sheet on the demo profile: the snowman sticker made of the day's articles, with Share, Copy and Save buttons">

The demo's snowman and Brontosaurus, each made of the four articles read that day:

<img src="screens/sticker-snowman.png" width="260" alt="Snowman sticker: four articles' titles and abstracts written into a snowman on a white die-cut, with the PushPin pin">
<img src="screens/sticker-brontosaurus.png" width="420" alt="Brontosaurus sticker: four articles' titles and abstracts written into a Brontosaurus on a white die-cut, with the PushPin pin">

Days that are over and weren't finished share what was missed: Oct 15 (3 of 4 read) and Oct 23
(nothing read):

<img src="screens/sticker-unfinished.png" width="420" alt="Unfinished day sticker: the read articles in part of the shape, the rest blank white die-cut">
<img src="screens/sticker-missed.png" width="200" alt="Missed day sticker: a blank white moon die-cut">

Today's unfinished picture (Oct 29, 2 of 3 read) shares just the part revealed so far:

<img src="screens/sticker-today.png" width="300" alt="Today's unfinished sticker: only the part revealed so far, die-cut around it">

## Before the first push: the welcome page

The welcome push opens `/start`, where the reader picks how many pushes a day, then goes on to the
profile.

<img src="screens/welcome.png" width="300" alt="Welcome page: How many pushes a day? Light up to 2, Medium up to 4, Heavy up to 8">

## Regenerating these

Run a card server on a copy of the database, so a screenshot doesn't record a real open, and drive
headless Chrome over the DevTools protocol: device metrics 390 × 844 at 2x with `mobile: true`, dark
mode. For the share sheet, stub `navigator.canShare` and `navigator.share` (headless Chrome hides the
button without them) and click a `.papier button.dino`; the sticker is the dialog `<img>`'s data URL.
