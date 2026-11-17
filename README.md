# Keystone Grid Radio — private podcast feed

This repository holds only the public static site for the Keystone Grid Radio podcast: the cover art, a short landing page, and the RSS feed. Episode audio is stored as GitHub Release assets and is not committed here.

The feed is unlisted (`itunes:block` is set) and is not submitted to Apple Podcasts, Spotify, or other directories. Anyone who has the feed URL, or who can browse this public repository, can listen. Do not advertise the repo or the feed URL.

New episodes are published from the production box with:

```
/workspace/podcast/feed/publish.sh /workspace/podcast/keystone-grid-radio-YYYY-MM-DD.mp3
```

The site is built for a free Render static site. See `render.yaml`.
