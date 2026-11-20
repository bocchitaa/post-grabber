"""
Telegram channel -> Discord webhook bot (single-run version for GitHub Actions).

How it remembers what it already sent:
  Telegram post numbers only go up (8981, 8982, 8983 ...). So the bot only
  needs to remember ONE number per channel: the newest post it has handled.
  That number is saved in last_posts.json, like this:   {"team_mew": 8982}

  * First run (no number saved yet): sends every post from "start_date" until now.
  * Every later run: sends only posts with a bigger number than the saved one.

For each post it sends the text and photos. Videos, audio and files can't be
shown in the preview page, so for those it sends a link to open the post.
"""

import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "last_posts.json"
MAX_PAGES = 60  # each page holds about 20 posts, so this is plenty

# The webhook URL comes from a GitHub secret (see the workflow file).
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
if not DISCORD_WEBHOOK_URL:
    sys.exit("Missing DISCORD_WEBHOOK_URL environment variable / secret.")

# Pretend to be a normal browser so Telegram shows the normal page
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36"
}

# If a post contains any of these, it has media we can't show (video, audio, file...)
MEDIA_MARKERS = (
    "message_video",
    "message_roundvideo",
    "message_document",
    "message_voice",
    "message_sticker",
    "message_media_not_supported",
)


# ---------- settings and saved state ----------

def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("channels", [])
    cfg.setdefault("start_date", "2026-11-14")  # used on the first run only (UTC)
    cfg.setdefault("required_text", "")         # optional filter, empty = send everything
    return cfg


def parse_start_date(value: str) -> datetime:
    """'2026-11-14' -> Nov 14 00:00 UTC. You can also write '2026-11-14T00:00:00+06:00'."""
    when = datetime.fromisoformat(value)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when


def load_state() -> dict:
    if STATE_PATH.exists():
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# ---------- reading Telegram ----------

def get_page(channel: str, before=None):
    """Download the public preview page. Returns the HTML text, or None."""
    url = f"https://t.me/s/{channel}"
    if before:
        url += f"?before={before}"  # shows posts older than this post number
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
    except requests.RequestException as err:
        print(f"Could not reach {url}: {err}")
        return None

    if resp.status_code != 200:
        print(f"{url} returned status {resp.status_code}")
        return None

    return resp.text


def clean_text(raw_html: str) -> str:
    """Turn the HTML of a post's text into plain text that Discord can show."""
    # Remove #hashtag links completely
    raw_html = re.sub(
        r'<a [^>]*href="[^"]*\?q=%23[^"]*"[^>]*>.*?</a>', "", raw_html, flags=re.DOTALL
    )
    # Line breaks
    raw_html = re.sub(r"<br\s*/?>", "\n", raw_html)

    # Links: keep them clickable
    def fix_link(match):
        url = html.unescape(match.group(1))
        label = re.sub(r"<[^>]+>", "", match.group(2)).strip()
        if not url.startswith("http"):
            return label
        if not label or label == url:
            return url
        return f"[{label}]({url})"

    raw_html = re.sub(
        r'<a [^>]*href="([^"]+)"[^>]*>(.*?)</a>', fix_link, raw_html, flags=re.DOTALL
    )

    text = re.sub(r"<[^>]+>", "", raw_html)  # remove any other HTML tags
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)  # no huge gaps
    return text.strip()


def get_post_text(piece: str) -> str:
    """Get the written text of one post (not the 'reply to' preview)."""
    match = re.search(
        r'class="tgme_widget_message_text(?![^"]*reply)[^"]*"[^>]*>(.*?)</div>',
        piece,
        flags=re.DOTALL,
    )
    if not match:
        return ""
    return clean_text(match.group(1))


def get_post_date(piece: str):
    """Read the time the post was written, like <time datetime="2026-11-14T10:10:00+00:00">."""
    match = re.search(r'<time[^>]*datetime="([^"]+)"', piece)
    if not match:
        return None
    try:
        return datetime.fromisoformat(match.group(1))
    except ValueError:
        return None


def post_number(post_id: str) -> int:
    return int(post_id.split("/")[-1])  # "team_mew/8982" -> 8982


def page_numbers(page_html: str) -> list:
    """All post numbers that appear on a page (including ones we skip)."""
    return [int(n) for n in re.findall(r'data-post="[^/"]+/(\d+)"', page_html)]


def find_posts(page_html: str, channel: str, required_text: str = ""):
    """
    Look through the page and return a list of posts.
    Each post is a small dictionary with its id, link, date, text, photos
    and whether it has other media (video, audio, file...).
    """
    posts = []

    # Every message in the page starts with: data-post="channelname/1234"
    pieces = page_html.split('data-post="')[1:]

    for piece in pieces:
        post_id = piece.split('"', 1)[0]  # e.g. "team_mew/8982"

        text = get_post_text(piece)

        # Photos are stored like: background-image:url('https://...jpg')
        photos = re.findall(
            r"tgme_widget_message_photo_wrap[^>]*background-image:url\('([^']+)'\)",
            piece,
        )
        photos = [html.unescape(p) for p in photos]

        other_media = any(marker in piece for marker in MEDIA_MARKERS)

        # Nothing useful in this message (for example a system notice) -> skip
        if not (text or photos or other_media):
            continue

        # Optional filter (not case sensitive)
        if required_text and required_text.lower() not in text.lower():
            continue

        posts.append(
            {
                "id": post_id,
                "link": f"https://t.me/{post_id}",
                "channel": channel,
                "date": get_post_date(piece),
                "text": text,
                "photos": photos,
                "other_media": other_media,
            }
        )

    return posts


