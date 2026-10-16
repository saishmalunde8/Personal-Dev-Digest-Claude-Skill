#!/usr/bin/env python3
"""Fireship digest helper: keeps the video queue and prepares clean transcripts.

The daily routine calls:
  python3 fireship.py prepare [--fill]   pick today's items (videos; news + a classic when no backlog), print a manifest
  python3 fireship.py hn NAME [NAME ...] --around YYYY-MM-DD   Hacker News threads near the upload date
  python3 fireship.py check draft.json   validate the day's entries and prepare them for one database write
  python3 fireship.py done --from draft.json   mark the draft's videos as summarised
  python3 fireship.py remind --reads N   add an Apple Reminders reminder (N = documents in the page's reads)
One-time setup:
  python3 fireship.py set-page URL       the web app the entries are written to (kept in state.json)
"""
import argparse
import html
import json
import random
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state.json"
TRANSCRIPTS = ROOT / "transcripts"
OUTBOX = ROOT / "outbox"
BACKUP = ROOT.parent / f"{ROOT.name}-backup"  # a copy of every saved note, outside the repo
CHANNEL = "https://www.youtube.com/@Fireship/videos"
BACKLOG_SIZE = 20
PER_DAY = 2
MAX_ATTEMPTS = 3
MIN_SECONDS = 90  # shorter uploads are treated as Shorts and skipped
YTDLP = shutil.which("yt-dlp") or "/opt/homebrew/bin/yt-dlp"
BREW = shutil.which("brew") or "/opt/homebrew/bin/brew"
TAGS = ["AI", "Web dev", "Security", "Business", "Tools", "Drama"]
CLAIM_TYPES = {"Fact", "Opinion", "Joke"}
NEWS_MIN_POINTS = 300
# A story counts as dev or AI news when its title or site matches one of these.
NEWS_TOPIC = re.compile(
    r"\b(ai|llms?|gpt|claude|gemini|openai|anthropic|neural|machine learning|deep learning|agents?|"
    r"open[- ]source|open[- ]weights?|github|git|linux|kernel|unix|bsd|rust|python|javascript|typescript|"
    r"node\.?js|deno|bun|react|webassembly|wasm|browsers?|chrome|firefox|apis?|databases?|postgres(ql)?|sql|"
    r"sqlite|redis|compilers?|programming|programmers?|coding|developers?|software|security|"
    r"vulnerabilit(y|ies)|exploits?|spyware|malware|cve|hackers?|cloud|aws|docker|kubernetes|"
    r"self[- ]host(ed|ing)?|servers?|wayland|gpus?|cpus?|nvidia)\b|code|\.ai/|github\.com|huggingface\.co|arxiv\.org",
    re.I)
NOT_A_YTDLP_BUG = ("Video unavailable", "Private video", "has been removed", "not a valid URL")
upgraded = False


def ytdlp(*args):
    """Run yt-dlp. YouTube changes often break old versions, so on a failure update it once and retry."""
    global upgraded
    while True:
        run = subprocess.run([YTDLP, "--no-warnings", "--no-update", *args],
                             capture_output=True, text=True, timeout=300)
        if run.returncode == 0:
            return run.stdout
        error = run.stderr.strip()[-400:] or f"yt-dlp exited with {run.returncode}"
        if upgraded or any(s in error for s in NOT_A_YTDLP_BUG) or not Path(BREW).exists():
            raise RuntimeError(error)
        upgraded = True
        subprocess.run([BREW, "upgrade", "yt-dlp"], capture_output=True, text=True, timeout=900)


def list_channel(count):
    """Newest-first list of uploads on the channel's Videos tab (Shorts live on a separate tab)."""
    videos = []
    for line in ytdlp("--flat-playlist", "--playlist-end", str(count),
                      "--print", "%(id)s\t%(duration)s\t%(title)s", CHANNEL).splitlines():
        vid, duration, title = line.split("\t", 2)
        if duration != "NA" and float(duration) < MIN_SECONDS:
            continue
        videos.append({"id": vid, "title": title})
    return videos


