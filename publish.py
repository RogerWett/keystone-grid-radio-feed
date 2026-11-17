#!/usr/bin/env python3
"""Publish a Keystone Grid Radio episode to the private podcast feed.

Uploads the MP3 as a GitHub Release asset (not in git), regenerates the
RSS feed (latest 30 episodes), prunes older releases, and pushes the
feed so Render can auto-deploy the static site.

Usage:
  publish.py <mp3-path>          Publish one episode
  publish.py --backfill          Publish existing official episodes (max 30)
  publish.py --rebuild-feed      Rebuild feed.xml from current releases
  publish.py --dry-run [mp3]     Build a local feed; do not upload or push
  publish.py --status            Show auth and config (no secrets)

Auth (first match wins; never printed):
  $GH_TOKEN, $GITHUB_TOKEN, or `gh auth token`
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from xml.dom import minidom
from zoneinfo import ZoneInfo

ET = ET  # noqa: keep alias for type checkers

FEED_DIR = Path(__file__).resolve().parent
PODCAST_DIR = FEED_DIR.parent
PUBLIC_DIR = FEED_DIR / "public"
CONFIG_PATH = FEED_DIR / "config.json"
TZ = ZoneInfo("America/New_York")
USER_AGENT = "KeystoneGridRadioFeed/1.0"
ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM_NS = "http://www.w3.org/2005/Atom"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"

EPISODE_RE = re.compile(
    r"^keystone-grid-radio(?:-(?P<edition>weekend|monthly|safety))?-(?P<date>\d{4}-\d{2}-\d{2})\.mp3$"
)
SPEAKER_RE = re.compile(
    r"^(?:@[A-Z0-9_]+|[A-Z][A-Z0-9 .'-]{1,30})(?:\|[^:]*)?:\s*(.*)$"
)
CUE_RE = re.compile(r"^@[A-Z0-9_]+$")


class PublishError(Exception):
    pass


def log(msg: str) -> None:
    print(msg, flush=True)


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise PublishError(f"Missing {CONFIG_PATH}. Copy config.example.json and fill it in.")
    return json.loads(CONFIG_PATH.read_text())


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Auth — never print token values
# ---------------------------------------------------------------------------

def get_token() -> Optional[str]:
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        val = os.environ.get(key, "").strip()
        if val:
            return val
    try:
        out = subprocess.run(
            ["gh", "auth", "token"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        token = (out.stdout or "").strip()
        if out.returncode == 0 and token:
            return token
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def auth_source() -> str:
    if os.environ.get("GH_TOKEN", "").strip():
        return "GH_TOKEN"
    if os.environ.get("GITHUB_TOKEN", "").strip():
        return "GITHUB_TOKEN"
    try:
        out = subprocess.run(
            ["gh", "auth", "token"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode == 0 and (out.stdout or "").strip():
            return "gh auth"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "missing"


def require_token() -> str:
    token = get_token()
    if token:
        return token
    raise PublishError(
        "No GitHub write token is available on this box.\n"
        "Need: a classic PAT with the public_repo scope (create the public "
        "repo, upload release assets, and push feed.xml), stored as GH_TOKEN "
        "or via `gh auth login`. Do not paste the token into chat.\n"
        "Repo to use: the public GitHub repo named in config.json "
        "(default RogerWett/keystone-grid-radio-feed)."
    )


# ---------------------------------------------------------------------------
# Episode metadata
# ---------------------------------------------------------------------------

@dataclass
class Episode:
    date: dt.date
    edition: str  # weekday | weekend | monthly | safety
    mp3_path: Path
    script_path: Optional[Path]
    title: str
    description: str
    pub_date: dt.datetime
    duration_sec: int
    size: int
    guid: str
    asset_name: str
    tag: str
    enclosure_url: str = ""


def parse_mp3_name(path: Path) -> Optional[tuple[dt.date, str]]:
    m = EPISODE_RE.match(path.name)
    if not m:
        return None
    date = dt.date.fromisoformat(m.group("date"))
    edition = m.group("edition") or "weekday"
    return date, edition


def official_episodes(root: Path = PODCAST_DIR) -> list[Path]:
    found = []
    for p in sorted(root.glob("keystone-grid-radio*.mp3")):
        if parse_mp3_name(p):
            found.append(p)
    return found


def format_title(date: dt.date, edition: str) -> str:
    # "Tuesday, November 17, 2026" — day number is not zero-padded
    day = date.strftime("%A, %B ") + str(date.day) + date.strftime(", %Y")
    if edition == "weekend":
        return f"Keystone Grid Radio — Weekend Edition, {day}"
    if edition in {"monthly", "safety"}:
        return f"Keystone Grid Radio — Monthly Safety Meeting, {day}"
    return f"Keystone Grid Radio — {day}"


def default_pub_datetime(date: dt.date, edition: str, script_text: str, mp3: Path) -> dt.datetime:
    """Scheduled time in America/New_York, with a few script-based overrides."""
    if edition == "weekend":
        hour, minute = 8, 0
    elif edition in {"monthly", "safety"}:
        hour, minute = 5, 30
    else:
        hour, minute = 5, 30
    # 2026-11-13 weekday episode was a Friday afternoon update
    lowered = script_text.lower()
    m = re.search(
        r"a little after (\w+)(?:\s+(\w+))?\s+eastern",
        lowered,
    )
    if m:
        word_to_hour = {
            "one": 13, "two": 14, "three": 15, "four": 16,
            "five": 17, "twelve": 12, "eleven": 11, "ten": 10,
            "nine": 9, "eight": 8, "seven": 7, "six": 6,
        }
        first, second = m.group(1), m.group(2)
        if first == "one" and second == "fifteen":
            hour, minute = 13, 15
        elif first in word_to_hour and not second:
            hour, minute = word_to_hour[first], 0
    return dt.datetime(date.year, date.month, date.day, hour, minute, tzinfo=TZ)


def ffprobe_duration(mp3: Path) -> int:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1",
            str(mp3),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return int(round(float(out.stdout.strip())))


def format_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def sibling_script(mp3: Path) -> Optional[Path]:
    txt = mp3.with_suffix(".txt")
    return txt if txt.exists() else None


def clean_spoken_line(line: str) -> Optional[str]:
    s = line.strip()
    if not s or s.startswith("#") or CUE_RE.match(s):
        return None
    m = SPEAKER_RE.match(s)
    if m:
        s = m.group(1).strip()
    s = re.sub(r"\s+", " ", s)
    return s or None


def extract_description(script_path: Optional[Path]) -> str:
    """First lines of the script (the lead / cold open), no production markup."""
    if not script_path or not script_path.exists():
        return "A new episode of Keystone Grid Radio."
    raw = script_path.read_text(encoding="utf-8", errors="replace")
    lines = raw.splitlines()
    # Prefer dialogue after @COLD, otherwise first spoken lines
    cold_idx = next((i for i, ln in enumerate(lines) if ln.strip() == "@COLD"), None)
    start = (cold_idx + 1) if cold_idx is not None else 0
    spoken: list[str] = []
    for ln in lines[start:]:
        if ln.strip().startswith("@") and not SPEAKER_RE.match(ln.strip()):
            if spoken:
                break
            continue
        cleaned = clean_spoken_line(ln)
        if not cleaned:
            continue
        # Skip pure idents / greetings if we already have substance later
        spoken.append(cleaned)
        if len(" ".join(spoken)) > 450:
            break
        if len(spoken) >= 6:
            break
    # Drop a leading ident-only line if later lines carry the lead
    ident = re.compile(
        r"^(good morning|hey, good morning|you.?re listening|this is keystone|i.?m brian)\b",
        re.I,
    )
    while spoken and ident.search(spoken[0]):
        if len(spoken[0]) >= 180:
            rest = re.sub(
                r"^(?:you.?re listening to keystone grid radio\.?\s*)?(?:brian here, with max and lena in the newsroom,\s*)?",
                "",
                spoken[0],
                flags=re.I,
            ).strip()
            if rest and rest != spoken[0]:
                rest = re.sub(r"^and\s+", "", rest, flags=re.I).strip()
                if rest:
                    rest = rest[0].upper() + rest[1:]
                    spoken[0] = rest
                    break
        if len(spoken) > 1:
            spoken.pop(0)
            continue
        break
    text = " ".join(spoken).strip()
    if len(text) > 600:
        cut = text[:600]
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        text = cut.rstrip(",;:") + "…"
    return text or "A new episode of Keystone Grid Radio."


def build_episode(mp3: Path, cfg: dict, enclosure_url: str = "") -> Episode:
    parsed = parse_mp3_name(mp3)
    if not parsed:
        raise PublishError(
            f"{mp3.name} is not an official episode filename "
            "(expected keystone-grid-radio-YYYY-MM-DD.mp3)."
        )
    date, edition = parsed
    script = sibling_script(mp3)
    script_text = script.read_text(encoding="utf-8", errors="replace") if script else ""
    tag = date.isoformat() if edition == "weekday" else f"{edition}-{date.isoformat()}"
    guid = f"kgr-{tag}"
    return Episode(
        date=date,
        edition=edition,
        mp3_path=mp3,
        script_path=script,
        title=format_title(date, edition),
        description=extract_description(script),
        pub_date=default_pub_datetime(date, edition, script_text, mp3),
        duration_sec=ffprobe_duration(mp3),
        size=mp3.stat().st_size,
        guid=guid,
        asset_name=mp3.name,
        tag=tag,
        enclosure_url=enclosure_url,
    )


# ---------------------------------------------------------------------------
# RSS
# ---------------------------------------------------------------------------

def site_url(cfg: dict) -> str:
    url = (cfg.get("site_url") or "").rstrip("/")
    return url or "https://keystone-grid-radio.onrender.com"


def feed_url(cfg: dict) -> str:
    return f"{site_url(cfg)}/{cfg['feed_filename']}"


def cover_url(cfg: dict) -> str:
    return f"{site_url(cfg)}/cover.png"


def rfc2822(ts: dt.datetime) -> str:
    return ts.strftime("%a, %d %b %Y %H:%M:%S %z")


def _itunes(tag: str) -> str:
    return f"{{{ITUNES_NS}}}{tag}"


def _atom(tag: str) -> str:
    return f"{{{ATOM_NS}}}{tag}"


def build_feed_xml(cfg: dict, episodes: list[Episode]) -> str:
    ET.register_namespace("itunes", ITUNES_NS)
    ET.register_namespace("atom", ATOM_NS)
    ET.register_namespace("content", CONTENT_NS)

    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    title = cfg.get("podcast_title") or "Keystone Grid Radio"
    desc = cfg.get("podcast_description") or title
    author = cfg.get("podcast_author") or title
    link = site_url(cfg) + "/"
    cover = cover_url(cfg)

    def add(parent, tag, text=None, attrib=None):
        el = ET.SubElement(parent, tag, attrib or {})
        if text is not None:
            el.text = text
        return el

    add(channel, "title", title)
    add(channel, "link", link)
    add(channel, "description", desc)
    add(channel, "language", cfg.get("language") or "en-us")
    year = dt.datetime.now(TZ).year
    add(channel, "copyright", f"© {year} Keystone Grid Radio")
    add(channel, "lastBuildDate", rfc2822(dt.datetime.now(TZ)))
    add(channel, "generator", "Keystone Grid Radio publisher")
    add(channel, "ttl", "60")
    add(channel, _atom("link"), attrib={
        "href": feed_url(cfg),
        "rel": "self",
        "type": "application/rss+xml",
    })
    image = add(channel, "image")
    add(image, "url", cover)
    add(image, "title", title)
    add(image, "link", link)

    add(channel, _itunes("author"), author)
    add(channel, _itunes("summary"), desc)
    add(channel, _itunes("subtitle"), "The Mid-Atlantic grid briefing")
    add(channel, _itunes("explicit"), "false")
    add(channel, _itunes("type"), "episodic")
    add(channel, _itunes("block"), "Yes")
    add(channel, _itunes("image"), attrib={"href": cover})
    owner = add(channel, _itunes("owner"))
    add(owner, _itunes("name"), author)
    # Deliberately no owner email — do not put a personal address in a public feed
    cat = add(channel, _itunes("category"), attrib={"text": cfg.get("itunes_category") or "News"})
    if cfg.get("itunes_subcategory"):
        add(cat, _itunes("category"), attrib={"text": cfg["itunes_subcategory"]})

    for ep in sorted(episodes, key=lambda e: e.pub_date, reverse=True):
        item = add(channel, "item")
        add(item, "title", ep.title)
        add(item, "description", ep.description)
        add(item, "pubDate", rfc2822(ep.pub_date))
        add(item, "guid", ep.guid, attrib={"isPermaLink": "false"})
        add(item, "link", ep.enclosure_url or link)
        add(
            item,
            "enclosure",
            attrib={
                "url": ep.enclosure_url,
                "length": str(ep.size),
                "type": "audio/mpeg",
            },
        )
        add(item, _itunes("title"), ep.title)
        add(item, _itunes("summary"), ep.description)
        add(item, _itunes("explicit"), "false")
        add(item, _itunes("duration"), format_duration(ep.duration_sec))
        add(item, _itunes("episodeType"), "full")
        add(item, _itunes("image"), attrib={"href": cover})

    raw = ET.tostring(rss, encoding="utf-8", xml_declaration=True)
    pretty = minidom.parseString(raw).toprettyxml(indent="  ", encoding="utf-8")
    # minidom adds an extra declaration-friendly header; keep one XML declaration
    text = pretty.decode("utf-8")
    # Remove blank lines minidom inserts
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines[0].startswith("<?xml"):
        lines.insert(0, '<?xml version="1.0" encoding="UTF-8"?>')
    return "\n".join(lines) + "\n"


def write_feed(cfg: dict, episodes: list[Episode]) -> Path:
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    xml = build_feed_xml(cfg, episodes)
    path = PUBLIC_DIR / "feed.xml"
    path.write_text(xml, encoding="utf-8")
    return path


def write_landing(cfg: dict, inject_feed_url: bool = False) -> Path:
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    subscribe_block = ""
    if inject_feed_url and cfg.get("site_url"):
        url = feed_url(cfg)
        subscribe_block = f"""
      <section class="card">
        <h2>Subscribe</h2>
        <p>This show is private and is not listed in Apple Podcasts or other directories. Add it by URL in your podcast app:</p>
        <p class="url">{_html(url)}</p>
        <ul>
          <li><strong>Apple Podcasts:</strong> Library → ⋯ → Follow a Show by URL</li>
          <li><strong>Overcast:</strong> Add URL</li>
          <li><strong>Pocket Casts:</strong> Discover → then paste the feed URL</li>
        </ul>
      </section>
