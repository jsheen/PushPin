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
  - **Crème**: the one article they've starred as their favorite (★). Tapping ☆ on any read
    article makes it the Crème instead.
  - **Pinned**: pushes they tapped but haven't read yet. These are always private. Tapping a
    headline reads it and moves it to Read; × deletes it.
  - **Read**: the count and daily streak, shown as papier-mâché dinosaurs made from the headlines
    (one every 5 reads).
  - how many pushes a day they want.

This sample reader has pinned 9 recent pushes, read 6 of them, and starred one as their Crème.
The data was set up on a copy of the database, not on a real reader.

<img src="screens/push-tapped.png" width="300" alt="Pinning a push: the pinned article with a Read button, then the reader's profile">

## 3. The demo profile

`/demo` is a public profile with 29 made-up reads (the titles are invented). It stores nothing.
It opens on a title card and goes to the profile when tapped.

<img src="screens/demo-splash.png" width="300" alt="Demo title card: Justin's, Read 29, Daily streak 6, Tap to continue">
<img src="screens/demo-profile.png" width="300" alt="Demo profile: Crème article, Read 29, papier-mâché dinosaurs made of headlines">

Every 5 reads makes a papier-mâché dinosaur out of the headlines. The full page has all five, plus
the sixth in progress: [demo-profile-full.png](screens/demo-profile-full.png).

## Before the first push: the welcome page

The welcome push opens `/start`, where the reader picks a volume. Nothing is pressed until they
choose, and then it goes on to the profile.

<img src="screens/welcome.png" width="300" alt="Welcome page: How many pushes a day? Light up to 2, Medium up to 4, Heavy up to 8">

## Regenerating these

Run a card server on a copy of the database so a screenshot doesn't record a real open, then
screenshot each page with headless Chrome at `--window-size=390,844 --force-device-scale-factor=2`.
Put each page in a 390 px-wide `<iframe>`, because headless Chrome won't lay out a window that
narrow.