def sync(state):
    """Add uploads we have not seen. The first run queues the last BACKLOG_SIZE videos."""
    known = state["videos"]
    listing = list_channel(BACKLOG_SIZE + 10)
    if not known:
        fresh, kind = listing[:BACKLOG_SIZE], "backlog"
    else:
        fresh, kind = [], "new"
        for v in listing:  # newest first: stop at the first video we already know
            if v["id"] in known:
                break
            fresh.append(v)
    seq = max((v["seq"] for v in known.values()), default=0)
    for v in reversed(fresh):  # oldest first, so seq follows upload order
        seq += 1
        known[v["id"]] = {"title": v["title"], "kind": kind, "seq": seq, "status": "pending",
                          "attempts": 0, "first_seen": date.today().isoformat()}


def candidates(state, fill):
    """New uploads first (oldest new first), then the backlog from its oldest video.
    With fill, act as if the backlog were empty, so free slots go to news and a classic."""
    open_ = [(vid, v) for vid, v in state["videos"].items()
             if v["status"] in ("pending", "failed") and v["attempts"] < MAX_ATTEMPTS
             and not (fill and v["kind"] != "new")]
    return sorted(open_, key=lambda item: (item[1]["kind"] != "new", item[1]["seq"]))


def pick_news(state):
    """Today's most-upvoted dev or AI story on Hacker News (300+ points, last 36 hours, not covered yet)."""
    since = int(datetime.now().timestamp()) - 36 * 3600
    params = urllib.parse.urlencode({"tags": "story", "hitsPerPage": 50,
                                     "numericFilters": f"points>={NEWS_MIN_POINTS},created_at_i>{since}"})
    hits = get_json(f"https://hn.algolia.com/api/v1/search?{params}")["hits"]
    for h in sorted(hits, key=lambda h: h.get("points") or 0, reverse=True):
        vid = f"news-{h['objectID']}"
        if h.get("url") and vid not in state["videos"] and NEWS_TOPIC.search(f"{h['title']} {h['url']}"):
            return vid, {"title": h["title"], "kind": "news", "seq": 0, "status": "pending", "attempts": 0,
                         "first_seen": date.today().isoformat(),
                         "story": {"url": h["url"], "points": h["points"], "comments": h.get("num_comments", 0),
                                   "hn_url": f"https://news.ycombinator.com/item?id={h['objectID']}",
                                   "posted": h["created_at"][:10]}}
    return None


def pick_classic(state):
    """A random Fireship "... in 100 Seconds" video not covered yet. The list is refreshed weekly."""
    if (date.today() - date.fromisoformat(state.get("classics_on", "2000-01-01"))).days >= 7:
        state["classics"] = [v for v in list_channel(2000) if "in 100 seconds" in v["title"].lower()]
        state["classics_on"] = date.today().isoformat()
    fresh = [c for c in state.get("classics", []) if c["id"] not in state["videos"]]
    if not fresh:
        return None
    c = random.choice(fresh)
    return c["id"], {"title": c["title"], "kind": "classic", "seq": 0, "status": "pending", "attempts": 0,
                     "first_seen": date.today().isoformat()}


def sponsor_segments(vid):
    url = (f"https://sponsor.ajay.app/api/skipSegments?videoID={vid}"
           "&categories=%5B%22sponsor%22,%22selfpromo%22%5D")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "fireship-digest"}),
                                    timeout=15) as resp:
            return sorted(s["segment"] for s in json.load(resp))
    except urllib.error.HTTPError as err:
        if err.code == 404:  # SponsorBlock answers 404 when a video has no segments
            return []
        return None
    except OSError:
        return None  # unknown: SponsorBlock unreachable


def get_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": "fireship-digest"})
    with urllib.request.urlopen(request, timeout=15) as resp:
        return json.load(resp)


def hn(queries, around):
    """Top 2 Hacker News stories from 10 days before to 3 days after the upload, plus 3 top comments.

    HN search needs every word of a query to match, so callers pass short names and the first one
    with results wins.
    """
    day = datetime.fromisoformat(around).replace(tzinfo=timezone.utc).timestamp()
    hits = []
    for query in queries:
        params = urllib.parse.urlencode({
            "query": query, "tags": "story", "hitsPerPage": 5,
            "numericFilters": f"created_at_i>{int(day - 10 * 86400)},created_at_i<{int(day + 3 * 86400)}"})
        hits = get_json(f"https://hn.algolia.com/api/v1/search?{params}")["hits"]
        # The search also matches loosely spelled words and post bodies; keep titles that name the topic.
        words = query.lower().split()
        hits = [h for h in hits if all(w in (h.get("title") or "").lower() for w in words)]
        if hits:
            break
    hits = sorted(hits, key=lambda h: h.get("points") or 0, reverse=True)[:2]
    if not hits:
        print("no HN discussion found")
        return
    for h in hits:
        print(f"{h['title']} | {h.get('points')} points, {h.get('num_comments')} comments"
              f" | https://news.ycombinator.com/item?id={h['objectID']}")
    for text in top_comments(hits[0]["objectID"], 3, 300):
        print(f"  top comment: {text}")