"""
    else:
        subscribe_block = """
      <section class="card">
        <h2>Subscribe</h2>
        <p>This show is private and is not listed in Apple Podcasts or other directories. If you have the subscribe link, add it as a show URL in your podcast app (Apple Podcasts: Library → ⋯ → Follow a Show by URL; Overcast: Add URL).</p>
      </section>
"""
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex, nofollow, noarchive">
  <title>Keystone Grid Radio</title>
  <link rel="icon" href="cover.png">
  <style>
    :root {{ color-scheme: dark; }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
      background: #061018;
      color: #e8edf3;
      line-height: 1.5;
    }}
    main {{
      max-width: 720px;
      margin: 0 auto;
      padding: 48px 24px 80px;
    }}
    img.cover {{
      width: 280px;
      height: 280px;
      border-radius: 28px;
      display: block;
      margin: 0 auto 32px;
      box-shadow: 0 24px 60px rgba(0,0,0,.45);
    }}
    h1 {{
      font-weight: 700;
      letter-spacing: .08em;
      text-align: center;
      margin: 0 0 8px;
      font-size: 1.6rem;
    }}
    .tag {{
      text-align: center;
      color: #d4a848;
      letter-spacing: .18em;
      font-size: .78rem;
      text-transform: uppercase;
      margin-bottom: 28px;
    }}
    p {{ color: #c5d0dc; }}
    .card {{
      background: #0b1d36;
      border: 1px solid #1c3354;
      border-radius: 16px;
      padding: 20px 24px;
      margin-top: 24px;
    }}
    h2 {{ margin: 0 0 10px; font-size: 1.05rem; }}
    .url {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: .85rem;
      word-break: break-all;
      background: #061018;
      padding: 12px;
      border-radius: 8px;
      color: #f2f4f7;
    }}
    ul {{ padding-left: 1.2em; }}
    footer {{
      margin-top: 40px;
      text-align: center;
      color: #7f8ea0;
      font-size: .85rem;
    }}
  </style>
</head>
<body>
  <main>
    <img class="cover" src="cover.png" width="280" height="280" alt="Keystone Grid Radio cover">
    <h1>KEYSTONE GRID RADIO</h1>
    <div class="tag">The Mid-Atlantic grid briefing</div>
    <p>A short daily briefing on the Mid-Atlantic power grid, infrastructure, and the work of the Keystone family of companies.</p>
    {subscribe_block}
    <footer>Private feed. Not listed in podcast directories.</footer>
  </main>
</body>
</html>
"""
    path = PUBLIC_DIR / "index.html"
    path.write_text(html, encoding="utf-8")
    return path


