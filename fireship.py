#!/usr/bin/env python3
"""Fireship digest helper: keeps the video queue and prepares clean transcripts.

The daily routine calls:
  python3 fireship.py prepare            pick today's videos, write transcripts, print a JSON manifest
  python3 fireship.py hn NAME [NAME ...] --around YYYY-MM-DD   Hacker News threads near the upload date
  python3 fireship.py done ID --tldr ..  mark a video as summarised
One-time setup:
  python3 fireship.py set-doc DOC_ID BODY_ID ANCHOR_ID   where entries are written (kept in state.json)
"""
import argparse
import html
import json
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state.json"
TRANSCRIPTS = ROOT / "transcripts"
CHANNEL = "https://www.youtube.com/@Fireship/videos"
BACKLOG_SIZE = 20
PER_DAY = 2
MAX_ATTEMPTS = 3
MIN_SECONDS = 90  # shorter uploads are treated as Shorts and skipped
YTDLP = shutil.which("yt-dlp") or "/opt/homebrew/bin/yt-dlp"


def ytdlp(*args):
    run = subprocess.run([YTDLP, "--no-warnings", "--no-update", *args],
                         capture_output=True, text=True, timeout=300)
    if run.returncode != 0:
        raise RuntimeError(run.stderr.strip()[-400:] or f"yt-dlp exited with {run.returncode}")
    return run.stdout


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


def candidates(state):
    """New uploads first (oldest new first), then the backlog from its oldest video."""
    open_ = [(vid, v) for vid, v in state["videos"].items()
             if v["status"] in ("pending", "failed") and v["attempts"] < MAX_ATTEMPTS]
    return sorted(open_, key=lambda item: (item[1]["kind"] != "new", item[1]["seq"]))


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
    # The official API lists comments in HN's ranked order (Algolia's order is oldest first).
    story = get_json(f"https://hacker-news.firebaseio.com/v0/item/{hits[0]['objectID']}.json") or {}
    for kid in (story.get("kids") or [])[:3]:
        comment = get_json(f"https://hacker-news.firebaseio.com/v0/item/{kid}.json") or {}
        if comment.get("text") and not comment.get("deleted"):
            text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", comment["text"])).split())
            print(f"  top comment: {text[:300]}")


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


def prepare(state, force):
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
    for vid, v in (picks if picks is not None else candidates(state)):
        if len(videos) == PER_DAY:
            break
        try:
            info = fetch(vid)
            info["kind"] = v["kind"]
            videos.append(info)
            v["status"] = "pending"
            if vid not in run["ids"]:
                run["ids"].append(vid)
        except Exception as err:  # one bad video must not stop the others
            v["attempts"] += 1
            v["status"] = "failed" if v["attempts"] < MAX_ATTEMPTS else "gave_up"
            v["last_error"] = str(err)
            problems.append({"id": vid, "title": v["title"], "error": str(err), "status": v["status"]})
    state["today"] = run
    week = [{"title": v["title"], "tldr": v.get("tldr", "")} for v in state["videos"].values()
            if v["status"] == "done" and (date.today() - date.fromisoformat(v["done_on"])).days < 7]
    left = [v for v in state["videos"].values() if v["status"] in ("pending", "failed")]
    return {"date": today, "is_sunday": date.today().weekday() == 6, "doc": state.get("doc"), "videos": videos,
            "problems": problems,
            "queue": {"backlog_left": sum(v["kind"] == "backlog" for v in left),
                      "new_waiting": sum(v["kind"] == "new" for v in left)},
            "week_done": week if date.today().weekday() == 6 else []}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare", help="pick today's videos and write their transcripts")
    prep.add_argument("--force", action="store_true", help="pick fresh videos even if today already ran")
    done = sub.add_parser("done", help="mark a video as summarised")
    done.add_argument("id")
    done.add_argument("--tldr", default="", help="one-line summary kept for the Sunday wrap-up")
    news = sub.add_parser("hn", help="Hacker News threads about a topic near a date")
    news.add_argument("queries", nargs="+", help="short names, most distinctive first")
    news.add_argument("--around", required=True, help="the video's upload date, YYYY-MM-DD")
    doc = sub.add_parser("set-doc", help="save where entries are written")
    doc.add_argument("doc_id")
    doc.add_argument("body_id")
    doc.add_argument("anchor_id", help="block that new entries are inserted after")
    args = parser.parse_args()

    if args.command == "hn":
        hn(args.queries, args.around)
        return
    state = json.loads(STATE.read_text()) if STATE.exists() else {"videos": {}}
    if args.command == "prepare":
        print(json.dumps(prepare(state, args.force), indent=2, ensure_ascii=False))
    elif args.command == "set-doc":
        state["doc"] = {"doc_id": args.doc_id, "body_id": args.body_id, "anchor_id": args.anchor_id}
        print("saved doc ids")
    else:
        v = state["videos"][args.id]
        v.update(status="done", done_on=date.today().isoformat(), tldr=args.tldr)
        (TRANSCRIPTS / f"{args.id}.txt").unlink(missing_ok=True)
        print(f"marked done: {v['title']}")
    STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