def top_comments(story_id, count, width):
    """The first comments of a story in HN's ranked order (Algolia's order is oldest first)."""
    story = get_json(f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json") or {}
    texts = []
    for kid in (story.get("kids") or [])[:count]:
        comment = get_json(f"https://hacker-news.firebaseio.com/v0/item/{kid}.json") or {}
        if comment.get("text") and not comment.get("deleted"):
            texts.append(" ".join(html.unescape(re.sub(r"<[^>]+>", " ", comment["text"])).split())[:width])
    return texts


def caption_lines(vtt):
    """YouTube auto-captions repeat each line across cues; keep every line once with its start second."""
    lines, last = [], None
    for block in vtt.split("\n\n"):
        stamp = re.match(r"(\d+):(\d+):(\d+)\.\d+ -->", block)
        if not stamp:
            continue
        start = int(stamp[1]) * 3600 + int(stamp[2]) * 60 + int(stamp[3])
        for raw in block.split("\n")[1:]:
            text = re.sub(r"<[^>]+>", "", raw).strip()
            if text and text != last:
                lines.append((start, text))
                last = text
    return lines


def mmss(seconds):
    return f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}"


def render_transcript(lines, sponsors):
    """Plain text with a [mm:ss] marker every 30 seconds; sponsor reads replaced by one marker."""
    out, bucket, marked = [], -1, set()
    for start, text in lines:
        seg = next((s for s in sponsors if s[0] <= start < s[1]), None)
        if seg:
            if tuple(seg) not in marked:
                marked.add(tuple(seg))
                out.append(f"\n[SPONSOR READ {mmss(seg[0])}-{mmss(seg[1])} omitted]\n")
            continue
        if start // 30 != bucket:
            bucket = start // 30
            out.append(f"\n[{mmss(start)}] ")
        out.append(text + " ")
    return "".join(out).strip()


def fetch(vid):
    """Download captions + description (no video), write transcripts/<id>.txt, return metadata."""
    tmp = TRANSCRIPTS / "tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    meta = ytdlp("--skip-download", "--no-simulate", "--write-subs", "--write-auto-subs",
                 "--sub-langs", "en.*", "--sub-format", "vtt", "--write-description",
                 "--print", "%(upload_date)s\t%(duration)s\t%(title)s",
                 "-o", str(tmp / "%(id)s.%(ext)s"), f"https://www.youtube.com/watch?v={vid}")
    upload, duration, title = meta.strip().splitlines()[-1].split("\t", 2)
    vtts = sorted(tmp.glob(f"{vid}.en*.vtt"), key=lambda p: p.name != f"{vid}.en.vtt")
    if not vtts:
        raise RuntimeError("no English captions yet")
    lines = caption_lines(vtts[0].read_text())
    if not lines:
        raise RuntimeError("caption file was empty")
    sponsors = sponsor_segments(vid)
    desc_file = tmp / f"{vid}.description"
    description = desc_file.read_text().strip()[:1500] if desc_file.exists() else ""
    uploaded = datetime.strptime(upload, "%Y%m%d").date()
    body = render_transcript(lines, sponsors or [])
    path = TRANSCRIPTS / f"{vid}.txt"
    path.write_text(f"TITLE: {title}\nUPLOADED: {uploaded}\nURL: https://www.youtube.com/watch?v={vid}\n\n"
                    f"DESCRIPTION:\n{description}\n\nTRANSCRIPT:\n{body}\n")
    shutil.rmtree(tmp, ignore_errors=True)
    return {"id": vid, "title": title, "url": f"https://www.youtube.com/watch?v={vid}",
            "uploaded": uploaded.isoformat(), "age_days": (date.today() - uploaded).days,
            "minutes": round(float(duration) / 60, 1), "transcript_file": str(path),
            "transcript_words": len(body.split()),
            "sponsor_segments": "unknown" if sponsors is None else [[mmss(a), mmss(b)] for a, b in sponsors]}