def _html(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------
# GitHub API
# ---------------------------------------------------------------------------

def gh_request(
    token: str,
    method: str,
    url: str,
    data: Optional[bytes] = None,
    content_type: str = "application/json",
    timeout: int = 120,
) -> tuple[int, dict | list | str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }
    if data is not None:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            status = resp.status
            ctype = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        body = e.read()
        status = e.code
        ctype = e.headers.get("Content-Type", "") if e.headers else ""
        if status >= 400:
            snippet = body.decode("utf-8", "replace")[:400]
            # Never echo Authorization or tokens if they somehow appear
            snippet = re.sub(r"gh[pousr]_[A-Za-z0-9_]+", "[redacted]", snippet)
            snippet = re.sub(r"github_pat_[A-Za-z0-9_]+", "[redacted]", snippet)
            raise PublishError(f"GitHub {method} {url} failed ({status}): {snippet}") from None
    if not body:
        return status, {}
    if "application/json" in ctype:
        return status, json.loads(body.decode("utf-8"))
    return status, body.decode("utf-8", "replace")


def repo_api(cfg: dict, path: str) -> str:
    return f"https://api.github.com/repos/{cfg['github_owner']}/{cfg['github_repo']}{path}"


def release_asset_url(cfg: dict, tag: str, asset_name: str) -> str:
    return (
        f"https://github.com/{cfg['github_owner']}/{cfg['github_repo']}"
        f"/releases/download/{tag}/{asset_name}"
    )


def get_release_by_tag(token: str, cfg: dict, tag: str) -> Optional[dict]:
    try:
        _, data = gh_request(token, "GET", repo_api(cfg, f"/releases/tags/{tag}"))
        return data if isinstance(data, dict) else None
    except PublishError as e:
        if "404" in str(e):
            return None
        raise


def list_all_releases(token: str, cfg: dict) -> list[dict]:
    releases = []
    page = 1
    while True:
        _, data = gh_request(
            token,
            "GET",
            repo_api(cfg, f"/releases?per_page=100&page={page}"),
        )
        if not isinstance(data, list) or not data:
            break
        releases.extend(data)
        if len(data) < 100:
            break
        page += 1
    return releases


def create_or_update_release(token: str, cfg: dict, ep: Episode) -> dict:
    existing = get_release_by_tag(token, cfg, ep.tag)
    payload = json.dumps({
        "tag_name": ep.tag,
        "name": ep.title,
        "body": ep.description,
        "draft": False,
        "prerelease": False,
    }).encode("utf-8")
    if existing:
        _, data = gh_request(
            token, "PATCH", repo_api(cfg, f"/releases/{existing['id']}"), data=payload
        )
        return data
    _, data = gh_request(token, "POST", repo_api(cfg, "/releases"), data=payload)
    return data


def upload_asset(token: str, release: dict, mp3: Path) -> dict:
    # Delete an existing asset with the same name so re-publishes replace it
    for asset in release.get("assets") or []:
        if asset.get("name") == mp3.name:
            gh_request(token, "DELETE", asset["url"])
            break
    upload_url = release["upload_url"].split("{", 1)[0]
    url = f"{upload_url}?name={urllib.parse.quote(mp3.name)}"
    data = mp3.read_bytes()
    _, result = gh_request(
        token,
        "POST",
        url,
        data=data,
        content_type="audio/mpeg",
        timeout=300,
    )
    if not isinstance(result, dict) or not result.get("browser_download_url"):
        raise PublishError(f"Upload of {mp3.name} did not return a download URL")
    return result


def delete_release(token: str, release: dict) -> None:
    gh_request(token, "DELETE", release["url"])
    # The tag is not always removed with the release; try to delete it too
    tag = release.get("tag_name")
    if tag:
        # ref delete URL is on the repo
        try:
            owner_repo = "/".join(urllib.parse.urlparse(release["url"]).path.split("/")[2:4])
            gh_request(token, "DELETE", f"https://api.github.com/repos/{owner_repo}/git/refs/tags/{tag}")
        except PublishError:
            pass


def prune_releases(token: str, cfg: dict) -> list[str]:
    releases = list_all_releases(token, cfg)
    # Sort by published_at descending; keep latest max_episodes
    def key(r):
        return r.get("published_at") or r.get("created_at") or ""
    releases.sort(key=key, reverse=True)
    keep = int(cfg.get("max_episodes") or 30)
    removed = []
    for r in releases[keep:]:
        tag = r.get("tag_name") or r.get("id")
        delete_release(token, r)
        removed.append(str(tag))
    return removed


def episodes_from_releases(token: str, cfg: dict, local_fallback: dict[str, Path]) -> list[Episode]:
    """Build Episode objects from GitHub releases, using local files for duration/script when present."""
    episodes = []
    for rel in list_all_releases(token, cfg):
        tag = rel.get("tag_name") or ""
        assets = [a for a in (rel.get("assets") or []) if a.get("name", "").endswith(".mp3")]
        if not assets:
            continue
        asset = assets[0]
        name = asset["name"]
        local = local_fallback.get(name)
        if local and local.exists():
            ep = build_episode(local, cfg, enclosure_url=asset.get("browser_download_url") or release_asset_url(cfg, tag, name))
        else:
            parsed = parse_mp3_name(Path(name))
            if not parsed:
                continue
            date, edition = parsed
            # Duration unknown if the file isn't local; fall back to size-based estimate only if needed
            duration = 0
            ep = Episode(
                date=date,
                edition=edition,
                mp3_path=Path(name),
                script_path=None,
                title=rel.get("name") or format_title(date, edition),
                description=(rel.get("body") or "A new episode of Keystone Grid Radio.").strip(),
                pub_date=default_pub_datetime(date, edition, "", Path(name)),
                duration_sec=duration,
                size=int(asset.get("size") or 0),
                guid=f"kgr-{tag}",
                asset_name=name,
                tag=tag,
                enclosure_url=asset.get("browser_download_url") or release_asset_url(cfg, tag, name),
            )
        episodes.append(ep)
    episodes.sort(key=lambda e: e.pub_date, reverse=True)
    return episodes[: int(cfg.get("max_episodes") or 30)]


# ---------------------------------------------------------------------------
# Git push of the site (feed.xml, cover, index)
# ---------------------------------------------------------------------------

def git_remote_url(cfg: dict, token: Optional[str] = None) -> str:
    owner, repo = cfg["github_owner"], cfg["github_repo"]
    if token:
        # Use x-access-token so the token is not stored in a remote URL we might print
        return f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"
    return f"https://github.com/{owner}/{repo}.git"


def push_site(cfg: dict, token: str, message: str) -> None:
    """Commit public site files to the feed repo and push main.

    Clones into a temp dir (does not keep a persistent checkout). The token
    is passed via a one-shot remote URL in memory and is not written to disk
    or printed.
    """
    owner, repo = cfg["github_owner"], cfg["github_repo"]
    with tempfile.TemporaryDirectory(prefix="kgr-feed-") as tmp:
        tmp_path = Path(tmp)
        # Prefer gh if it already has auth; otherwise token remote
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GH_TOKEN"] = token
        env["GITHUB_TOKEN"] = token
        clone = subprocess.run(
            ["git", "clone", "--depth", "1", f"https://github.com/{owner}/{repo}.git", str(tmp_path / "repo")],
            capture_output=True,
            text=True,
            env=env,
        )
        repo_dir = tmp_path / "repo"
        if clone.returncode != 0:
            # Maybe the repo is empty or not created yet
            err = clone.stderr or clone.stdout or ""
            err = re.sub(r"gh[pousr]_[A-Za-z0-9_]+", "[redacted]", err)
            err = re.sub(r"github_pat_[A-Za-z0-9_]+", "[redacted]", err)
            if "Repository not found" in err or "not found" in err.lower():
                raise PublishError(
                    f"GitHub repo {owner}/{repo} does not exist or this token cannot see it. "
                    "Create a public empty repository with that name, then rerun."
                )
            # Empty repo: init and push
            repo_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
        # Copy site files
        dest_public = repo_dir / "public"
        dest_public.mkdir(exist_ok=True)
        for name in ("feed.xml", "index.html", "cover.png", "cover.jpg"):
            src = PUBLIC_DIR / name
            if src.exists():
                (dest_public / name).write_bytes(src.read_bytes())
        # Root support files
        for src_name, dest_name in (
            (FEED_DIR / "build.sh", "build.sh"),
            (FEED_DIR / "render.yaml", "render.yaml"),
            (FEED_DIR / "repo-gitignore", ".gitignore"),
            (FEED_DIR / "repo-README.md", "README.md"),
        ):
            if Path(src_name).exists():
                (repo_dir / dest_name).write_bytes(Path(src_name).read_bytes())
        subprocess.run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Keystone Grid Radio publisher"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True, capture_output=True)
        st = subprocess.run(["git", "status", "--porcelain"], cwd=repo_dir, capture_output=True, text=True, check=True)
        if not st.stdout.strip():
            log("No site changes to push.")
            return
        subprocess.run(["git", "commit", "-m", message], cwd=repo_dir, check=True, capture_output=True)
        # Push using an in-memory remote; do not log the URL
        push = subprocess.run(
            ["git", "push", git_remote_url(cfg, token), "HEAD:main"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        if push.returncode != 0:
            err = push.stderr or push.stdout or "git push failed"
            err = re.sub(r"https://x-access-token:[^@]+@", "https://x-access-token:[redacted]@", err)
            err = re.sub(r"gh[pousr]_[A-Za-z0-9_]+", "[redacted]", err)
            err = re.sub(r"github_pat_[A-Za-z0-9_]+", "[redacted]", err)
            raise PublishError(f"git push failed: {err}")
        log("Pushed site (feed.xml, cover, landing page) to main. Render will auto-deploy if connected.")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_status(cfg: dict) -> int:
    log(f"Config: {CONFIG_PATH}")
    log(f"GitHub repo: {cfg['github_owner']}/{cfg['github_repo']} (must be public so podcast apps can fetch MP3s)")
    log(f"Feed filename: {cfg['feed_filename']}")
    log(f"Site URL: {cfg.get('site_url') or '(not set yet — filled after Render deploy)'}")
    log(f"Max episodes: {cfg.get('max_episodes')}")
    log(f"GitHub auth: {auth_source()}")
    log(f"Render CLI: {'yes' if shutil_which('render') else 'not installed'}")
    log(f"Official local episodes: {len(official_episodes())}")
    for p in official_episodes():
        log(f"  {p.name} ({p.stat().st_size} bytes)")
    return 0


def shutil_which(name: str) -> Optional[str]:
    from shutil import which
    return which(name)


def cmd_dry_run(cfg: dict, mp3: Optional[Path]) -> int:
    paths = [mp3] if mp3 else official_episodes()
    if not paths:
        raise PublishError("No official episodes found to build a feed from.")
    episodes = []
    for p in paths:
        ep = build_episode(p, cfg, enclosure_url=release_asset_url(cfg, parse_tag(p), p.name))
        episodes.append(ep)
        log(f"  {ep.tag}: {ep.title}")
        log(f"    duration {format_duration(ep.duration_sec)}  size {ep.size}")
        log(f"    {ep.description[:160]}{'…' if len(ep.description)>160 else ''}")
    episodes.sort(key=lambda e: e.pub_date, reverse=True)
    episodes = episodes[: int(cfg.get("max_episodes") or 30)]
    write_landing(cfg, inject_feed_url=bool(cfg.get("site_url")))
    path = write_feed(cfg, episodes)
    # Validate XML
    ET.parse(path)
    log(f"Wrote {path} ({path.stat().st_size} bytes, {len(episodes)} items). XML is well-formed.")
    log(f"Landing page: {PUBLIC_DIR / 'index.html'}")
    log("Dry run only — nothing was uploaded or deployed.")
    return 0


def parse_tag(mp3: Path) -> str:
    date, edition = parse_mp3_name(mp3)
    return date.isoformat() if edition == "weekday" else f"{edition}-{date.isoformat()}"


def publish_one(token: str, cfg: dict, mp3: Path, push: bool = True) -> Episode:
    ep = build_episode(mp3, cfg)
    log(f"Publishing {ep.tag}: {ep.title}")
    release = create_or_update_release(token, cfg, ep)
    asset = upload_asset(token, release, mp3)
    ep.enclosure_url = asset.get("browser_download_url") or release_asset_url(cfg, ep.tag, ep.asset_name)
    log(f"  asset uploaded ({ep.size} bytes)")
    return ep


def rebuild_and_deploy(token: str, cfg: dict, message: str, push: bool = True) -> Path:
    local = {p.name: p for p in official_episodes()}
    # Also index by tag aliases
    episodes = episodes_from_releases(token, cfg, local)
    if not episodes:
        raise PublishError("No releases with MP3 assets found; publish an episode first.")
    write_landing(cfg, inject_feed_url=bool(cfg.get("site_url")))
    path = write_feed(cfg, episodes)
    ET.parse(path)
    log(f"Feed rebuilt with {len(episodes)} episode(s): {path}")
    if push:
        push_site(cfg, token, message)
    return path


def cmd_publish(cfg: dict, mp3: Path, dry_run: bool) -> int:
    if dry_run:
        return cmd_dry_run(cfg, mp3)
    token = require_token()
    publish_one(token, cfg, mp3)
    removed = prune_releases(token, cfg)
    if removed:
        log(f"Pruned old releases: {', '.join(removed)}")
    rebuild_and_deploy(token, cfg, f"Publish {mp3.name}")
    log("Done.")
    if cfg.get("site_url"):
        log(f"Feed URL: {feed_url(cfg)}")
    else:
        log("Set site_url in config.json after the first Render deploy, then rerun --rebuild-feed.")
    return 0


def cmd_backfill(cfg: dict, dry_run: bool) -> int:
    paths = official_episodes()
    if not paths:
        raise PublishError("No official episodes found in /workspace/podcast/.")
    paths = paths[-int(cfg.get("max_episodes") or 30):]
    log(f"Backfilling {len(paths)} episode(s).")
    if dry_run:
        return cmd_dry_run(cfg, None)
    token = require_token()
    for p in paths:
        publish_one(token, cfg, p)
        time.sleep(0.5)
    removed = prune_releases(token, cfg)
    if removed:
        log(f"Pruned old releases: {', '.join(removed)}")
    rebuild_and_deploy(token, cfg, f"Backfill {len(paths)} episodes")
    log("Backfill done.")
    return 0


def cmd_rebuild(cfg: dict, dry_run: bool) -> int:
    if dry_run:
        return cmd_dry_run(cfg, None)
    token = require_token()
    rebuild_and_deploy(token, cfg, "Rebuild podcast feed")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mp3", nargs="?", help="Path to an official episode MP3")
    parser.add_argument("--backfill", action="store_true", help="Publish existing official episodes (max 30)")
    parser.add_argument("--rebuild-feed", action="store_true", help="Regenerate feed.xml from current releases")
    parser.add_argument("--dry-run", action="store_true", help="Build a local feed; do not upload or push")
    parser.add_argument("--status", action="store_true", help="Show config and auth (no secrets)")
    args = parser.parse_args(argv)

    try:
        cfg = load_config()
        if args.status:
            return cmd_status(cfg)
        if args.backfill:
            return cmd_backfill(cfg, args.dry_run)
        if args.rebuild_feed:
            return cmd_rebuild(cfg, args.dry_run)
        if args.mp3:
            return cmd_publish(cfg, Path(args.mp3).expanduser().resolve(), args.dry_run)
        if args.dry_run:
            return cmd_dry_run(cfg, None)
        parser.print_help()
        return 2
    except PublishError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
