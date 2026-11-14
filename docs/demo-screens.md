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
all of them are read (Oct 15 · 3/4). A day with nothing read says "Start reading to make this
day's word art" (Oct 23 · 0/4).

## 4. Sharing a picture

Tapping a finished picture opens it as text to paste into any chat: the shape spelled out in
everything read that day, every title with its whole abstract, from the first title to the final
period, with 📌 in the top left corner. The shape grows to fit the words, so a busy day makes a
bigger picture. Each picture is unique to its reader and day. **Share** opens the phone's share
sheet; **Copy** copies it.

<img src="screens/share-sheet.png" width="300" alt="Share sheet on the demo profile: a snowman spelled out in the letters of the day's articles, with Share and Copy buttons">

The demo's snowman and Brontosaurus, each made of the four articles read that day:

```
📌             TheQuietR
               eturnofth
            eNightTrain—Sle
             epercarsarero
              llingagaina
              str ave lers
              tradeairpor
               tsforaslo
wer              wayacr             oss
 theco        untry.·What        theTi
    deLea   vesBehind—Beach   combe
        rscatalogtheshells,glass
           anddriftwoodthate
           verystormbringsas
            hore.·ACityLear
             nstoLoveItsRa
              in—Afteryear
           soffloods,onecity
          builtparksthatsoaku
         pstorms,andnowrainyda
        ysbringpeopleoutside.·T
        heLastTypewriterRepairm
         aninOhio—Hehasfixedtho
         usandsofmachinesandst
          illkeepsadrawerofri
            bbonsnobodymake
               sanymore.
```

```
📌                                             Insidethe
                                             RacetoMapthe
                                            OceanFloor—Sc
                                           ientistsands
                                         onarshipsa
                          rechart      ingthevasts
                   eabedthatremainslessknownthan
               themoon.·WhyEveryoneIsBakingBre
           adAgain—Sourdoughstartersarepassed
        handtohandashomebakersrediscoveraslo
      w,patientcraft.·TheSecretLifeofSubwayPi
    geons—Thebirdsthatridethetrainsknowthesc
  hedulesbetterthanmanycommutersdo.·HowaSma
 llTownSavedItsLibrary—Whenthecountycutitsb
udg                 et,residentsheldb akesa
                    lesandrais   eden ought
                    okeep thed   oors open.
```

## Before the first push: the welcome page

The welcome push opens `/start`, where the reader picks how many pushes a day, then goes on to the
profile.

<img src="screens/welcome.png" width="300" alt="Welcome page: How many pushes a day? Light up to 2, Medium up to 4, Heavy up to 8">

## Regenerating these

Run a card server on a copy of the database, so a screenshot doesn't record a real open, and drive
headless Chrome over the DevTools protocol: device metrics 390 × 844 at 2x with `mobile: true`, dark
mode. For the share sheet, stub `navigator.share` (headless Chrome hides the button without it) and
click a `.papier .dino`; the shared text is the dialog's `<pre>`.
