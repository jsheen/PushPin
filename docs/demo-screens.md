# What the phone shows

Screens from the web version of PushPin (ntfy push → card server pages), at iPhone width
(390 pt). The pages are real screenshots of the card server. The lock screen is a mockup that
uses a real push the notifier sent on November 13, 2026.

## 1. A push arrives

The notification shows the PushPin icon, the article's headline as the title, and its abstract as
the body.

<img src="screens/push-notification.png" width="300" alt="Lock screen with a PushPin notification">

*Mockup: how iOS draws a notification depends on the ntfy app and the phone's settings.*

## 2. Pinning the push

Tapping a push pins it. The tap opens `/p/<push_id>`, which records the open (the click the model
learns from) and shows the reader's profile with the article pinned on top:

- **Read** opens the story on nytimes.com and moves the article to Read.
- Below it is the reader's profile:
  - the privacy line, the public toggle, and an optional first name;
  - **Pinned**: pushes they tapped but haven't read yet. These are always private. Tapping a
    headline reads it and moves it to Read; × deletes it.
  - **Read**: the count and daily streak, shown as papier-mâché pictures: one small picture a
    day, made of that day's read headlines and finished when all of the day's pushes are read.
  - how many pushes a day they want.

This sample reader has pinned 9 recent pushes and read 6 of the 10 they opened that day, so the
day's picture is 6/10 of the way there.
The data was set up on a copy of the database, not on a real reader.

<img src="screens/push-tapped.png" width="300" alt="Pinning a push: the pinned article with a Read button, then the reader's profile">

## 3. The demo profile

`/demo` is a public profile with 29 made-up reads (the titles are invented). It stores nothing.
It opens on a title card and goes to the profile when tapped.

<img src="screens/demo-splash.png" width="300" alt="Demo title card: Justin's, Read 29, Daily streak 6, Tap to continue">
<img src="screens/demo-profile.png" width="300" alt="Demo profile: Read 29, a grid of small papier-mâché pictures, one a day: a snowman, dinosaurs, a heart, a star, a moon, a tree and more, a few unfinished">

Each day's pushes make one small papier-mâché picture out of the headlines read that day. The
pictures are common symbols (snowman, heart, star, sun, moon, pine tree, umbrella, house, cat,
fish, apple, cloud, lightning bolt, coffee cup, flower, music note, balloon), taking turns with the
dinosaurs. A picture fills in from one side as the day's pushes are read and is finished only when
all of them are. An unfinished one is pencilled in, with how many were read under its date (Oct 9
· 2/3); a day with nothing read is a blank outline (Oct 14 · 0/2). The demo has 22 days, 18 of them
finished, and they all fit on one screen. A faint silhouette sits under the words so each shape
reads at this size.

## 4. Sharing a picture

Tapping a finished picture opens it as text, ready to paste into any chat: the shape drawn with the
day's headlines' letters on a 32-character grid, with plain spaces around it and 📌 in the top left
corner, on the top line. The spaces between words are dropped inside the shape so it has no holes.
**Share** opens the phone's share sheet with the text, and **Copy** copies it. Browsers without a
share sheet only show Copy. Unfinished pictures can't be shared yet.

It lines up in a monospace font. In a chat's proportional font the rows shift a little, and that's
fine.

<img src="screens/share-sheet.png" width="300" alt="Share sheet on the demo profile: a snowman spelled out in the letters of the day's headlines, with Share and Copy buttons">

What two of the demo pictures share as, the snowman and the heart:

```
📌          WhattheT
            ideLeave
          sBehind·TheQ
           uietReturn
           oftheNight
            Train·Wh
at           theTid           eL
 eave      sBehind·Th      eQui
     etReturnoftheNigh tTra
        in·WhattheTideLe
         avesBehind·The
          QuietReturno
           ftheNightT
         rain·WhattheTi
        deLeavesBehind·T
       heQuietReturnofthe
       NightTrain·Whatthe
       TideLeavesBehind·T
        heQuietReturnoft
         heNightTrain·W
           hattheTid
```

```
📌  TheLastTyp    ewriterRep
  airmaninOhio·InsidetheRaceto
MaptheOceanFloor·TheLastTypewrit
erRepairmaninOhio·InsidetheRacet
oMaptheOceanFloor·TheLastTypewri
terRepairmaninOhio·InsidetheRace
 toMaptheOceanFloor·TheLastType
   writerRepairmaninOhio·Insi
     detheRacetoMaptheOcean
      Floor·TheLastTypewri
        terRepairmaninOh
          io·Insidethe
           RacetoMapt
             heOcea
               nF
```

## Before the first push: the welcome page

The welcome push opens `/start`, where the reader picks a volume. Nothing is pressed until they
choose, and then it goes on to the profile.

<img src="screens/welcome.png" width="300" alt="Welcome page: How many pushes a day? Light up to 2, Medium up to 4, Heavy up to 8">

## Regenerating these

Run a card server on a copy of the database so a screenshot doesn't record a real open, then
screenshot each page with headless Chrome at `--window-size=390,844 --force-device-scale-factor=2`.
Put each page in a 390 px-wide `<iframe>`, because headless Chrome won't lay out a window that
narrow.

The share screen needs a tap, so it was driven over the DevTools protocol instead. Set the device
metrics to 390 × 844 at 2x with `mobile: true` and the dark color scheme, open `/demo`, and click the
splash. Stub `navigator.share`, because headless Chrome doesn't have it and would hide the button.
Then click a `.papier .dino`. The shared text is the dialog `<pre>`'s text.

The other screenshots that show the pictures (`push-tapped.png` and `demo-profile.png`) were taken
the same way, in dark mode. `push-tapped.png` uses the page's full height as the window.