def fetch_news(vid, v):
    """A news story's details and its top comments, written as the 'transcript' the routine reads."""
    s = v["story"]
    comments = top_comments(vid.split("-", 1)[1], 6, 500)
    body = "\n".join(f"- {c}" for c in comments) or "(no comments yet)"
    TRANSCRIPTS.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPTS / f"{vid}.txt"
    path.write_text(f"TITLE: {v['title']}\nARTICLE: {s['url']}\n"
                    f"HACKER NEWS: {s['hn_url']} ({s['points']} points, {s['comments']} comments)\n"
                    f"POSTED: {s['posted']}\n\nTOP COMMENTS (in Hacker News' ranked order):\n{body}\n")
    return {"id": vid, "title": v["title"], "url": s["url"], "uploaded": s["posted"],
            "age_days": (date.today() - date.fromisoformat(s["posted"])).days, "minutes": 0,
            "transcript_file": str(path), "transcript_words": len(body.split()), "sponsor_segments": []}


def prepare(state, force, fill=False):
    today = date.today().isoformat()
    sync(state)
    run = state.get("today", {})
    if run.get("date") == today and not force:
        # Already picked today (e.g. a catch-up run): re-offer only what is not done yet.
        picks = [(vid, state["videos"][vid]) for vid in run["ids"]
                 if state["videos"][vid]["status"] in ("pending", "failed")]
    else:
        picks, run = None, {"date": today, "ids": []}
    videos, problems = [], []

    def take(vid, v):
        state["videos"][vid] = v
        try:
            info = fetch_news(vid, v) if v["kind"] == "news" else fetch(vid)
            info["kind"] = v["kind"]
            videos.append(info)
            v["status"] = "pending"
            v["meta"] = {k: info[k] for k in ("title", "url", "uploaded", "minutes", "sponsor_segments", "kind")}
            if v["kind"] == "news":
                v["meta"]["story"] = v["story"]
            if vid not in run["ids"]:
                run["ids"].append(vid)
        except Exception as err:  # one bad item must not stop the others
            v["attempts"] += 1
            v["status"] = "failed" if v["attempts"] < MAX_ATTEMPTS else "gave_up"
            v["last_error"] = str(err)
            problems.append({"id": vid, "title": v["title"], "error": str(err), "status": v["status"]})

    for vid, v in (picks if picks is not None else candidates(state, fill)):
        if len(videos) == PER_DAY:
            break
        take(vid, v)
    if picks is None:
        # No backlog left (or --fill): free slots get the top dev/AI news story, then a Fireship classic.
        for pick in (pick_news, pick_classic):
            if len(videos) == PER_DAY:
                break
            try:
                chosen = pick(state)
            except Exception as err:
                problems.append({"id": pick.__name__, "title": "", "error": str(err), "status": "failed"})
                continue
            if chosen:
                take(*chosen)
    state["today"] = run
    week = [{"title": v["title"], "tldr": v.get("tldr", "")} for v in state["videos"].values()
            if v["status"] == "done" and (date.today() - date.fromisoformat(v["done_on"])).days < 7]
    left = [v for v in state["videos"].values() if v["status"] in ("pending", "failed")]
    return {"date": today, "is_sunday": date.today().weekday() == 6, "page": state.get("page_url"), "videos": videos,
            "problems": problems,
            "queue": {"backlog_left": sum(v["kind"] == "backlog" for v in left),
                      "new_waiting": sum(v["kind"] == "new" for v in left)},
            "week_done": week if date.today().weekday() == 6 else []}


def count_words(value):
    if isinstance(value, str):
        return len(value.split())
    if isinstance(value, dict):
        return sum(count_words(v) for v in value.values())
    if isinstance(value, list):
        return sum(count_words(v) for v in value)
    return 0


def text_list(value, low, high):
    return isinstance(value, list) and low <= len(value) <= high and all(
        isinstance(s, str) and s.strip() for s in value)


def rows(value, keys, low, high):
    return isinstance(value, list) and low <= len(value) <= high and all(
        isinstance(r, dict) and all(isinstance(r.get(k), str) and r[k].strip() for k in keys) for r in value)


