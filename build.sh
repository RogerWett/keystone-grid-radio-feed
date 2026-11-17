#!/usr/bin/env bash
# Render build: copy the static site and publish the feed under the secret filename.
# FEED_SECRET is a Render environment variable (the hex suffix only, no "feed-" prefix
# or with it — both work). SITE_URL is the public https origin, e.g.
# https://keystone-grid-radio.onrender.com
set -euo pipefail
rm -rf dist
mkdir -p dist
cp -a public/. dist/
# Cover + landing + canonical feed.xml are already in public/

secret="${FEED_SECRET:-}"
if [[ -n "$secret" ]]; then
  secret="${secret#feed-}"
  secret="${secret%.xml}"
  cp public/feed.xml "dist/feed-${secret}.xml"
  # Do not publish the obvious /feed.xml path; only the unguessable filename.
  rm -f dist/feed.xml
fi

# Optional: inject the subscribe URL into the landing page at build time
# so the secret is not stored in git. A marker file is used if present.
if [[ -n "${SITE_URL:-}" && -n "${secret:-}" ]]; then
  python3 - << PY
from pathlib import Path
import os, re
html = Path("dist/index.html").read_text(encoding="utf-8")
site = os.environ["SITE_URL"].rstrip("/")
secret = os.environ["FEED_SECRET"]
secret = secret.removeprefix("feed-").removesuffix(".xml")
url = f"{site}/feed-{secret}.xml"
# If the committed page has no URL yet, replace the generic subscribe section.
card = f'''      <section class="card">
        <h2>Subscribe</h2>
        <p>This show is private and is not listed in Apple Podcasts or other directories. Add it by URL in your podcast app:</p>
        <p class="url">{url}</p>
        <ul>
          <li><strong>Apple Podcasts:</strong> Library → ⋯ → Follow a Show by URL</li>
          <li><strong>Overcast:</strong> Add URL</li>
          <li><strong>Pocket Casts:</strong> Discover → then paste the feed URL</li>
        </ul>
      </section>'''
html2, n = re.subn(r'<section class="card">\s*<h2>Subscribe</h2>.*?</section>', card, html, count=1, flags=re.S)
if n:
    Path("dist/index.html").write_text(html2, encoding="utf-8")
PY
fi
echo "build ok"
