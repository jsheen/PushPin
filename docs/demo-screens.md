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
or a dinosaur, written in the titles and abstracts read that day, each used once. The type is
sized so the text fills the whole shape, thin parts included. A picture is revealed from one side
as the day's pushes are read, with no outline of the rest, so it's a guess what it will be until
all of them are read (Oct 15 · 3/4). A day that's over with nothing read shows the grey outline of
what it could have been (Oct 23 · 0/4); today, with nothing read yet, says "Start reading to make
today's word art".

## 4. Sharing a picture

Tapping a finished picture makes it a sticker: everything read that day, every title with its
whole abstract, written into the shape on a white die-cut with a soft shadow and the PushPin pin
in the corner, as a PNG with a transparent background. It looks the same in any chat, in light or
dark mode, and each one is unique to its reader and day. **Share** opens the phone's share sheet
with the image, **Copy** puts the image on the clipboard to paste into a message, and **Save**
downloads it. On iOS 17+, long-press it in Photos and choose Add Sticker to use it as a sticker in
Messages.

<img src="screens/share-sheet.png" width="300" alt="Share sheet on the demo profile: the snowman sticker made of the day's articles, with Share, Copy and Save buttons">

The demo's snowman and Brontosaurus, each made of the four articles read that day:

<img src="screens/sticker-snowman.png" width="260" alt="Snowman sticker: four articles' titles and abstracts written into a snowman on a white die-cut, with the PushPin pin">
<img src="screens/sticker-brontosaurus.png" width="420" alt="Brontosaurus sticker: four articles' titles and abstracts written into a Brontosaurus on a white die-cut, with the PushPin pin">

## Before the first push: the welcome page

The welcome push opens `/start`, where the reader picks how many pushes a day, then goes on to the
profile.

<img src="screens/welcome.png" width="300" alt="Welcome page: How many pushes a day? Light up to 2, Medium up to 4, Heavy up to 8">

## Regenerating these

Run a card server on a copy of the database, so a screenshot doesn't record a real open, and drive
headless Chrome over the DevTools protocol: device metrics 390 × 844 at 2x with `mobile: true`, dark
mode. For the share sheet, stub `navigator.canShare` and `navigator.share` (headless Chrome hides the
button without them) and click a `.papier button.dino`; the sticker is the dialog `<img>`'s data URL.