def entry_errors(e, meta):
    """Everything wrong with one entry, in plain words the routine can act on."""
    errors = []
    need = lambda ok, msg: ok or errors.append(msg)
    tags = e.get("tags")
    need(isinstance(tags, list) and 1 <= len(tags) <= 2 and set(tags) <= set(TAGS), f"tags: 1 or 2 of {TAGS}")
    need(text_list(e.get("tldr"), 3, 3), "tldr: exactly 3 lines")
    need(text_list(e.get("said"), 2, 8), "said: 2 to 8 paragraphs")
    need(count_words(e.get("said")) <= 450, f"said: {count_words(e.get('said'))} words, the limit is 450")
    moments = e.get("moments")
    if meta["kind"] == "news":
        need(moments in ([], None), "moments: leave empty for news, there is no video")
    else:
        need(rows(moments, ["t", "text"], 2, 4) and all(re.fullmatch(r"\d{1,2}:\d{2}", m["t"]) for m in moments),
             "moments: 2 to 4 rows of {t: 'mm:ss', text}")
    claims = e.get("claims")
    need(rows(claims, ["claim", "type"], 1, 5) and all(c["type"] in CLAIM_TYPES for c in claims),
         "claims: 1 to 5 rows of {claim, type}, type is Fact, Opinion or Joke")
    mentioned = e.get("mentioned")
    need(rows(mentioned, ["name", "what"], 1, 10)
         and all(isinstance(m.get("link"), str) and (m["link"] == "" or m["link"].startswith("https://"))
                 for m in mentioned),
         "mentioned: 1 to 10 rows of {name, what, link}, link is '' or starts with https://")
    need(rows(e.get("glossary"), ["term", "meaning"], 3, 6), "glossary: 3 to 6 rows of {term, meaning}")
    need(text_list(e.get("for_me"), 3, 5), "for_me: 3 to 5 lines")
    age = (date.today() - date.fromisoformat(meta["uploaded"])).days
    since = e.get("since")
    if meta["kind"] == "backlog" and age > 14:
        need(isinstance(since, str) and since.strip(), "since: needed, this backlog video is older than 14 days")
    else:
        need(since in ("", None), "since: leave empty, it is only for backlog videos older than 14 days")
    hn_thread = e.get("hn")
    need(hn_thread is None or (isinstance(hn_thread, dict) and isinstance(hn_thread.get("title"), str)
                               and isinstance(hn_thread.get("points"), int)
                               and str(hn_thread.get("url", "")).startswith("https://news.ycombinator.com/item?id=")),
         "hn: null or {title, url (news.ycombinator.com/item?id=...), points (a number)}")
    total = count_words({k: e.get(k) for k in ("tldr", "said", "moments", "claims", "mentioned",
                                                 "glossary", "for_me", "since")})
    need(total <= 1000, f"the whole entry is {total} words, the limit is 1,000")
    return errors, total


def check(path, state):
    """Validate the routine's draft. On success write one JSON file per database document and print the batch."""
    draft = json.loads(Path(path).read_text())
    today_ids = state.get("today", {}).get("ids", [])
    shutil.rmtree(OUTBOX, ignore_errors=True)
    errors, writes, words = [], [], {}
    added = datetime.now().isoformat(timespec="seconds")
    for e in draft.get("entries", []):
        vid = e.get("id")
        v = state["videos"].get(vid, {})
        if vid not in today_ids or "meta" not in v:
            errors.append(f"{vid}: not one of today's videos")
            continue
        problems, total = entry_errors(e, v["meta"])
        errors += [f"{vid}: {p}" for p in problems]
        if problems:
            continue
        meta, segs = v["meta"], v["meta"]["sponsor_segments"]
        doc = {"id": vid, "title": meta["title"], "url": meta["url"], "uploaded": meta["uploaded"],
               "minutes": meta["minutes"], "kind": meta["kind"], "added_at": added, "words": total,
               "sponsor": "unknown" if segs == "unknown" else ", ".join(f"{a}–{b}" for a, b in segs),
               **{k: e.get(k) for k in ("tags", "tldr", "said", "moments", "claims", "mentioned",
                                        "glossary", "for_me", "hn")},
               "since": e.get("since") or ""}
        if meta["kind"] == "news":
            story = meta["story"]
            doc.update(moments=[], hn={"title": meta["title"], "url": story["hn_url"], "points": story["points"]})
        writes.append(write_doc("entries", vid, doc))
        words[vid] = total
    week = draft.get("week")
    if week is not None:
        ok = (isinstance(week, dict) and text_list(week.get("bullets"), 3, 8)
              and all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(week.get(k, ""))) for k in ("from", "to")))
        if ok:
            writes.append(write_doc("weeks", week["from"], {**week, "added_at": added}))
        else:
            errors.append("week: {from: 'YYYY-MM-DD', to: 'YYYY-MM-DD', bullets: 3 to 8 lines} or null")
    if not draft.get("entries") and week is None:
        errors.append("the draft has no entries and no week")
    if errors:
        print("NOT READY. Fix these in the draft, then run check again:")
        print("\n".join(f"- {e}" for e in errors))
        sys.exit(1)
    print(json.dumps({"ready": True, "words": words, "writes": writes}, indent=1))


