# Fireship Digest

I've never skipped a fireship video and I always find those videos useful so this is just a little something to
ease up things. 
A daily routine that turns new [Fireship](https://www.youtube.com/@Fireship) videos into simple, lightly
researched notes in a small private web app, so I keep up with web dev and AI news without watching everything.

Every day at 11:00 it picks two videos (new uploads first, then a backlog of the 20 most recent), reads their
captions, checks a few facts, saves one note per video to the app, and adds a reminder to read them.

## The app

A phone-friendly page with five tabs:

- **Today**: the two newest notes as big cards, then everything still unread.
- **All videos**: every note as a card, filtered by topic (AI, Web dev, Security, Business, Tools, Drama), by
  search, or by "unread only".
- **Weeks**: a short wrap-up every Sunday.
- **Glossary**: every term from every note, A to Z, in plain words.
- **Tools**: everything the videos mentioned, with official links.

Each note has its own page with colour-labelled sections: TL;DR, What was said, Key moments (links that jump
to that second of the video), Claims (fact, opinion or joke), Mentioned, Glossary, For me as a developer, and
What happened since (older videos only). A "Mark as read" button feeds the unread count and a reading streak,
and an "Ask Claude" box answers questions about the video in simple words.

The notes are written in simple English: short sentences, everyday words, every tech term explained right where
it appears, and one "think of it like..." comparison per video.

## How it works

```
11:00  Claude Code scheduled task (local, on my Mac), following ROUTINE.md
  │
  ├─ python3 fireship.py prepare
  │     ├─ yt-dlp lists the channel's Videos tab (Shorts are skipped)
  │     ├─ state.json keeps the queue: new uploads first, then the backlog oldest first
  │     ├─ yt-dlp downloads English captions and the description (never the video)
  │     ├─ SponsorBlock gives the sponsor segment, which is cut out and replaced by a marker
  │     └─ prints a JSON manifest with today's videos and their clean transcripts
  │
  ├─ for each video the model reads the transcript, fixes misheard names, runs
  │     python3 fireship.py hn "<name>" ...   (free Hacker News lookup near the upload date)
  │  and does 1 to 3 web searches and at most 2 page reads
  │
  ├─ the model writes draft.json: plain fields only (TL;DR, paragraphs, claims, links, ...)
  ├─ python3 fireship.py check draft.json
  │     validates every field, the 450-word and 1,000-word limits and the links,
  │     then prepares one JSON file per database document
  ├─ ONE database write (a batch that points at those files, so nothing is retyped)
  ├─ python3 fireship.py done --from draft.json      marks the videos done, deletes the transcripts
  └─ python3 fireship.py remind --reads N            adds "Read Fireship Digest: 2 new, 3 unread"
                                                     to Apple Reminders at 13:00; iCloud brings it to the iPhone
```

The model writes content; the script owns format, validation and bookkeeping. That keeps the daily run short,
stops formatting mistakes before they reach the app, and enforces the length limits in code.

| File | Purpose |
| --- | --- |
| `fireship.py` | Queue, captions, sponsor removal, Hacker News lookup, draft check, done marking, reminders |
| `ROUTINE.md` | The instructions the scheduled run follows: steps, research budget, draft format, language rules |
| `page/index.html` | The web app (vanilla JS); it reads the notes from the app's own small database |
| `state.json`, `transcripts/`, `draft.json`, `outbox/` | Local working data, not in git |

## Design decisions

- **Two videos a day from a 20-video backlog.** New uploads always go first. Fireship posts about three videos a
  week, so the backlog clears in about two weeks.
- **Idempotent runs.** A second run on the same day re-offers only unfinished videos and never picks extra
  ones. A video that fails (no captions yet) is retried on later runs, up to three times.
- **Self-repairing downloads.** When yt-dlp fails in a way that looks like a YouTube change, the script runs
  `brew upgrade yt-dlp` once and retries before it reports a problem.
- **Sponsor reads are removed before the model reads anything**, using SponsorBlock's crowd-sourced segments.
- **A hard research budget** (3 searches and 2 page reads per video) keeps the cost small and predictable.
- **Hacker News through code, not searches.** HN search needs every word of a query to match, so the helper
  takes short names, tries them in order, and keeps only threads whose title names the topic.

## Cost

Measured on the Claude Pro plan: one run (two videos, Sonnet) takes about 2 minutes and uses about 2% of the
5-hour usage window, and less than 1 point of the weekly limit.

## Setup

Requirements: macOS, Python 3, [yt-dlp](https://github.com/yt-dlp/yt-dlp) (`brew install yt-dlp`), and the
Claude desktop app with Claude Code (for local scheduled tasks).

1. Publish `page/index.html` as a Claude artifact with the `db` and `sample` capabilities, then save its link:
   `python3 fireship.py set-page <artifact link>`
2. Run `python3 fireship.py prepare` once. It queues the last 20 videos and shows the first two.
3. In the Claude app, Code tab, Routines: create a local routine in this folder that runs daily with the prompt
   "Read ROUTINE.md in this folder and follow it exactly", and set its model (Sonnet) and permission mode.
4. Click "Run now" once. macOS asks once to let Claude control Reminders.

## Privacy

Only captions and descriptions are downloaded, never the video. Transcripts stay on the local machine and are
deleted once the note is saved. The app is private to its owner; nothing is republished.

## Limitations

- The Mac must be awake with the Claude app open at 11:00. A missed run catches up once when it wakes.
- Auto-captions mishear names; the routine corrects them from the title, the description and its searches,
  but mistakes can slip through.

## Credits

[yt-dlp](https://github.com/yt-dlp/yt-dlp) for captions, [SponsorBlock](https://sponsor.ajay.app) for sponsor
segments, the [HN Search API](https://hn.algolia.com/api) and the
[official Hacker News API](https://github.com/HackerNews/API) for discussions.
