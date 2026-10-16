# Fireship Digest

A daily routine that turns new [Fireship](https://www.youtube.com/@Fireship) videos into detailed, lightly
researched notes in one running document, so I keep up with web dev and AI news without watching everything.

Every day at 11:00 it picks two videos (new uploads first, then a backlog of the 20 most recent), reads their
captions, checks a few facts, and adds one entry per video to the top of a Claude Doc.

## What an entry looks like

Each entry is 800 to 1,000 words:

- **Meta line**: watch link, length, new upload or backlog, where the sponsor read was skipped, the Hacker News
  thread about the story and its points.
- **TL;DR** in three lines.
- **What was said**: the video's content in order, in plain words.
- **Claims**: the main claims marked fact, opinion or joke (Fireship is sarcastic, so this matters).
- **Mentioned**: tools, companies and papers with their official links.
- **Glossary** of new terms.
- **For me as a developer**: what changes for a backend developer, and how it could come up in an interview.
- **What happened since**: for older backlog videos, the follow-up news.

On Sundays a short wrap-up of the week goes on top.

## How it works

```
11:00  Claude Code scheduled task (local, on my Mac)
  │
  ├─ python3 fireship.py prepare
  │     ├─ yt-dlp lists the channel's Videos tab (Shorts are skipped)
  │     ├─ state.json keeps the queue: new uploads first, then the backlog oldest first
  │     ├─ yt-dlp downloads English captions and the description (never the video)
  │     ├─ SponsorBlock gives the sponsor segment, which is cut out and replaced by a marker
  │     └─ prints a JSON manifest with today's videos and their clean transcripts
  │
  ├─ for each video (the model, following ROUTINE.md)
  │     ├─ fixes names the auto-captions misheard
  │     ├─ python3 fireship.py hn "<name>" ...   free Hacker News lookup near the upload date
  │     ├─ 1 to 3 web searches, at most 2 page reads
  │     ├─ writes the entry into the Claude Doc (one insert under the lead line)
  │     └─ python3 fireship.py done <id> --tldr "..."   marks it done, deletes the transcript
  │
  └─ desktop notification when the run ends
```

The script does everything mechanical: it never forgets, never makes things up and costs no model usage. The
model only does what needs judgment: summarising, researching and writing.

| File | Purpose |
| --- | --- |
| `fireship.py` | Queue, captions, sponsor removal, Hacker News lookup, done marking |
| `ROUTINE.md` | The instructions the scheduled run follows: steps, research budget, entry template, rules |
| `state.json` | The queue and the doc's ids (local only, not in git) |
| `transcripts/` | Clean transcripts waiting to be summarised (local only, deleted after use) |

## Design decisions

- **Two videos a day from a 20-video backlog.** New uploads always go first. Fireship posts about three videos a
  week, so the backlog clears in about two weeks.
- **Idempotent runs.** A second run on the same day (for example a catch-up after the Mac wakes) re-offers only
  unfinished videos and never picks extra ones. A video that fails (no captions yet) is retried on later runs, up
  to three times.
- **Sponsor reads are removed before the model reads anything**, using SponsorBlock's crowd-sourced segments.
  Less noise and fewer tokens.
- **A hard research budget** (3 searches and 2 page reads per video) keeps the cost small and predictable.
- **Hacker News through code, not searches.** HN search needs every word of a query to match, so the helper takes
  short names, tries them in order, and keeps only threads whose title names the topic. Top comments come from
  the official HN API, which lists them in ranked order.

## Cost

Measured on the Claude Pro plan: one run (two videos, Sonnet) takes 1 to 2.5 minutes and uses about 2% of the
5-hour usage window, and less than 1 point of the weekly limit.

## Setup

Requirements: macOS, Python 3, [yt-dlp](https://github.com/yt-dlp/yt-dlp) (`brew install yt-dlp`), and the
Claude desktop app with Claude Code (for local scheduled tasks) and the Claude Docs connector.

1. Clone this repo outside any synced folder if you can.
2. Create an empty Claude Doc with a title and one lead line, then save where entries should go:
   `python3 fireship.py set-doc <doc id> <body node id> <lead block id>`
3. Run `python3 fireship.py prepare` once. It queues the last 20 videos and shows the first two.
4. In the Claude app, Code tab, Routines: create a local routine in this folder that runs daily with the prompt
   "Read ROUTINE.md in this folder and follow it exactly", and set its model (Sonnet) and permission mode.
5. Click "Run now" once to check it end to end.

Keep `yt-dlp` up to date: YouTube changes often break older versions.

## Privacy

Only captions and descriptions are downloaded, never the video. Transcripts stay on the local machine and are
deleted once the entry is written. The notes are personal summaries; nothing is republished.

## Limitations

- The Mac must be awake with the Claude app open at 11:00. A missed run catches up once when it wakes.
- Auto-captions mishear names; the routine corrects them from the title, the description and its searches,
  but mistakes can slip through.
- One running document grows over time. Monthly tabs are a likely next step.

## Credits

[yt-dlp](https://github.com/yt-dlp/yt-dlp) for captions, [SponsorBlock](https://sponsor.ajay.app) for sponsor
segments, the [HN Search API](https://hn.algolia.com/api) and the
[official Hacker News API](https://github.com/HackerNews/API) for discussions.