def write_doc(collection, doc_id, doc):
    path = OUTBOX / collection / f"{doc_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False))
    return {"op": "set", "collection": collection, "doc_id": doc_id, "file_path": str(path)}


REMINDER_SCRIPT = """
on run argv
  set d to (current date)
  if (item 3 of argv) is "later" then
    set d to d + 3600
  else
    set hours of d to 13
    set minutes of d to 0
    set seconds of d to 0
  end if
  tell application "Reminders"
    make new reminder with properties {name:(item 1 of argv), body:(item 2 of argv), due date:d, remind me date:d}
  end tell
end run
"""


def remind(state, reads):
    """An Apple Reminders reminder at 13:00 (or in an hour when later): iCloud brings it to the iPhone."""
    done = [v for v in state["videos"].values() if v["status"] == "done"]
    new = [state["videos"][i]["title"] for i in state.get("today", {}).get("ids", [])
           if state["videos"][i]["status"] == "done"]
    unread = max(len(done) - reads, 0)
    name = f"Read Fireship Digest: {len(new)} new, {unread} unread"
    body = "\n".join([*new, state.get("page_url", "")]).strip()
    when = "later" if datetime.now().hour >= 13 else "today"
    run = subprocess.run(["osascript", "-e", REMINDER_SCRIPT, name, body, when],
                         capture_output=True, text=True, timeout=60)
    if run.returncode != 0:
        raise SystemExit(f"reminder failed: {run.stderr.strip()}")
    print(f"reminder added: {name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare", help="pick today's videos and write their transcripts")
    prep.add_argument("--force", action="store_true", help="pick fresh videos even if today already ran")
    prep.add_argument("--fill", action="store_true",
                      help="act as if the backlog were empty: free slots get news and a classic")
    done = sub.add_parser("done", help="mark the draft's videos as summarised")
    done.add_argument("--from", dest="draft", required=True, help="the draft that check accepted")
    news = sub.add_parser("hn", help="Hacker News threads about a topic near a date")
    news.add_argument("queries", nargs="+", help="short names, most distinctive first")
    news.add_argument("--around", required=True, help="the video's upload date, YYYY-MM-DD")
    chk = sub.add_parser("check", help="validate the draft and prepare one database write")
    chk.add_argument("draft")
    rem = sub.add_parser("remind", help="add today's reading reminder to Apple Reminders")
    rem.add_argument("--reads", type=int, required=True, help="number of documents in the page's reads")
    page = sub.add_parser("set-page", help="save the web app's URL")
    page.add_argument("url")
    args = parser.parse_args()

    if args.command == "hn":
        hn(args.queries, args.around)
        return
    state = json.loads(STATE.read_text()) if STATE.exists() else {"videos": {}}
    if args.command == "check":
        check(args.draft, state)
        return
    if args.command == "remind":
        remind(state, args.reads)
        return
    if args.command == "prepare":
        print(json.dumps(prepare(state, args.force, args.fill), indent=2, ensure_ascii=False))
    elif args.command == "set-page":
        state["page_url"] = args.url
        print("saved page url")
    else:
        for e in json.loads(Path(args.draft).read_text()).get("entries", []):
            v = state["videos"][e["id"]]
            v.update(status="done", done_on=date.today().isoformat(), tldr=e["tldr"][0])
            (TRANSCRIPTS / f"{e['id']}.txt").unlink(missing_ok=True)
            print(f"marked done: {v['title']}")
        left = [v for v in state["videos"].values() if v["status"] in ("pending", "failed")]
        print(f"backlog left: {sum(v['kind'] == 'backlog' for v in left)}, new waiting: {sum(v['kind'] == 'new' for v in left)}")
        if OUTBOX.exists():
            shutil.copytree(OUTBOX, BACKUP, dirs_exist_ok=True)
            print(f"backup: {BACKUP}")
    STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