def collect_posts(channel: str, config: dict, last_number, start_dt: datetime):
    """
    Read the newest page, then keep going back page by page until we reach
    posts we already handled (or, on the first run, posts older than start_date).
    Returns (posts oldest-first, newest post number), or None if it failed.
    """
    page = get_page(channel)
    if page is None:
        return None

    first_numbers = page_numbers(page)
    newest_number = max(first_numbers) if first_numbers else None

    posts = {}
    for _ in range(MAX_PAGES):
        for p in find_posts(page, channel, config["required_text"]):
            posts[p["id"]] = p

        numbers = page_numbers(page)
        if not numbers:
            break
        oldest = min(numbers)

        if last_number is not None:
            if oldest <= last_number:   # we reached posts we already handled
                break
        else:
            pieces = page.split('data-post="')[1:]
            dates = [d for d in (get_post_date(x) for x in pieces) if d]
            if dates and min(dates) < start_dt:   # we went back far enough
                break

        page = get_page(channel, before=oldest)
        if page is None:
            break
        time.sleep(1)

    ordered = sorted(posts.values(), key=lambda p: post_number(p["id"]))
    return ordered, newest_number


# ---------- sending to Discord ----------

def build_embeds(post: dict) -> list:
    """Make the Discord message for one post: text + photo(s)."""
    text = post["text"]

    # Video / audio / file can't be shown here, so give a link to open it
    if post["other_media"] and not post["photos"]:
        link_line = f"[Open media in Telegram]({post['link']})"
        text = f"{text}\n\n{link_line}" if text else link_line

    first = {"color": 0x2AABEE, "footer": {"text": post["channel"]}}
    if text:
        first["description"] = text[:4000]  # Discord's limit is 4096

    embeds = [first]
    if post["photos"]:
        first["image"] = {"url": post["photos"][0]}
        for extra_photo in post["photos"][1:]:
            embeds.append({"image": {"url": extra_photo}})

    return embeds[:10]  # Discord allows at most 10 embeds per message


def send_to_discord(embeds: list) -> bool:
    """Send one message. Returns True if Discord accepted it."""
    resp = None
    for _ in range(2):  # try once, and once more if Discord says "slow down"
        resp = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": embeds}, timeout=15)
        if resp.status_code == 429:
            try:
                wait = float(resp.json().get("retry_after", 2))
            except ValueError:
                wait = 2
            time.sleep(wait + 0.5)
            continue
        break

    if resp is not None and resp.status_code < 300:
        return True

    print(f"Discord webhook error {resp.status_code if resp is not None else '?'}: "
          f"{resp.text if resp is not None else ''}")
    return False


# ---------- main ----------

def main():
    config = load_config()
    channels = config["channels"]
    if not channels:
        sys.exit('config.json has no channels. Example: {"channels": ["team_mew"]}')

    start_dt = parse_start_date(config["start_date"])
    state = load_state()
    new_state = dict(state)

    any_channel_worked = False
    sent = 0

    for channel in channels:
        last_number = state.get(channel)  # None = first run for this channel

        result = collect_posts(channel, config, last_number, start_dt)
        if result is None:
            continue
        any_channel_worked = True
        posts, newest_number = result

        if last_number is None:
            print(f"{channel}: first run, sending posts since {config['start_date']}")

        # Decide which posts to send
        to_send = []
        for post in posts:
            if last_number is not None:
                if post_number(post["id"]) <= last_number:
                    continue
            else:
                if post["date"] is None or post["date"] < start_dt:
                    continue
            to_send.append(post)

        print(f"{channel}: {len(posts)} post(s) read, {len(to_send)} to send")

        # Send oldest first. If Discord refuses one, stop there and try again next run.
        saved = last_number
        all_ok = True
        for post in to_send:
            if not send_to_discord(build_embeds(post)):
                all_ok = False
                break
            saved = post_number(post["id"])
            sent += 1
            print(f"Posted: {post['id']}")
            time.sleep(1)  # small pause so Discord doesn't rate-limit us

        # Everything went fine -> remember the newest post number on the page
        if all_ok and newest_number is not None:
            saved = max(saved or 0, newest_number)

        if saved is not None:
            new_state[channel] = saved

    if new_state != state:
        save_state(new_state)

    if not any_channel_worked:
        sys.exit("No channel could be read. Check the log above.")

    print(f"Done. Posted {sent} post(s).")


if __name__ == "__main__":
    main()
