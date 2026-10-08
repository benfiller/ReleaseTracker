#!/usr/bin/env python3
"""
Release tracker: local web app.

Run:  python releases.py
This starts a small server on 127.0.0.1 and opens the page in your browser. The page asks the
server to refresh any stale artists each time it opens (nothing refreshes while no page is open).
Standard library only.

Files (all next to this script): releases.db (created automatically), index.html.
"""
import hashlib
import json
import mimetypes
import os
import re
import socket
import sqlite3
import sys
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "releases.db"
INDEX_PATH = HERE / "index.html"

HOST, PORT = "127.0.0.1", 8765
API_BASE = "https://itunes.apple.com"
REQUEST_DELAY = 3.0                 # seconds between Apple requests, across everything (limit is ~20/min)
REQUEST_BURST = 3                   # ...except that this many may go out back to back after a quiet spell
BATCH_SIZE = 25                     # artists looked up per request during a refresh
RECENT_LIMIT = 10                   # newest catalog entries requested per artist in a batch
FULL_LOOKUP_LIMIT = 200             # most entries Apple returns for one artist (the full catalog lookup)
WIDEN_STEPS = (20, 30, 50, 100, 200)  # if the newest 10 are all unfamiliar: ask for the newest 20, then 30, ...
STALE_AFTER = timedelta(hours=6)    # artists checked more recently are skipped
PHOTO_RECHECK_AFTER = timedelta(days=30)   # how often to re-check an artist's photo
MAX_PHOTO_RECHECKS = 10             # photo re-checks per refresh (spreads them out; missing photos are always fetched)
# For artists at Apple's 200-entry catalog limit, periodically redo the full catalog lookup
# so a release that fell outside the lookup window gets another chance to be found.
CATALOG_RECHECK_AFTER = timedelta(days=60)  # how often to redo a full catalog lookup for a capped artist
MAX_CATALOG_RECHECKS = 3            # full rechecks per refresh (each costs 2 requests; spreads the cost out)
BATCH_RETRIES = 2                   # extra tries for a failed batch lookup before falling back to one-by-one
BATCH_RETRY_DELAY = 4.0             # seconds to wait between those tries
CLOSE_GRACE_SECONDS = 6.0           # after the last page closes, wait this long before quitting
PREVIEW_CACHE_SECONDS = 600
BACKUP_DIR = HERE / "backups"
BACKUPS_KEPT = 7                    # daily copies of releases.db to keep
LOG_MAX_BYTES = 512 * 1024      # tracker.log rotates to tracker.log.old past this size
IMAGE_CACHE_DIR = HERE / "image_cache"   # Apple artwork, cached to disk forever so the browser's
IMAGE_CACHE_DIR.mkdir(exist_ok=True)     # own (volatile) cache clearing doesn't mean a slow re-fetch
EXPORT_BATCH = 8                    # albums looked up per request when building a playlist export
EXPORT_MAX_RELEASES = 400           # most releases one export request may name
ISRC_KEEP_FOR = timedelta(days=90)     # cached ISRC matches older than this are looked up again
TRACKS_KEEP_FOR = timedelta(days=30)   # stored songs of exported albums older than this are deleted
LISTEN_LATER_HIDDEN_KEEP_FOR = timedelta(days=1)   # hidden Listen later entries are deleted after this long
FEED_DEFAULT_DAYS = 183             # feed hides releases older than this (0 = no limit)
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# Identifies this exact version of the code, so a relaunch can tell whether the
# tracker already running is stale (e.g. you replaced this file but never quit it).
_stat = Path(__file__).stat()
CODE_STAMP = f"{int(_stat.st_mtime)}-{_stat.st_size}"

SCHEMA = """
CREATE TABLE IF NOT EXISTS artists (
    id           INTEGER PRIMARY KEY,   -- iTunes artistId
    name         TEXT NOT NULL,
    added_at     TEXT NOT NULL,
    last_checked TEXT,
    photo        TEXT,                  -- NULL = not looked up yet, '' = looked up, none available
    photo_checked_at TEXT,
    favorite     INTEGER NOT NULL DEFAULT 0,
    -- 0 = catalog complete, 2 = Apple's 200-entry limit reached (both windows stored, see fetch_full_catalog)
    catalog_capped INTEGER NOT NULL DEFAULT 0,
    -- last full re-fetch of a capped artist's catalog (see CATALOG_RECHECK_AFTER). NULL means "due";
    -- MAX_CATALOG_RECHECKS spreads a first pass over several refreshes rather than doing them all at once.
    catalog_rechecked_at TEXT
);

-- names the user muted: any release whose credit includes one of them is left out everywhere
CREATE TABLE IF NOT EXISTS muted_names (
    norm_name TEXT PRIMARY KEY,         -- normalize_name(name)
    name      TEXT NOT NULL,            -- as shown to the user
    muted_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS releases (
    id           INTEGER NOT NULL,      -- iTunes collectionId
    artist_id    INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    title        TEXT NOT NULL,
    norm_title   TEXT NOT NULL,         -- normalized title used for dedup
    release_date TEXT,
    url          TEXT,
    artwork      TEXT,
    track_count  INTEGER,
    first_seen   TEXT NOT NULL,
    is_baseline  INTEGER NOT NULL DEFAULT 0,  -- 1 = existed when artist was added
    ignored      INTEGER NOT NULL DEFAULT 0,  -- 1 = hidden from the feed (listened to)
    removed      INTEGER NOT NULL DEFAULT 0,  -- 1 = removed from this artist's catalog, counts and feed
    kind         TEXT NOT NULL DEFAULT 'own', -- 'own', or 'feature' (the artist "appears on" it)
    credit       TEXT,                  -- the artist credit Apple shows, e.g. "Artist A & Artist B"
    primary_artist_id INTEGER,          -- the artist Apple files the release under
    explicitness TEXT,                  -- Apple's collectionExplicitness: 'explicit', 'cleaned', 'notExplicit'
    -- one row per (release, tracked artist): a joint release of two tracked artists is in both
    PRIMARY KEY (id, artist_id)
);

CREATE INDEX IF NOT EXISTS idx_releases_artist_norm
    ON releases(artist_id, norm_title);

-- songs of released albums, looked up for playlist exports (a released album's songs don't change,
-- so each album is only looked up once); tracks_fetched marks which albums are stored
CREATE TABLE IF NOT EXISTS tracks (
    collection_id INTEGER NOT NULL,     -- iTunes collectionId of the album/EP/single
    track_id      INTEGER NOT NULL,
    disc          INTEGER,
    number        INTEGER,
    title         TEXT NOT NULL,
    artist        TEXT,                 -- the song's own credit, e.g. "A & B"
    duration_ms   INTEGER,
    PRIMARY KEY (collection_id, track_id)
);

-- the Listen later queue. A snapshot of the release, not a reference to `releases`: entries stay
-- when the artist they came from is untracked (deleting an artist deletes its `releases` rows).
CREATE TABLE IF NOT EXISTS listen_later (
    id           INTEGER PRIMARY KEY,   -- iTunes collectionId
    artist_id    INTEGER,               -- tracked artist it was queued from (may not be tracked any more)
    for_artist   TEXT,                  -- that artist's name at the time
    kind         TEXT NOT NULL DEFAULT 'own',   -- 'own' or 'feature', as in `releases`
    title        TEXT NOT NULL,
    release_date TEXT,
    url          TEXT,
    artwork      TEXT,
    track_count  INTEGER,
    credit       TEXT,
    added_at     TEXT NOT NULL,
    hidden       INTEGER NOT NULL DEFAULT 0,    -- 1 = hidden from the feed (listened to)
    hidden_at    TEXT                           -- when it was hidden (NULL while not hidden); see sweep_old_listen_later
);

CREATE TABLE IF NOT EXISTS tracks_fetched (
    collection_id INTEGER PRIMARY KEY,
    fetched_at    TEXT NOT NULL
);

-- ISRCs found on Deezer for Apple songs, so exporting the same songs again doesn't repeat the lookups.
-- Only matches are kept (a miss is tried again next time, so a better matcher helps past misses);
-- entries expire after ISRC_KEEP_FOR in case a match was wrong.
CREATE TABLE IF NOT EXISTS isrc_cache (
    track_id INTEGER PRIMARY KEY,       -- iTunes trackId
    isrc     TEXT NOT NULL,
    found_at TEXT NOT NULL
);
"""


# ---------------------------------------------------------------- helpers

def now_iso():
    return datetime.now(timezone.utc).strftime(TS_FORMAT)


def log(msg):
    print(f"{now_iso()} {msg}", flush=True)


# Bracketed chunks containing these words are stripped before comparing
# titles. A re-recording marker (e.g. "(Redone Version)") is deliberately NOT here: a
# re-recording is arguably a different release.
NOISE_WORDS = (
    "deluxe", "remaster", "remastered", "edition", "expanded", "anniversary",
    "bonus", "explicit", "clean", "reissue", "special", "collector",
    "super deluxe", "digital",
)
_BRACKETS = re.compile(r"[(\[]([^)\]]*)[)\]]")
_TRAILING_TYPE = re.compile(r"\s+-\s+(single|ep)\s*$", re.IGNORECASE)
_TRAILING_NOISE = re.compile(
    r"\s+-\s+.*\b(deluxe|remaster(ed)?|edition|expanded|anniversary)\b.*$",
    re.IGNORECASE,
)


def normalize_title(title):
    """Reduce a release title to a comparable form."""
    t = title.strip()

    def drop_noisy(match):
        inner = match.group(1).lower()
        return "" if any(w in inner for w in NOISE_WORDS) else match.group(0)

    t = _BRACKETS.sub(drop_noisy, t)
    t = _TRAILING_TYPE.sub("", t)
    t = _TRAILING_NOISE.sub("", t)
    t = t.lower()
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


# ---------------------------------------------------------------- own vs "appears on"

_SEPARATORS = re.compile(
    r"\s*(?:&|\+|,|/|×|\bx\b|\band\b|\bfeat\.?|\bfeaturing\b|\bwith\b|\bvs\.?)\s*",
    re.IGNORECASE)


def normalize_name(name):
    n = unicodedata.normalize("NFKD", name or "")
    n = "".join(c for c in n if not unicodedata.combining(c)).casefold()
    n = re.sub(r"[^\w\s]", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def is_credited(artist_name, credit):
    """
    True if artist_name is one of the names in an artist credit such as
    "Artist A & Artist B" or "A feat. B". A single-word name must match a whole name in the
    credit; a longer name may also appear as a run of words inside it.
    """
    target = normalize_name(artist_name)
    if not target or not credit:
        return False
    if " " in target:
        full = normalize_name(credit)
        if re.search(rf"(?<!\w){re.escape(target)}(?!\w)", full):
            return True
    segments = [seg for seg in (normalize_name(p) for p in _SEPARATORS.split(credit)) if seg]
    return any(" ".join(segments[i:j + 1]) == target
               for i in range(len(segments)) for j in range(i, len(segments)))


def classify_release(artist_id, artist_name, primary_artist_id, credit):
    """
    'own'     the release is filed under this artist, or names them in its credit
              (a joint release like "Artist A & Artist B" belongs to both).
    'feature' anything else Apple lists for the artist, i.e. its "appears on" section.
    With no information to go on, assume 'own' so nothing disappears.
    """
    if primary_artist_id is None and not credit:
        return "own"
    if primary_artist_id is not None and primary_artist_id == artist_id:
        return "own"
    if is_credited(artist_name, credit):
        return "own"
    return "feature"


def split_by_kind(artist_id, artist_name, releases):
    """(distinct own releases, distinct features) from a raw list of Apple releases."""
    own, features = [], []
    for r in releases:
        item = {**r, "norm_title": normalize_title(r["title"])}
        kind = classify_release(artist_id, artist_name, r.get("primary_artist_id"), r.get("credit"))
        (own if kind == "own" else features).append(item)
    return distinct_releases(own), distinct_releases(features)


# ---------------------------------------------------------------- iTunes API

class RateLimited(RuntimeError):
    """Apple is refusing requests because we've made too many (HTTP 403 or 429)."""


class RateLimiter:
    """
    Spaces requests at least `interval` seconds apart across all threads, letting up to `burst` of them
    go out back to back after a quiet spell (burst=1: strictly evenly spaced). Thread-safe: callers are
    handed their slots in the order they ask, and each sleeps until its own slot comes up.
    """
    def __init__(self, interval, burst=1):
        self.interval = interval
        self.burst = burst
        self.lock = threading.Lock()
        self.next = 0.0          # when the next slot would be due if nobody were allowed a burst

    def wait(self):
        with self.lock:
            now = time.monotonic()
            due = max(self.next, now)
            delay = due - (self.burst - 1) * self.interval - now
            self.next = due + self.interval
        if delay > 0:
            time.sleep(delay)


# Every request to itunes.apple.com goes through api_get, and api_get waits on this, so a refresh, a
# search, an add-artist and an export running at the same time share one budget instead of each
# assuming it has the whole thing to itself.
APPLE_LIMITER = RateLimiter(REQUEST_DELAY, REQUEST_BURST)


def api_get(endpoint, **params):
    APPLE_LIMITER.wait()
    url = f"{API_BASE}/{endpoint}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "release-tracker/0.2"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            raise RateLimited(f"iTunes API returned HTTP {e.code} (too many requests)") from e
        raise RuntimeError(f"iTunes API returned HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not reach iTunes API: {e.reason}") from e
    except OSError as e:                # a timeout while reading the reply
        raise RuntimeError(f"Could not reach iTunes API: {e}") from e
    except ValueError as e:             # the reply wasn't valid JSON
        raise RuntimeError(f"iTunes API sent an unreadable reply: {e}") from e


def search_artists(term, limit=8):
    data = api_get("search", term=term, entity="musicArtist", limit=limit)
    return [r for r in data.get("results", []) if r.get("artistId")]


# https://music.apple.com/us/artist/some-name/123456789, with or without the country, the name slug,
# or an "id" prefix (older itunes.apple.com links), or the music:// form
_ARTIST_URL = re.compile(
    r"(?:https?|music)://(?:geo\.)?(?:music|itunes)\.apple\.com/(?:[a-z]{2}/)?artist/(?:[^/?#]+/)?(?:id)?(\d+)",
    re.IGNORECASE)


def artist_id_from_url(text):
    m = _ARTIST_URL.search(text or "")
    return int(m.group(1)) if m else None


def lookup_artist(artist_id):
    data = api_get("lookup", id=artist_id)
    return [r for r in data.get("results", []) if r.get("wrapperType") == "artist" and r.get("artistId")]


def parse_collection(r):
    return {
        "id": r["collectionId"],
        "title": r.get("collectionName", "(untitled)"),
        "release_date": r.get("releaseDate"),
        "url": r.get("collectionViewUrl"),
        "artwork": r.get("artworkUrl100"),
        "track_count": r.get("trackCount"),
        "primary_artist_id": r.get("artistId"),
        "credit": r.get("artistName"),
        "explicitness": r.get("collectionExplicitness"),
    }


def fetch_releases(artist_id):
    """All albums/EPs/singles the catalog lists for this artist ID (max 200)."""
    data = api_get("lookup", id=artist_id, entity="album", limit=FULL_LOOKUP_LIMIT)
    return [parse_collection(r) for r in data.get("results", [])
            if r.get("wrapperType") == "collection"]


def fetch_full_catalog(artist_id):
    """
    The artist's catalog as far as Apple lets us see it. A lookup returns at most 200
    entries and can't be paged (offset is ignored, limit above 200 is capped). The default
    order and the newest-added order return different sets, so an artist that reaches the
    limit gets a second lookup sorted by newest added and the two lists are merged
    (up to 400 entries). A list of 200 or more entries means the catalog is capped.
    """
    releases = fetch_releases(artist_id)
    if len(releases) >= FULL_LOOKUP_LIMIT:
        newest = fetch_recent_batch([artist_id], limit=FULL_LOOKUP_LIMIT).get(artist_id) or []
        have = {r["id"] for r in releases}
        releases = releases + [r for r in newest if r["id"] not in have]
    return releases


def fetch_recent_batch(artist_ids, limit=RECENT_LIMIT):
    """
    One request covering many artists: each artist's newest catalog entries.
    Apple orders these by when they were added to its catalog (not by release date),
    and applies the limit to each artist separately. The reply is an artist entry
    followed by that artist's releases, so releases are attributed by position, not
    by their own artistId: a collaboration can be filed under the other artist's ID
    yet still belongs in this artist's list.
    Returns {artist_id: [release, ...]}; an artist missing from the reply is absent.
    """
    data = api_get("lookup", id=",".join(str(i) for i in artist_ids),
                   entity="album", limit=limit, sort="recent")
    out, current = {}, None
    for r in data.get("results", []):
        if r.get("wrapperType") == "artist":
            current = r.get("artistId")
            out.setdefault(current, [])
        elif r.get("wrapperType") == "collection" and current is not None:
            out[current].append(parse_collection(r))
    return out


def fetch_recent_batch_with_retry(artist_ids):
    """
    fetch_recent_batch, retried a couple of times on a temporary failure (network not up
    yet after sleep, a timeout, an HTTP 5xx, an empty reply). Returns None if every
    try failed; RateLimited is never retried.
    """
    for attempt in range(BATCH_RETRIES + 1):
        try:
            results = fetch_recent_batch(artist_ids)
            if not results:
                raise RuntimeError("Apple returned an empty reply")
            return results
        except RateLimited:
            raise
        except RuntimeError as e:
            log(f"batch lookup of {len(artist_ids)} artists failed "
                f"(try {attempt + 1}/{BATCH_RETRIES + 1}): {e}")
            if attempt < BATCH_RETRIES:
                time.sleep(BATCH_RETRY_DELAY)
    return None


def fetch_recent_until_known(conn, artist_id, name=""):
    """
    The artist's newest catalog entries, asking for a few more each time until the list
    reaches something we already have (so everything newer is included), or Apple has
    no more to give. Used when the newest 10 were all unfamiliar; asks for the newest 20,
    then 30, 50, 100 and at most 200 (see WIDEN_STEPS). Releases come back newest-added
    first, so unlike the full lookup this can't miss recent releases of a huge catalog.
    """
    known = {row[0] for row in conn.execute("SELECT id FROM releases WHERE artist_id = ?", (artist_id,))}
    releases = None
    for limit in WIDEN_STEPS:
        log(f"widening lookup for {name or artist_id}: newest {limit}")
        releases = fetch_recent_batch([artist_id], limit=limit).get(artist_id)
        if releases is None:
            raise RuntimeError("Apple returned nothing for this artist")
        if len(releases) < limit or any(r["id"] in known for r in releases):
            break
    return releases


BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def resize_mzstatic(url, size=300):
    """Rewrite an Apple image URL to a square thumbnail of the given size."""
    url = (url.replace("{w}", str(size)).replace("{h}", str(size))
              .replace("{c}", "cc").replace("{f}", "jpg"))
    return re.sub(
        r"/\d+x\d+[a-z]*(?:-\d+)?\.(jpg|png|webp)$",
        lambda m: f"/{size}x{size}cc.{m.group(1)}",
        url,
    )


def big_picture(url):
    """
    Artist pictures are shown at 300x300 to match album artwork. Photos saved earlier
    (and cover art used as a stand-in) were stored smaller, so resize the address on
    the way out; only Apple image URLs are touched.
    """
    if url and "mzstatic.com" in url:
        return resize_mzstatic(url, 300)
    return url


def apple_music_app_url(url):
    """The address that opens a release in the Apple Music app: music:// instead of https://, without the
    title slug in the path (Apple ignores it) or the tracking query."""
    if not url:
        return ""
    url = re.sub(r"^https?://", "music://", url)
    url = re.sub(r"/album/[^/]+/", "/album/release/", url, count=1)
    return re.sub(r"\?.*$", "", url)


def parse_artist_photo(html):
    """Pull the og:image URL out of an Apple Music artist page (either attribute order)."""
    for pattern in (
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
    ):
        m = re.search(pattern, html)
        if m and "mzstatic.com" in m.group(1):
            return resize_mzstatic(m.group(1))
    return None


def fetch_artist_photo(artist_id):
    """
    The iTunes API has no artist photos, but the artist's web page does (as its
    Open Graph image). Returns a URL, or None if the page has none.
    Raises RuntimeError on network problems.
    """
    url = f"https://music.apple.com/us/artist/{artist_id}"
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            # The og:image tag is in <head>, so the top of the page is enough.
            html = resp.read(150_000).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(f"Could not load artist page: {e}") from e
    return parse_artist_photo(html)


def _is_apple_image_url(url):
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return False
    host = urllib.parse.urlparse(url).hostname or ""
    return host == "mzstatic.com" or host.endswith(".mzstatic.com")


def proxy_image_url(url):
    """Point an Apple artwork URL at our own /img cache instead of Apple's CDN. Anything
    else (or already-empty) passes through untouched."""
    return "/img?u=" + urllib.parse.quote(url, safe="") if _is_apple_image_url(url) else url


def proxify(obj):
    """Walk a JSON-able response and rewrite every Apple image URL found in it, however
    deeply nested, so every artwork/photo field the client sees is /img-backed."""
    if isinstance(obj, dict):
        return {k: proxify(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [proxify(v) for v in obj]
    if isinstance(obj, str):
        return proxy_image_url(obj)
    return obj


_image_locks = {}
_image_locks_guard = threading.Lock()


def _image_lock(cache_key):
    with _image_locks_guard:
        return _image_locks.setdefault(cache_key, threading.Lock())


def _image_cache_path(url):
    ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"
    return IMAGE_CACHE_DIR / (hashlib.sha1(url.encode("utf-8")).hexdigest() + ext)


def fetch_cached_image(url):
    """Bytes and content-type for an Apple artwork URL, backed by a permanent file on disk
    (unlike the browser's own cache, this survives it being cleared). The first request for
    a given picture fetches it from Apple; every one after is an instant local read. Raises
    RuntimeError on a download failure (an existing cached file is never touched by that)."""
    path = _image_cache_path(url)
    if path.exists():
        return path.read_bytes(), mimetypes.guess_type(path.name)[0] or "image/jpeg"
    try:
        with _image_lock(str(path)):
            if path.exists():   # another request already filled it in while we waited
                return path.read_bytes(), mimetypes.guess_type(path.name)[0] or "image/jpeg"
            req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = resp.read()
                    ctype = resp.headers.get("Content-Type") or mimetypes.guess_type(path.name)[0] or "image/jpeg"
            except (urllib.error.URLError, OSError) as e:
                raise RuntimeError(f"could not fetch image: {e}") from e
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)   # atomic: a request racing this one never sees a half-written file
            return data, ctype
    finally:
        # However this ended (success, failure, or finding the file already there), drop the lock entry so
        # one doesn't linger for every picture that ever failed. A request still waiting on the old lock
        # object simply finds the file (or tries the download itself) once it gets its turn.
        with _image_locks_guard:
            _image_locks.pop(str(path), None)


def cached_image_filenames(conn):
    """The cache filename for every artist photo or release artwork currently stored anywhere in the
    database (visible or not -- a removed release's row survives, and its picture should too, in case it
    comes back). The page is sent the 300x300 form of each address (big_picture), so that is the form
    whose cache file is kept, not the 100x100 form Apple's API gave."""
    urls = set()
    for (photo,) in conn.execute("SELECT photo FROM artists WHERE photo IS NOT NULL AND photo != ''"):
        urls.add(big_picture(photo))
    for (art,) in conn.execute("SELECT artwork FROM releases WHERE artwork IS NOT NULL AND artwork != ''"):
        urls.add(big_picture(art))
    for (art,) in conn.execute("SELECT artwork FROM listen_later WHERE artwork IS NOT NULL AND artwork != ''"):
        urls.add(big_picture(art))
    return {_image_cache_path(u).name for u in urls if _is_apple_image_url(u)}


def sweep_orphaned_images(conn):
    """Delete cached files nothing in the database points to any more — Apple gave an artist
    a new photo (the old one's URL, and its cached file, are simply no longer referenced),
    a release was removed, an artist was untracked. Runs after every refresh. A file still
    being downloaded (.tmp) is left alone even if the sweep catches it mid-write."""
    keep = cached_image_filenames(conn)
    removed = 0
    for path in IMAGE_CACHE_DIR.iterdir():
        if path.suffix == ".tmp" or path.name in keep:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    if removed:
        log(f"image cache: removed {removed} orphaned file(s)")
    return removed


def sweep_old_listen_later(conn):
    """
    Delete Listen later entries that were hidden (listened to) more than LISTEN_LATER_HIDDEN_KEEP_FOR ago.
    The Undo toast covers an accidental hide, and a deleted entry can be queued again from the catalog.
    Runs after every refresh, before the image sweep, so the artwork of a deleted entry is cleaned up in
    the same pass. Only listen_later rows go: the matching `releases` row keeps its ignored flag.
    """
    cutoff = (datetime.now(timezone.utc) - LISTEN_LATER_HIDDEN_KEEP_FOR).strftime(TS_FORMAT)
    with conn:
        removed = conn.execute("DELETE FROM listen_later WHERE hidden = 1 AND hidden_at < ?", (cutoff,)).rowcount
    if removed:
        log(f"listen later: removed {removed} entr{'y' if removed == 1 else 'ies'} hidden for more than "
            f"{LISTEN_LATER_HIDDEN_KEEP_FOR.days} day(s)")
    return removed


def sweep_old_tracks(conn):
    """
    Delete the stored songs of albums that were looked up for an export more than TRACKS_KEEP_FOR ago.
    The songs and their tracks_fetched marker go together: a marker without its songs would make a
    later export think the album has none. Re-exporting an old album just looks it up again.
    Runs after every refresh.
    """
    cutoff = (datetime.now(timezone.utc) - TRACKS_KEEP_FOR).strftime(TS_FORMAT)
    with conn:
        conn.execute("""DELETE FROM tracks WHERE collection_id IN
                        (SELECT collection_id FROM tracks_fetched WHERE fetched_at < ?)""", (cutoff,))
        removed = conn.execute("DELETE FROM tracks_fetched WHERE fetched_at < ?", (cutoff,)).rowcount
    if removed:
        log(f"export songs: removed {removed} album(s) stored for more than {TRACKS_KEEP_FOR.days} days")
    return removed


_cache = {}
_cache_lock = threading.Lock()
_photo_cache = {}


def _drop_expired(cache):
    """Remove entries older than PREVIEW_CACHE_SECONDS (caller holds _cache_lock)."""
    cutoff = time.time() - PREVIEW_CACHE_SECONDS
    for key in [k for k, (stamp, _) in cache.items() if stamp < cutoff]:
        del cache[key]


def cached_photo(artist_id):
    """Photo URL for an artist ('' if none exists, None if the lookup failed)."""
    with _cache_lock:
        hit = _photo_cache.get(artist_id)
        if hit and time.time() - hit[0] < PREVIEW_CACHE_SECONDS:
            return hit[1]
    try:
        photo = fetch_artist_photo(artist_id) or ""
    except RuntimeError:
        return None          # don't cache failures
    with _cache_lock:
        _drop_expired(_photo_cache)
        _photo_cache[artist_id] = (time.time(), photo)
    return photo


def cached_releases(artist_id):
    """Fetch releases, reusing a recent result (preview -> add shares one call)."""
    with _cache_lock:
        hit = _cache.get(artist_id)
        if hit and time.time() - hit[0] < PREVIEW_CACHE_SECONDS:
            return hit[1]
    releases = fetch_full_catalog(artist_id)
    with _cache_lock:
        _drop_expired(_cache)
        _cache[artist_id] = (time.time(), releases)
    return releases


# ---------------------------------------------------------------- database

# Columns the code needs that CREATE TABLE IF NOT EXISTS above would not add to a database that
# already has the table. An old database missing one of these is reported with a clear error
# instead of failing later with a confusing SQL error.
REQUIRED_COLUMNS = {
    "artists": {"id", "name", "added_at", "last_checked", "photo", "photo_checked_at", "favorite",
                "catalog_capped", "catalog_rechecked_at"},
    "releases": {"id", "artist_id", "title", "norm_title", "release_date", "url", "artwork", "track_count",
                 "first_seen", "is_baseline", "ignored", "removed", "kind", "credit", "primary_artist_id",
                 "explicitness"},
}


def check_schema(conn):
    for table, needed in REQUIRED_COLUMNS.items():
        missing = needed - {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if missing:
            raise RuntimeError(
                f"releases.db is out of date: {table} lacks {', '.join(sorted(missing))}. It comes from a much "
                f"older version of the tracker; open it once with that version's releases.py, then use this one.")


# A same-titled release that adds only a couple of tracks within a few days of one we already know
# is treated as the same release, not a new edition -- Apple's reported track count for a release
# can briefly be slightly off and then get corrected. A genuine deluxe edition almost always adds
# more tracks than that, arrives later than that, or both.
DUPLICATE_TRACK_SLACK = 2
DUPLICATE_DATE_WINDOW_DAYS = 7


def is_duplicate(norm_title, track_count, release_date, known):
    """
    A release is a duplicate if we already know its normalized title and either it adds no
    tracks, or it adds only a couple of tracks within a few days of what we know (see
    DUPLICATE_TRACK_SLACK / DUPLICATE_DATE_WINDOW_DAYS above). known maps norm_title -> (max
    tracks seen for it, that release's date).
    """
    if norm_title not in known:
        return False
    known_tracks, known_date = known[norm_title]
    tc = track_count or 0
    if tc <= known_tracks:
        return True
    if tc - known_tracks > DUPLICATE_TRACK_SLACK or not release_date or not known_date:
        return False
    try:
        gap = abs((datetime.strptime(release_date[:10], "%Y-%m-%d")
                   - datetime.strptime(known_date[:10], "%Y-%m-%d")).days)
    except ValueError:
        return False
    return gap <= DUPLICATE_DATE_WINDOW_DAYS


def remember(norm_title, track_count, release_date, known):
    prev_tracks, _ = known.get(norm_title, (0, None))
    known[norm_title] = (max(prev_tracks, track_count or 0), release_date)


_schema_lock = threading.Lock()
_schema_ready = set()


def connect(path=None):
    path = str(path or DB_PATH)
    conn = sqlite3.connect(path, timeout=15)
    conn.execute("PRAGMA foreign_keys = ON")
    # Schema setup runs once per process, under a lock, so two threads connecting at the same
    # moment can't both try to create the tables.
    with _schema_lock:
        if path not in _schema_ready:
            conn.executescript(SCHEMA)
            check_schema(conn)
            _schema_ready.add(path)
    return conn


@contextmanager
def db():
    """
    A database connection for the length of a with-block: committed if the block finishes normally,
    rolled back if it raises, and always closed.
        with db() as conn:
            ...
    """
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def distinct_releases(items):
    """
    Collapse a list of releases to the distinct ones. This is the ONLY place "duplicate" is decided:
    the feed, the catalogs and the artist counts all go through it (see collapsed_releases), so they
    can't disagree, and nothing about it is stored.

    Releases are judged in the order the tracker first saw them (first_seen), then by release date and
    fewer tracks first, so a base edition is judged before its deluxe. A release is dropped only if its
    title is already known and it doesn't meaningfully add anything (see is_duplicate): remasters and
    explicit/clean copies collapse, a deluxe with a real batch of bonus tracks and a same-named single
    each stay. Going by first_seen means the copy you already saw wins over one Apple lists later, which
    is what keeps a hidden release from coming back as its own "duplicate".

    Among copies seen at the same time, the explicit one is judged first, so it is the one kept (it is what
    Apple Music shows by default; the clean copy otherwise tends to win by having an earlier date). When the
    copy kept is the clean one because it was seen first, it borrows the explicit copy's link, so
    "Open in Apple Music" still lands on the explicit version while the release keeps its own id (which
    hiding and Listen later depend on).
    
    items: dicts with norm_title, track_count, release_date, and optionally first_seen and id
    (a catalog fetched from Apple but not stored yet has neither, and is judged by date alone).
    """
    known = {}
    kept = []
    for it in sorted(items, key=lambda r: (r.get("first_seen") or "",
                                           r.get("explicitness") == "cleaned",   # explicit (or unknown) first
                                           r["release_date"] is None,
                                           r["release_date"] or "", r["track_count"] or 0,
                                           r.get("id") or 0)):
        if is_duplicate(it["norm_title"], it["track_count"], it["release_date"], known):
            if it.get("explicitness") == "explicit" and it.get("url"):
                clean = next((k for k in kept if k["norm_title"] == it["norm_title"]
                              and k.get("explicitness") == "cleaned" and not k.get("_link_borrowed")
                              and abs((k["track_count"] or 0) - (it["track_count"] or 0)) <= DUPLICATE_TRACK_SLACK),
                             None)
                if clean is not None:
                    clean["url"] = it["url"]
                    clean["_link_borrowed"] = True   # the first explicit copy judged (fewest tracks) is the one linked
            continue
        kept.append(it)
        remember(it["norm_title"], it["track_count"], it["release_date"], known)
    return kept


def save_artist_with_baseline(conn, artist_id, name, releases, photo=None):
    """
    Add an artist and mark every existing release as baseline (hidden), except releases
    that haven't come out yet: those stay visible so they show up under Upcoming.
    """
    ts = now_iso()
    today = ts[:10]
    rows = []
    for r in releases:
        kind = classify_release(artist_id, name, r.get("primary_artist_id"), r.get("credit"))
        upcoming = bool(r["release_date"]) and r["release_date"][:10] > today
        rows.append((r["id"], artist_id, r["title"], normalize_title(r["title"]), r["release_date"], r["url"],
                     r["artwork"], r["track_count"], ts, 0 if upcoming else 1,
                     kind, r.get("credit"), r.get("primary_artist_id"), r.get("explicitness")))
    with conn:
        conn.execute(
            """INSERT INTO artists (id, name, added_at, last_checked, photo, photo_checked_at, catalog_capped)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (artist_id, name, ts, ts, photo, ts if photo is not None else None,
             2 if len(releases) >= FULL_LOOKUP_LIMIT else 0),
        )
        conn.executemany(
            """INSERT OR IGNORE INTO releases
               (id, artist_id, title, norm_title, release_date, url, artwork,
                track_count, first_seen, is_baseline, kind, credit, primary_artist_id, explicitness)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )


def apply_new_releases(conn, artist_id, releases, baseline_through=None):
    """
    Store releases we haven't seen. Whether one is a duplicate of something already stored is not
    decided here (see distinct_releases): it is worked out whenever releases are read.
    baseline_through="YYYY-MM-DD": unseen releases dated on or before that day are stored
    as baseline (hidden), because they can't be new: they were just outside what we
    fetched before.
    """
    ts = now_iso()
    with conn:
        row = conn.execute("SELECT name FROM artists WHERE id = ?", (artist_id,)).fetchone()
        artist_name = row[0] if row else ""
        known = {rid: rest for rid, *rest in conn.execute(
            "SELECT id, release_date, title, url, artwork, track_count, explicitness FROM releases WHERE artist_id = ?",
            (artist_id,))}
        today = ts[:10]

        for r in releases:
            if r["id"] in known:
                old_date, old_title, old_url, old_art, old_tracks, old_expl = known[r["id"]]
                # Release dates of upcoming releases get moved: keep them current.
                new_date = r["release_date"]
                if old_date and new_date and old_date != new_date and max(old_date, new_date)[:10] > today:
                    conn.execute("UPDATE releases SET release_date = ? WHERE id = ? AND artist_id = ?",
                                 (new_date, r["id"], artist_id))
                # Apple can correct a title, artwork or track count after the fact; keep those current,
                # but never replace a stored value with a missing one. Not touched here: kind, credit and
                # primary_artist_id (they drive own/feature classification and muting), and anything the
                # user owns (first_seen, is_baseline, ignored, removed).
                fresh = (r["title"] or old_title, r["url"] or old_url, r["artwork"] or old_art,
                         r["track_count"] if r["track_count"] is not None else old_tracks)
                if fresh != (old_title, old_url, old_art, old_tracks):
                    conn.execute(
                        "UPDATE releases SET title = ?, norm_title = ?, url = ?, artwork = ?, track_count = ? "
                        "WHERE id = ? AND artist_id = ?",
                        (fresh[0], normalize_title(fresh[0]), fresh[1], fresh[2], fresh[3], r["id"], artist_id))
                if r.get("explicitness") and r["explicitness"] != old_expl:
                    conn.execute("UPDATE releases SET explicitness = ? WHERE id = ? AND artist_id = ?",
                                 (r["explicitness"], r["id"], artist_id))
                continue
            kind = classify_release(artist_id, artist_name, r.get("primary_artist_id"), r.get("credit"))
            baseline = bool(baseline_through and r["release_date"]
                            and r["release_date"][:10] <= baseline_through)
            conn.execute(
                """INSERT OR IGNORE INTO releases
                   (id, artist_id, title, norm_title, release_date, url, artwork,
                    track_count, first_seen, is_baseline, kind, credit, primary_artist_id, explicitness)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (r["id"], artist_id, r["title"], normalize_title(r["title"]), r["release_date"], r["url"],
                 r["artwork"], r["track_count"], ts, int(baseline), kind,
                 r.get("credit"), r.get("primary_artist_id"), r.get("explicitness")),
            )
            known[r["id"]] = (r["release_date"], r["title"], r["url"], r["artwork"], r["track_count"], r.get("explicitness"))
        conn.execute("UPDATE artists SET last_checked = ? WHERE id = ?", (ts, artist_id))


def looks_truncated(conn, artist_id, releases, limit=RECENT_LIMIT):
    """
    True if a newest-first list may have cut off releases we haven't seen. If the list
    is shorter than the limit we got everything. If any release in it is already
    stored, everything newer than that one is in the list too. Only when a full list
    is entirely unfamiliar (say, you were away for months) might more lie beyond it.
    """
    if len(releases) < limit:
        return False
    known = {row[0] for row in conn.execute(
        "SELECT id FROM releases WHERE artist_id = ?", (artist_id,))}
    return not any(r["id"] in known for r in releases)


# ---------------------------------------------------------------- muted names

def load_muted(conn):
    """Display names of everything the user muted."""
    return [r[0] for r in conn.execute("SELECT name FROM muted_names ORDER BY name COLLATE NOCASE")]


def is_muted(credit, muted):
    return bool(credit) and any(is_credited(name, credit) for name in muted)


def tracked_name_norms(conn):
    return {normalize_name(r[0]) for r in conn.execute("SELECT name FROM artists")}


def mute_options(credit, tracked_norms):
    """The names in a credit that could be muted: everyone except the artists being tracked."""
    out, seen = [], set()
    for part in _SEPARATORS.split(credit or ""):
        name = part.strip()
        key = normalize_name(name)
        if key and key not in tracked_norms and key not in seen:
            seen.add(key)
            out.append(name)
    return out


_RELEASE_COLS = ("id", "artist_id", "kind", "title", "norm_title", "release_date", "url", "artwork",
                 "track_count", "first_seen", "credit", "is_baseline", "ignored", "explicitness")


def collapsed_releases(conn, artist_id=None):
    """
    Every release the app should count, grouped by (artist_id, kind), duplicates already collapsed.
    Removed releases and releases crediting a muted name are left out. Hidden (listened-to) and baseline
    releases are kept, because they are still "known" when judging whether a later release is a
    duplicate; callers filter those out for themselves. The feed, the catalogs and the artist counts
    all read releases through here, which is what makes them agree.
    """
    muted = load_muted(conn)
    sql = f"SELECT {', '.join(_RELEASE_COLS)} FROM releases WHERE removed = 0"
    params = ()
    if artist_id is not None:
        sql += " AND artist_id = ?"
        params = (artist_id,)
    groups = {}
    for row in conn.execute(sql, params):
        r = dict(zip(_RELEASE_COLS, row))
        if not is_muted(r["credit"], muted):
            groups.setdefault((r["artist_id"], r["kind"]), []).append(r)
    return {key: distinct_releases(items) for key, items in groups.items()}


def get_feed(conn, days=FEED_DEFAULT_DAYS, limit=500):
    """
    New releases, newest first, each labeled 'own' or 'feature' and whether it is from a
    favorite artist. A release shared by two tracked artists appears once.
    Returns (releases, total): at most `limit` releases plus the whole Listen later queue, and how many
    releases there were before the limit cut the list (so the page can say so).
    """
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    tracked_norms = tracked_name_norms(conn)
    artists = {i: (n, f) for i, n, f in conn.execute("SELECT id, name, favorite FROM artists")}
    queued = {r[0] for r in conn.execute("SELECT id FROM listen_later WHERE hidden = 0")}
    cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d") if days > 0 else None
    rows = []
    for (artist_id, kind), items in collapsed_releases(conn).items():
        if artist_id not in artists:
            continue
        for r in items:
            if r["is_baseline"] or r["ignored"] or r["id"] in queued:
                continue
            if cutoff and r["release_date"] and r["release_date"][:10] < cutoff:
                continue
            rows.append((artist_id, kind, r))
    rows.sort(key=lambda x: x[2]["release_date"] or "", reverse=True)
    by_id = {}
    for artist_id, kind, r in rows:
        artist, fav = artists[artist_id]
        rid, credit, date = r["id"], r["credit"], r["release_date"]
        item = by_id.get(rid)
        if item is None:
            item = by_id[rid] = {
                "id": rid, "title": r["title"], "release_date": date, "app_url": apple_music_app_url(r["url"]),
                "artwork": big_picture(r["artwork"]), "track_count": r["track_count"], "artist": credit or artist,
                "for_artist": artist, "kind": kind,
                "mute_options": mute_options(credit, tracked_norms),
                "upcoming": bool(date) and date[:10] > today,
                "fav_own": False, "fav_any": False,
            }
        elif kind == "own" and item["kind"] != "own":
            item.update(kind="own", artist=credit or artist, for_artist=artist)
        item["fav_any"] = item["fav_any"] or bool(fav)
        if kind == "own":
            item["fav_own"] = item["fav_own"] or bool(fav)
    out = []
    for item in by_id.values():
        fav_own, fav_any = item.pop("fav_own"), item.pop("fav_any")
        item["favorite"] = fav_own if item["kind"] == "own" else fav_any
        out.append(item)
    return out[:limit] + get_listen_later(conn, today), len(out)


def get_listen_later(conn, today=None):
    """The Listen later queue, most recently added first. Not filtered by period or muted names:
    the user picked these releases by hand."""
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = []
    for (rid, for_artist, kind, title, date, url, art, tracks, credit, added, fav) in conn.execute(
            """SELECT l.id, l.for_artist, l.kind, l.title, l.release_date, l.url, l.artwork, l.track_count,
                      l.credit, l.added_at, COALESCE(a.favorite, 0)
               FROM listen_later l LEFT JOIN artists a ON a.id = l.artist_id
               WHERE l.hidden = 0 ORDER BY l.added_at DESC, l.id DESC"""):
        out.append({"id": rid, "title": title, "release_date": date, "app_url": apple_music_app_url(url),
                    "artwork": big_picture(art), "track_count": tracks, "artist": credit or for_artist or "",
                    "for_artist": for_artist or "", "kind": kind, "mute_options": [],
                    "upcoming": bool(date) and date[:10] > today, "favorite": bool(fav), "later": True})
    return out


def set_listen_later(conn, release_id, artist_id, on):
    """Add a release from an artist's catalog to the queue (or take it out). False if it isn't stored."""
    if not on:
        with conn:
            conn.execute("DELETE FROM listen_later WHERE id = ?", (release_id,))
        return True
    row = conn.execute(
        """SELECT r.title, r.release_date, r.url, r.artwork, r.track_count, r.kind, r.credit, a.name
           FROM releases r JOIN artists a ON a.id = r.artist_id
           WHERE r.id = ? AND r.artist_id = ?""", (release_id, artist_id)).fetchone()
    if row is None:
        return False
    title, date, url, art, tracks, kind, credit, artist = row
    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO listen_later
               (id, artist_id, for_artist, kind, title, release_date, url, artwork, track_count, credit, added_at, hidden)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            (release_id, artist_id, artist, kind, title, date, url, art, tracks, credit, now_iso()))
    return True


# ---------------------------------------------------------------- playlist export

def parse_track(r):
    return {
        "id": r["trackId"],
        "disc": r.get("discNumber") or 1,
        "number": r.get("trackNumber") or 0,
        "title": (r.get("trackName") or "").strip(),
        "artist": (r.get("artistName") or "").strip(),
        "duration_ms": r.get("trackTimeMillis"),
    }


def fetch_album_tracks(collection_ids):
    """
    The songs of one or more albums in a single lookup.
    Returns (songs per collection id, the track count Apple reports per collection id, cut_off),
    where cut_off means the reply hit Apple's 200-entry limit and may be incomplete.
    """
    data = api_get("lookup", id=",".join(str(i) for i in collection_ids),
                   entity="song", limit=FULL_LOOKUP_LIMIT)
    results = data.get("results", [])
    songs = {cid: [] for cid in collection_ids}
    expected = {}
    for r in results:
        cid = r.get("collectionId")
        if r.get("wrapperType") == "collection":
            expected[cid] = r.get("trackCount")
        elif r.get("wrapperType") == "track" and r.get("kind") == "song" \
                and cid in songs and r.get("trackId"):
            songs[cid].append(parse_track(r))
    for lst in songs.values():
        lst.sort(key=lambda t: (t["disc"], t["number"]))
    return songs, expected, len(results) >= FULL_LOOKUP_LIMIT


def load_cached_tracks(conn, collection_id):
    return [{"id": r[0], "disc": r[1], "number": r[2], "title": r[3], "artist": r[4], "duration_ms": r[5]}
            for r in conn.execute(
                """SELECT track_id, disc, number, title, artist, duration_ms FROM tracks
                   WHERE collection_id = ? ORDER BY disc, number, track_id""", (collection_id,))]


def store_tracks(conn, collection_id, tracks):
    with conn:
        conn.execute("DELETE FROM tracks WHERE collection_id = ?", (collection_id,))
        conn.executemany(
            """INSERT OR REPLACE INTO tracks (collection_id, track_id, disc, number, title, artist, duration_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [(collection_id, t["id"], t["disc"], t["number"], t["title"], t["artist"], t["duration_ms"])
             for t in tracks])
        conn.execute("INSERT OR REPLACE INTO tracks_fetched (collection_id, fetched_at) VALUES (?, ?)",
                     (collection_id, now_iso()))


def tracks_for_releases(conn, releases):
    """
    releases: [(collection_id, already_out)]. Returns {collection_id: [songs in album order]}.
    Stored albums come from the database; the rest are looked up together in one request.
    An album that comes back short (Apple's reply was cut off, or it is missing) is asked for again
    on its own. Only albums that are already out are stored: an upcoming one can still change.
    """
    result, todo = {}, []
    for cid, out in releases:
        if out and conn.execute("SELECT 1 FROM tracks_fetched WHERE collection_id = ?", (cid,)).fetchone():
            result[cid] = load_cached_tracks(conn, cid)
        else:
            todo.append((cid, out))
    if not todo:
        return result

    def finish(cid, out, songs):
        result[cid] = songs
        if out and songs:
            store_tracks(conn, cid, songs)

    songs, expected, cut_off = fetch_album_tracks([cid for cid, _ in todo])
    retry = []
    for cid, out in todo:
        got, want = songs.get(cid, []), expected.get(cid)
        if len(todo) > 1 and (cut_off or not got or (want and len(got) < want)):
            retry.append((cid, out))
        else:
            finish(cid, out, got)
    for cid, out in retry:
        alone, _, _ = fetch_album_tracks([cid])
        finish(cid, out, alone.get(cid, []))
    return result


# ---------------------------------------------------------------- ISRC lookup (Deezer)
# Soundiiz matches a CSV row to a song in Apple Music by fuzzy title/artist, which can pick the wrong
# song. An ISRC (the code that identifies one recording) makes the match exact, but Apple's free API
# and web pages don't include it. Deezer's public API does, so at export time each song is looked up
# there and the ISRC goes into the CSV. Nothing is stored; a song Deezer can't confidently match is
# left blank (and the reason is logged).
DEEZER_BASE = "https://api.deezer.com"
DEEZER_INTERVAL = 0.12              # seconds between Deezer requests overall (its limit is about 50 per 5 s)
DEEZER_WORKERS = 4                  # songs looked up at the same time
DEEZER_DURATION_TOLERANCE = 4       # seconds a Deezer song's length may differ from Apple's
DEEZER_MAX_FAILURES = 3             # consecutive failed lookups before giving up for this export
DEEZER_LOG_MATCHES = False          # log every successful match too, not just misses/errors (debugging)


def deezer_get(path, **params):
    url = f"{DEEZER_BASE}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "release-tracker/0.2"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise RuntimeError(f"Could not reach Deezer: {e}") from e
    if isinstance(data, dict) and data.get("error"):     # Deezer reports errors inside a 200 reply
        err = data["error"]
        raise RuntimeError(f"Deezer error: {err.get('message') if isinstance(err, dict) else err}")
    return data


_GUEST_BRACKETS = re.compile(r"[(\[]\s*(?:feat|ft|featuring|with|prod)\b[^)\]]*[)\]]", re.IGNORECASE)
_GUEST_WORD = re.compile(r"^(?:feat|ft|featuring|with|prod)\s+")


def title_key(title):
    """A song title reduced for comparison across services: "(feat. X)" chunks, case and punctuation dropped."""
    return normalize_name(_GUEST_BRACKETS.sub("", title or ""))


def guest_key(title):
    """The guests a title names in "(feat. X)" style brackets, for telling two versions apart."""
    return " ".join(sorted(_GUEST_WORD.sub("", normalize_name(m.group(0)))
                           for m in _GUEST_BRACKETS.finditer(title or "")))


def _compact(name):
    return normalize_name(name).replace(" ", "")


def artist_matches(deezer_name, credit, lead):
    """Is Deezer's artist one of the names in Apple's credit? Also ignores spaces and punctuation ("J.P." / "JP")."""
    if is_credited(deezer_name, credit) or is_credited(lead, deezer_name):
        return True
    target = _compact(deezer_name)
    return bool(target) and any(_compact(seg) == target for seg in _SEPARATORS.split(credit or ""))


def deezer_isrc_search(title, artist, seconds, limiter):
    """
    (isrc, why): the ISRC of the Deezer song that is the same as this one, found by searching Deezer's
    track index directly, or None plus a short reason. A plain track search result doesn't carry its
    own ISRC, so a match needs one extra request to fetch it. This is a fallback for when the album
    can't be matched first (find_deezer_album): a plain track search ranks by popularity, so an
    obscure song can be outranked and missed, which searching for the album and reading its real
    tracklist avoids. Raises RuntimeError if Deezer can't be reached.
    """
    lead = next((p.strip() for p in _SEPARATORS.split(artist or "") if p.strip()), "")
    want = title_key(title)
    if not want:
        return None, "empty title"
    want_guests = guest_key(title)
    # the searches leave out "(feat. X)": Deezer often finds nothing when it is included
    clean = (_GUEST_BRACKETS.sub("", title).strip() or title).replace('"', " ")
    # punctuation is stripped from the query text (Deezer's search can choke on it); the title
    # comparison above still uses the real title.
    search_clean = re.sub(r"[^\w\s&]", " ", clean).strip()
    search_lead = re.sub(r"[^\w\s&]", " ", lead).strip()
    queries = [({"q": f'artist:"{lead.replace(chr(34), " ")}" track:"{clean}"', "strict": "on"}, 25),
               ({"q": f"{search_lead} {search_clean}"}, 50),
               ({"q": f'track:"{clean}"'}, 50)]                                        # ignores the artist entirely
    best, best_label = None, ""                            # best = (title rank, guest rank, length difference, deezer id)
    rejected, counts = [], []
    for params, limit in queries:
        limiter.wait()
        found = deezer_get("search", limit=limit, **params).get("data", [])
        counts.append(len(found))
        for c in found:
            name = (c.get("artist") or {}).get("name", "")
            dur = c.get("duration") or 0
            label = f"{c.get('title')!r} by {name!r} ({dur}s)"
            got = title_key(c.get("title"))
            exact = got == want
            if not exact and not (got.startswith(want + " ") or want.startswith(got + " ")):
                rejected.append(f"{label}: title differs")
                continue
            if not artist_matches(name, artist, lead):
                rejected.append(f"{label}: artist differs")
                continue
            gap = abs(dur - seconds) if seconds else 0
            if seconds and gap > (DEEZER_DURATION_TOLERANCE if exact else 1):
                rejected.append(f"{label}: length off by {gap}s")
                continue
            if not exact and not seconds:
                rejected.append(f"{label}: similar title but no length to check")
                continue
            rank = (0 if exact else 1, 0 if guest_key(c.get("title")) == want_guests else 1, gap, c["id"])
            if best is None or rank < best:
                best, best_label = rank, label
        if best:
            break                                          # later, looser searches only run on a miss
    if best is None:
        found_note = f"searches returned {'/'.join(map(str, counts))} results"
        why = "; ".join(dict.fromkeys(rejected[:6])) if rejected else "no results"
        return None, f"{why} [{found_note}]"
    limiter.wait()
    isrc = deezer_get(f"track/{best[3]}").get("isrc") or None
    return isrc, (f"matched {best_label}" if isrc else f"matched {best_label}, which has no ISRC")


def find_deezer_album(album_title, artist, limiter):
    """
    (album_id, deezer_title, deezer_artist, why) of the Deezer album that is this release; the first
    three are None on a miss, and why explains it (query counts and any rejected candidates). A
    plain, unquoted, punctuation-free query works best for Deezer's album search, so the title is
    reduced the same way normalize_title does ("Song Title - Single" -> "song title") and used as the
    search text too, not just for comparing results. Apple sometimes bakes a feature credit into the
    release title itself ("Song Title (feat. X) - Single"); that's stripped before comparing or
    searching, since Deezer's own album title normally wouldn't include it. A quoted/fielded query
    is tried second in case it helps a case the plain one misses.
    """
    lead = next((p.strip() for p in _SEPARATORS.split(artist or "") if p.strip()), "")
    bare_title = _GUEST_BRACKETS.sub("", album_title).strip() or album_title
    want = normalize_title(bare_title)
    if not want:
        return None, None, None, "empty title"
    plain_lead = normalize_name(lead)
    # An initials-style name ("J.P." -> "j p") is also tried with the initials run together ("jp"),
    # since Deezer's search can return different results for each form. Only fires when every token
    # normalize_name produced is a single letter, so an ordinary multi-word name is left alone.
    initials = plain_lead.split()
    compact_lead = "".join(initials) if initials and all(len(w) == 1 for w in initials) else None
    clean = re.sub(r"[^\w\s&]", " ", bare_title).strip()
    search_lead = re.sub(r"[^\w\s&]", " ", lead).strip()
    queries = [f"{compact_lead} {want}"] if compact_lead else []
    queries += [f"{plain_lead} {want}", f'artist:"{lead.replace(chr(34), " ")}" album:"{clean}"',
               f"{search_lead} {clean}"]
    rejected, counts = [], []
    for q in queries:
        limiter.wait()
        found = deezer_get("search/album", q=q, limit=25).get("data", [])
        counts.append(len(found))
        for a in found:
            got = normalize_title(a.get("title") or "")
            name = (a.get("artist") or {}).get("name", "")
            label = f"{a.get('title')!r} by {name!r}"
            if not (got == want or got.startswith(want) or want.startswith(got)):
                rejected.append(f"{label}: title differs")
                continue
            if not artist_matches(name, artist, lead):
                rejected.append(f"{label}: artist differs")
                continue
            return a["id"], a.get("title"), name, ""
    found_note = f"album searches returned {'/'.join(map(str, counts))} results; queries: " + " | ".join(queries)
    why = "; ".join(dict.fromkeys(rejected[:6])) if rejected else "no results"
    return None, None, None, f"{why} [{found_note}]"


def deezer_album_tracklist(album_id, limiter):
    """Every song Deezer lists for this album, in whatever order it returns them (paginated if needed)."""
    limiter.wait()
    data = deezer_get(f"album/{album_id}/tracks", limit=200)
    items = list(data.get("data", []))
    next_url = data.get("next")
    while next_url:
        limiter.wait()
        req = urllib.request.Request(next_url, headers={"User-Agent": "release-tracker/0.2"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.load(resp)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise RuntimeError(f"Could not reach Deezer: {e}") from e
        items += data.get("data", [])
        next_url = data.get("next")
    return items


def match_in_tracklist(title, seconds, tracklist):
    """The song in a Deezer tracklist that matches this Apple title/length, or None. No artist check
    here -- everything in the list already belongs to the album that was already matched by artist."""
    want = title_key(title)
    if not want:
        return None
    want_guests = guest_key(title)
    best = None
    for t in tracklist:
        got = title_key(t.get("title"))
        exact = got == want
        if not exact and not (got.startswith(want + " ") or want.startswith(got + " ")):
            continue
        dur = t.get("duration") or 0
        gap = abs(dur - seconds) if seconds else 0
        if seconds and gap > (DEEZER_DURATION_TOLERANCE if exact else 1):
            continue
        if not exact and not seconds:
            continue
        rank = (0 if exact else 1, 0 if guest_key(t.get("title")) == want_guests else 1, gap)
        if best is None or rank < best[0]:
            best = (rank, t)
    return best[1] if best else None


def load_cached_isrcs(conn, track_ids):
    """{track_id: isrc} for the songs whose ISRC was found (and not too long ago) on an earlier export."""
    cutoff = (datetime.now(timezone.utc) - ISRC_KEEP_FOR).strftime(TS_FORMAT)
    ids, found = list(dict.fromkeys(track_ids)), {}
    for start in range(0, len(ids), 500):       # SQLite caps how many ? a statement may have
        chunk = ids[start:start + 500]
        for tid, isrc in conn.execute(
                f"SELECT track_id, isrc FROM isrc_cache WHERE found_at >= ? AND track_id IN ({','.join('?' * len(chunk))})",
                [cutoff] + chunk):
            found[tid] = isrc
    return found


def store_isrcs(conn, tracks):
    ts = now_iso()
    with conn:
        conn.executemany("INSERT OR REPLACE INTO isrc_cache (track_id, isrc, found_at) VALUES (?, ?, ?)",
                         [(t["id"], t["isrc"], ts) for t in tracks if t.get("id") and t.get("isrc")])


def sweep_old_isrcs(conn):
    cutoff = (datetime.now(timezone.utc) - ISRC_KEEP_FOR).strftime(TS_FORMAT)
    with conn:
        removed = conn.execute("DELETE FROM isrc_cache WHERE found_at < ?", (cutoff,)).rowcount
    if removed:
        log(f"isrc cache: removed {removed} match(es) older than {ISRC_KEEP_FOR.days} days")
    return removed


def add_isrcs(conn, tracks):
    """
    Set t["isrc"] on each exported song (None when unknown). Songs are grouped by their release; for
    each release, Deezer's album is looked up once and matched against directly, since that has proven
    more reliable than searching Deezer's track index song by song. A song not found that way falls
    back to a direct track search (deezer_isrc_search). Every outcome (and the reason for a miss) goes
    to the log. Runs several releases at once, sharing one rate limiter across all of them.
    Songs whose ISRC was found on an earlier export come from isrc_cache and are not looked up again; new
    matches are added to it.
    """
    cached = load_cached_isrcs(conn, [t["id"] for t in tracks])
    for t in tracks:
        t["isrc"] = cached.get(t["id"])
    todo = [t for t in tracks if not t["isrc"]]
    limiter = RateLimiter(DEEZER_INTERVAL)
    lock = threading.Lock()
    failures = 0

    def note_failure():
        nonlocal failures
        with lock:
            failures += 1

    def clear_failure():
        nonlocal failures
        with lock:
            failures = 0

    def failing():
        with lock:
            return failures >= DEEZER_MAX_FAILURES

    groups, order = {}, []
    for t in todo:
        key = (t["album"], t["artist"])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(t)

    def handle_song(t, tracklist, album_label, album_error):
        match = match_in_tracklist(t["title"], t["seconds"], tracklist) if tracklist else None
        if match is not None:
            # the album's own tracklist already carries each track's isrc -- no extra request needed
            isrc = match.get("isrc") or None
            if isrc:
                t["isrc"] = isrc
                if DEEZER_LOG_MATCHES:
                    log(f"isrc found {isrc}: {t['artist']} - {t['title']} (in album match {album_label})")
                return
        if failing():
            return
        try:
            isrc, why = deezer_isrc_search(t["title"], t["artist"], t["seconds"], limiter)
            clear_failure()
        except RuntimeError as e:
            note_failure()
            log(f"isrc lookup failed for {t['artist']} - {t['title']}: {e}")
            return
        t["isrc"] = isrc
        if isrc:
            if DEEZER_LOG_MATCHES:
                log(f"isrc found {isrc}: {t['artist']} - {t['title']} ({why})")
        else:
            note = album_error or (f"song not found in matched album {album_label}" if tracklist
                                   else f"no matching Deezer album for {t['album']!r}")
            log(f"isrc no match: {t['artist']} - {t['title']} ({t['seconds']}s) -> {why} [{note}]")

    def handle_group(key):
        album, artist = key
        songs = groups[key]
        if failing():
            for t in songs:
                handle_song(t, None, "", "Deezer kept failing")
            return
        tracklist, album_label, album_error = None, "", ""
        album_id = d_title = d_artist = None
        why = ""
        try:
            album_id, d_title, d_artist, why = find_deezer_album(album, artist, limiter)
            clear_failure()
        except RuntimeError as e:
            note_failure()
            album_error = f"album lookup failed: {e}"
        if album_id is not None:
            album_label = f"{d_title!r} by {d_artist!r}"
            try:
                tracklist = deezer_album_tracklist(album_id, limiter)
                clear_failure()
            except RuntimeError as e:
                note_failure()
                album_error = f"found album {album_label} but could not read its tracklist: {e}"
        elif not album_error:
            album_error = f"no matching Deezer album for {album!r}: {why}"
        for t in songs:
            handle_song(t, tracklist, album_label, album_error)

    with ThreadPoolExecutor(max_workers=DEEZER_WORKERS) as pool:
        list(pool.map(handle_group, order))
    store_isrcs(conn, todo)
    found = sum(1 for t in tracks if t["isrc"])
    log(f"isrc lookup: {found}/{len(tracks)} songs matched ({len(cached)} already known)")
    if failing():
        log("isrc lookup: Deezer kept failing, so some songs were skipped")
    return found



def build_export(conn, items, limit=0):
    """
    The songs of the releases shown in a feed tab, ready to become a playlist.
    items: [{"id", "kind", "for_artist"}] in the order shown. Every song of each release is taken,
    features included (Apple's "appears on" is almost always singles; kind/for_artist aren't used here).
    A song that is already in the list (same title and credit, e.g. a deluxe edition repeating
    the standard album) is skipped. limit (0 = none) caps the number of songs: whole releases are
    added until the next one would go over it; the first release is always included.
    """
    info = {}
    ids = [it["id"] for it in items]
    if ids:
        marks = ",".join("?" * len(ids))
        for table in ("listen_later", "releases"):      # queued releases outlive their artist
            for rid, title, date in conn.execute(
                    f"SELECT id, title, release_date FROM {table} WHERE id IN ({marks})", ids):
                info.setdefault(rid, (title, date))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tracks, done_ids, seen = [], [], set()
    no_tracks = left_out = 0
    stop = False
    for start in range(0, len(items), EXPORT_BATCH):
        chunk = items[start:start + EXPORT_BATCH]
        fetched = tracks_for_releases(
            conn, [(it["id"], bool(info[it["id"]][1]) and info[it["id"]][1][:10] <= today)
                   for it in chunk if it["id"] in info])
        for pos, it in enumerate(chunk):
            if it["id"] not in info:
                continue
            album = info[it["id"]][0]
            songs = fetched.get(it["id"], [])
            if not songs:
                no_tracks += 1
                continue
            fresh = []
            for t in songs:
                key = (t["title"].casefold(), normalize_name(t["artist"]))
                if key not in seen:
                    seen.add(key)
                    fresh.append(t)
            if limit and tracks and len(tracks) + len(fresh) > limit:
                left_out = len(items) - (start + pos)
                stop = True
                break
            done_ids.append(it["id"])
            tracks += [{"title": t["title"], "artist": t["artist"], "album": album,
                        "seconds": round(t["duration_ms"] / 1000) if t["duration_ms"] else None,
                        "id": t["id"]}
                       for t in fresh]
        if stop:
            break
    isrc_found = add_isrcs(conn, tracks)
    return {"tracks": tracks, "release_ids": done_ids, "releases": len(done_ids),
            "left_out": left_out, "no_tracks": no_tracks, "isrc_found": isrc_found}


def get_artists(conn):
    rows = conn.execute(
        """SELECT a.id, a.name, a.added_at, a.last_checked, a.favorite,
                  COALESCE(NULLIF(a.photo, ''),
                           (SELECT artwork FROM releases
                            WHERE artist_id = a.id AND artwork IS NOT NULL
                            ORDER BY release_date DESC LIMIT 1))
           FROM artists a ORDER BY a.name COLLATE NOCASE"""
    ).fetchall()
    groups = collapsed_releases(conn)
    return [
        {"id": i, "name": n, "added_at": added, "last_checked": checked, "favorite": bool(fav),
         "releases": len(groups.get((i, "own"), [])),
         "features": len(groups.get((i, "feature"), [])),
         "artwork": big_picture(art)}
        for i, n, added, checked, fav, art in rows
    ]


def get_catalog(conn, artist_id):
    """
    Everything we have stored for one artist, newest first, in two lists: their own
    releases and the features (releases they "appear on"). Editions that add nothing
    collapse (see distinct_releases). None if the artist isn't tracked.
    """
    row = conn.execute("SELECT name, photo, catalog_capped FROM artists WHERE id = ?", (artist_id,)).fetchone()
    if row is None:
        return None
    name, photo, capped = row
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    muted = load_muted(conn)
    tracked_norms = tracked_name_norms(conn)
    queued = {r[0] for r in conn.execute("SELECT id FROM listen_later WHERE hidden = 0")}
    groups = collapsed_releases(conn, artist_id)

    def shape(r):
        return {"id": r["id"], "title": r["title"], "release_date": r["release_date"],
                "app_url": apple_music_app_url(r["url"]), "artwork": big_picture(r["artwork"]), "track_count": r["track_count"], "artist": r["credit"] or name,
                "kind": r["kind"], "mute_options": mute_options(r["credit"], tracked_norms),
                "upcoming": bool(r["release_date"]) and r["release_date"][:10] > today,
                "later": r["id"] in queued}

    def newest_first(kind):
        return sorted((shape(r) for r in groups.get((artist_id, kind), [])),
                      key=lambda r: r["release_date"] or "", reverse=True)

    removed = [{"id": rid, "title": title, "release_date": date}
               for rid, title, date, credit in conn.execute(
                   "SELECT id, title, release_date, credit FROM releases WHERE artist_id = ? AND removed = 1",
                   (artist_id,))
               if not is_muted(credit, muted)]
    releases, features = newest_first("own"), newest_first("feature")
    picture = big_picture(photo or next((r["artwork"] for r in releases + features if r["artwork"]), None))
    return {"artist": {"id": artist_id, "name": name, "artwork": picture},
            "capped": bool(capped),      # Apple's 200-entry limit was reached: older releases may be missing
            "count": len(releases), "releases": releases,
            "feature_count": len(features), "features": features,
            "removed": sorted(removed, key=lambda r: r["release_date"] or "", reverse=True)}


def preview(artist_id, artist_name=""):
    """Summary for telling artists apart. Picture is the artist's photo when the
    artist page has one, otherwise the cover of their most recent release."""
    # Catalog lookup (iTunes API) and photo (artist page) are independent: run together.
    with ThreadPoolExecutor(max_workers=2) as pool:
        rels_job = pool.submit(cached_releases, artist_id)
        photo_job = pool.submit(cached_photo, artist_id)
        rels = rels_job.result()
        photo = photo_job.result()
    artwork = photo or None
    newest_first = sorted(rels, key=lambda r: r["release_date"] or "", reverse=True)
    if artwork is None:
        artwork = next((r["artwork"] for r in newest_first if r["artwork"]), None)
    own, features = split_by_kind(artist_id, artist_name, rels)
    latest = sorted(own, key=lambda r: r["release_date"] or "", reverse=True)[:3]
    return {"count": len(own), "features": len(features),
            "recent": [r["title"] for r in latest],
            "recent_items": [{"title": r["title"], "artwork": r["artwork"]} for r in latest],
            "artwork": big_picture(artwork)}


PREVIEW_BATCH_LIMIT = 10   # newest entries per artist in the "other matches" lookup
PREVIEW_RECENT_SHOWN = 3   # recent releases shown per candidate
PREVIEW_WIDE_LIMIT = 40    # wider window for a candidate whose newest entries are mostly features


def preview_many(artists):
    """
    Light previews for several candidate artists at once: ONE batched Apple lookup (their newest
    PREVIEW_BATCH_LIMIT entries each) plus their photos, fetched in parallel (the artist pages are on
    a different host from the API, so they don't touch the Apple rate limiter).
    Same shape as preview(), except `count` is only exact when "more" is false: if Apple returned a
    full window, older releases may exist.
    artists: [(artist_id, name)]. Returns {str(artist_id): preview dict}.
    """
    names = dict(artists)
    ids = list(names)
    with ThreadPoolExecutor(max_workers=4) as pool:
        photo_jobs = {i: pool.submit(cached_photo, i) for i in ids}
        batch = fetch_recent_batch(ids, limit=PREVIEW_BATCH_LIMIT)
        photos = {i: job.result() for i, job in photo_jobs.items()}
    # The newest entries include "appears on" releases, so an artist with lots of features can have a
    # full window with fewer than three releases of their own. Those few get one wider lookup (a single
    # extra request, only when needed); if it fails, the narrower result is used.
    thin = [i for i in ids
            if len(batch.get(i) or []) >= PREVIEW_BATCH_LIMIT
            and len(split_by_kind(i, names[i], batch[i])[0]) < PREVIEW_RECENT_SHOWN]
    if thin:
        try:
            batch.update({i: r for i, r in fetch_recent_batch(thin, limit=PREVIEW_WIDE_LIMIT).items() if r})
        except RuntimeError:
            pass
    out = {}
    for i in ids:
        rels = batch.get(i) or []
        own, _features = split_by_kind(i, names[i], rels)
        newest = sorted(own, key=lambda r: r["release_date"] or "", reverse=True)
        latest = newest[:PREVIEW_RECENT_SHOWN]
        artwork = photos.get(i) or next((r["artwork"] for r in newest if r["artwork"]), None)
        more = len(rels) >= PREVIEW_BATCH_LIMIT
        # A full window means older releases may exist, so show the window size ("10+"), not how many
        # of those 10 happened to be the artist's own (the rest may be features or duplicate editions).
        out[str(i)] = {"count": PREVIEW_BATCH_LIMIT if more else len(own), "more": more,
                       "recent": [r["title"] for r in latest],
                       "recent_items": [{"title": r["title"], "artwork": r["artwork"]} for r in latest],
                       "artwork": big_picture(artwork)}
    return out


# ---------------------------------------------------------------- refresh

def refresh_photo(conn, artist_id, current_photo):
    """
    Re-read the artist's photo from their Apple Music page. Apple gives a changed
    photo a new URL, so a different URL means a new picture. If the page has no
    photo now, keep the one we have (likely a glitch); network errors are retried
    on a later refresh.
    """
    try:
        found = fetch_artist_photo(artist_id)
    except RuntimeError:
        return
    ts = now_iso()
    with conn:
        if found:
            conn.execute("UPDATE artists SET photo = ?, photo_checked_at = ? WHERE id = ?",
                         (found, ts, artist_id))
        elif current_photo is None:
            conn.execute("UPDATE artists SET photo = '', photo_checked_at = ? WHERE id = ?",
                         (ts, artist_id))
        else:
            conn.execute("UPDATE artists SET photo_checked_at = ? WHERE id = ?", (ts, artist_id))


class RefreshState:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.done = 0
        self.total = 0
        self.current = ""
        self.errors = []
        self.finished_at = None

    def snapshot(self):
        with self.lock:
            return {
                "running": self.running, "done": self.done, "total": self.total,
                "current": self.current, "errors": list(self.errors),
                "finished_at": self.finished_at,
            }


state = RefreshState()


# A row is (id, name, last_checked, photo, photo_checked_at, catalog_rechecked_at).

def _job_recheck_catalog(conn, row):
    """Redo a full catalog lookup for a capped artist (see CATALOG_RECHECK_AFTER), so a release that
    fell outside the lookup window has another chance to surface."""
    artist_id, name, last_checked, rechecked = row[0], row[1], row[2], row[5]
    log(f"periodic recheck: redoing {name}'s full catalog lookup "
        f"(capped artist, last rechecked {rechecked or 'never'})")
    releases = fetch_full_catalog(artist_id)   # a fresh default-order + recent-order merge
    # Anything this turns up dated on/before our last check was already out there and simply
    # missed -- file it as baseline rather than flagging a backlog of "new" releases.
    apply_new_releases(conn, artist_id, releases, baseline_through=(last_checked or now_iso())[:10])
    with conn:
        conn.execute("UPDATE artists SET catalog_capped = ?, catalog_rechecked_at = ? WHERE id = ?",
                     (2 if len(releases) >= FULL_LOOKUP_LIMIT else 0, now_iso(), artist_id))


def _refresh_artists(conn, force):
    now = datetime.now(timezone.utc)
    cutoff = (now - STALE_AFTER).strftime(TS_FORMAT)
    photo_cutoff = (now - PHOTO_RECHECK_AFTER).strftime(TS_FORMAT)
    recheck_cutoff = (now - CATALOG_RECHECK_AFTER).strftime(TS_FORMAT)
    rows = conn.execute(
        "SELECT id, name, last_checked, photo, photo_checked_at, catalog_rechecked_at, catalog_capped "
        "FROM artists").fetchall()

    # Capped artists (Apple's 200-entry limit) get their full catalog looked up again now and then.
    rechecks = [r for r in rows if r[6] == 2 and (r[5] is None or r[5] < recheck_cutoff)]
    rechecks.sort(key=lambda r: r[5] or "")   # longest-since-rechecked first
    rechecks = rechecks[:MAX_CATALOG_RECHECKS]   # spreads the cost out
    claimed = {r[0] for r in rechecks}
    stale = [r for r in rows if r[0] not in claimed and (force or r[2] is None or r[2] < cutoff)]
    stale.sort(key=lambda r: r[2] or "")   # longest-unchecked first, so an interrupted
                                           # refresh picks up where it left off
    with state.lock:
        state.total = len(rechecks) + len(stale)
    log(f"refresh started: {len(rows)} artists, {len(rechecks)} capped catalogs to recheck, "
        f"{len(stale)} stale{' (forced)' if force else ''}")
    photo_rechecks = 0

    def check_photo(artist_id, photo, photo_checked):
        nonlocal photo_rechecks
        if photo is None or photo_checked is None:
            refresh_photo(conn, artist_id, photo)          # no photo yet: always try
        elif photo_checked < photo_cutoff and photo_rechecks < MAX_PHOTO_RECHECKS:
            photo_rechecks += 1                            # monthly re-check, a few per refresh
            refresh_photo(conn, artist_id, photo)

    def fail(name, e):
        with state.lock:
            state.errors.append(f"{name}: {e}")

    def finished_one():
        with state.lock:
            state.done += 1

    # Request pacing is not handled here: api_get waits on the shared limiter.
    for row in rechecks:
        artist_id, name, photo, photo_checked = row[0], row[1], row[3], row[4]
        with state.lock:
            state.current = name
        try:
            _job_recheck_catalog(conn, row)
            check_photo(artist_id, photo, photo_checked)
        except RateLimited:
            raise
        except Exception as e:
            fail(name, e)
        finished_one()

    # Everyone else: their newest entries, looked up in batches.
    for start in range(0, len(stale), BATCH_SIZE):
        batch = stale[start:start + BATCH_SIZE]
        label = batch[0][1] + (f" (+{len(batch) - 1} more)" if len(batch) > 1 else "")
        with state.lock:
            state.current = label

        results = fetch_recent_batch_with_retry([r[0] for r in batch])
        if results is None:
            log("batch lookup failed every try; falling back to one artist at a time")
            results = {}
        else:
            log(f"batch of {len(batch)} ok: Apple returned {len(results)} artists")

        for artist_id, name, _checked, photo, photo_checked, *_ in batch:
            try:
                releases = results.get(artist_id)
                reason = None
                if releases is None:
                    reason = "no batch result for this artist"
                elif looks_truncated(conn, artist_id, releases):
                    reason = "its 10 newest entries were all unfamiliar"
                if reason:
                    with state.lock:
                        state.current = name
                    log(f"extra lookup for {name}: {reason}")
                    try:
                        releases = fetch_recent_until_known(conn, artist_id, name)
                    except RateLimited:
                        raise
                    except RuntimeError as e:
                        log(f"widening lookup for {name} failed ({e}); doing a full lookup instead")
                        releases = fetch_releases(artist_id)   # the artist's full catalog
                apply_new_releases(conn, artist_id, releases)
                check_photo(artist_id, photo, photo_checked)
            except RateLimited:
                raise
            except Exception as e:  # keep going; report at the end
                fail(name, e)
            finished_one()


def _sweep_after_refresh():
    try:
        with db() as conn:
            try:
                sweep_old_listen_later(conn)    # first, so the image sweep below can drop their artwork too
            except sqlite3.Error as e:
                log(f"listen later sweep failed: {e}")
            try:
                sweep_orphaned_images(conn)
            except OSError as e:   # never let a cache-cleanup problem hide how the refresh itself went
                log(f"image cache sweep failed: {e}")
            try:
                sweep_old_tracks(conn)
                sweep_old_isrcs(conn)
            except sqlite3.Error as e:
                log(f"old export songs sweep failed: {e}")
    except Exception as e:
        log(f"post-refresh cleanup failed: {e}")


def _refresh_worker(force):
    started = time.time()
    try:
        with db() as conn:
            _refresh_artists(conn, force)
    except RateLimited:
        with state.lock:
            state.errors.append("Apple's rate limit was reached; the remaining artists "
                                "will be checked next time you open the tracker")
    except Exception as e:  # anything unexpected: report it rather than hang the UI
        with state.lock:
            state.errors.append(f"refresh failed: {type(e).__name__}: {e}")
    finally:
        _sweep_after_refresh()
        log(f"refresh finished in {time.time() - started:.0f}s")
        with state.lock:
            state.running = False
            state.current = ""
            state.finished_at = now_iso()


def start_refresh(force=False):
    with state.lock:
        if state.running:
            return False
        state.running = True
        state.done = 0
        state.total = 0
        state.current = ""
        state.errors = []
    threading.Thread(target=_refresh_worker, args=(force,), daemon=True).start()
    return True


# ---------------------------------------------------------------- server

class TrackerServer(ThreadingHTTPServer):
    # On Windows, SO_REUSEADDR (which http.server turns on by default) lets a second
    # server bind a port that is already in use, so two trackers could run side by
    # side and split your browser's requests. Elsewhere it only speeds up restarts.
    allow_reuse_address = sys.platform != "win32"

    def handle_error(self, request, client_address):
        # A browser closing its tab mid-reply is normal, not an error worth a traceback.
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)


_server: Optional[TrackerServer] = None   # set by main(); used by /api/quit


class PageTracker:
    """
    Knows which browser pages are open, so the server can quit when the last one
    closes. Each page says hello when it loads and bye when it is closed. A reload
    sends bye and then hello again, so quitting waits a few seconds to be sure no
    new page arrives. If a browser crashes without saying bye, the server just
    keeps running.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.pages = set()

    def hello(self, page_id):
        with self.lock:
            self.pages.add(page_id)
            print(f"{now_iso()} page opened ({len(self.pages)} open)", flush=True)

    def bye(self, page_id):
        with self.lock:
            self.pages.discard(page_id)
            print(f"{now_iso()} page closed ({len(self.pages)} open)", flush=True)
            if self.pages:
                return
        timer = threading.Timer(CLOSE_GRACE_SECONDS, self._quit_if_still_empty)
        timer.daemon = True
        timer.start()

    def _quit_if_still_empty(self):
        with self.lock:
            if self.pages:
                return
        server = _server
        if server is not None:
            print(f"{now_iso()} last page closed, quitting", flush=True)
            server.shutdown()


pages = PageTracker()


# ---------------------------------------------------------------- API routes

class BadRequest(Exception):
    """The request itself is wrong (missing or malformed input): reported as HTTP 400. Anything else that
    goes wrong inside a handler is a bug in this program and is reported as a 500, with the traceback in the log."""


def as_int(value, what):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise BadRequest(f"{what} must be a number") from None


def query_value(query, key, default=""):
    return (query.get(key) or [default])[0]


# Each handler is called as handler(req, match, query) and returns (HTTP status, JSON-able payload).
# req is the request handler (for req._body()), match the regex match of the path, query the parsed query string.

def api_status(req, m, query):
    return 200, state.snapshot()


def api_version(req, m, query):
    return 200, {"stamp": CODE_STAMP}


def api_search(req, m, query):
    term = query_value(query, "term").strip()
    if not term:
        raise BadRequest("term is required")
    link_id = artist_id_from_url(term)
    cands = lookup_artist(link_id) if link_id else search_artists(term)
    with db() as conn:
        tracked = {r[0] for r in conn.execute("SELECT id FROM artists")}
    return 200, {"candidates": [
        {"id": c["artistId"], "name": c["artistName"],
         "genre": c.get("primaryGenreName"), "tracked": c["artistId"] in tracked}
        for c in cands
    ]}


def api_catalog(req, m, query):
    with db() as conn:
        catalog = get_catalog(conn, int(m.group(1)))
    if catalog is None:
        return 404, {"error": "artist not found"}
    return 200, catalog


def api_preview(req, m, query):
    return 200, preview(as_int(query_value(query, "id"), "id"), query_value(query, "name"))


def api_previews(req, m, query):
    raw = req._body().get("artists")
    if not isinstance(raw, list) or not 0 < len(raw) <= 10:
        raise BadRequest("artists must be a list of 1 to 10 artists")
    artists = [(as_int(a.get("id"), "artist id"), str(a.get("name") or "")[:200])
               for a in raw if isinstance(a, dict)]
    return 200, preview_many(artists)


def api_muted_list(req, m, query):
    with db() as conn:
        return 200, {"muted": load_muted(conn)}


def api_feed(req, m, query):
    days = as_int(query_value(query, "days", FEED_DEFAULT_DAYS), "days")
    with db() as conn:
        releases, total = get_feed(conn, days=max(days, 0))
    return 200, {"releases": releases, "total": total}


def api_artists_list(req, m, query):
    with db() as conn:
        return 200, {"artists": get_artists(conn)}


def _page_id(req):
    page_id = str(req._body().get("id", ""))[:64]
    if not page_id:
        raise BadRequest("id is required")
    return page_id


def api_hello(req, m, query):
    pages.hello(_page_id(req))
    return 200, {"ok": True}


def api_bye(req, m, query):
    pages.bye(_page_id(req))
    return 200, {"ok": True}


def api_quit(req, m, query):
    server = _server
    if server is not None:
        threading.Thread(target=server.shutdown, daemon=True).start()
    return 200, {"ok": True}


def api_refresh(req, m, query):
    return 200, {"started": start_refresh(bool(req._body().get("force")))}


def api_export(req, m, query):
    body = req._body()
    raw = body.get("items")
    if not isinstance(raw, list) or len(raw) > EXPORT_MAX_RELEASES:
        raise BadRequest(f"items must be a list of at most {EXPORT_MAX_RELEASES} releases")
    items = []
    for it in raw:
        if not isinstance(it, dict):
            raise BadRequest("each item must be an object")
        items.append({"id": as_int(it.get("id"), "item id"),
                      "kind": "feature" if it.get("kind") == "feature" else "own",
                      "for_artist": str(it.get("for_artist") or "")[:200]})
    limit = max(0, min(as_int(body.get("limit") or 0, "limit"), 5000))
    with db() as conn:
        return 200, build_export(conn, items, limit)


def api_mute(req, m, query):
    name = str(req._body().get("name", "")).strip()[:200]
    key = normalize_name(name)
    if not key:
        raise BadRequest("name is required")
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO muted_names (norm_name, name, muted_at) VALUES (?, ?, ?)",
                     (key, name, now_iso()))
    return 200, {"ok": True}


def api_unmute(req, m, query):
    with db() as conn:
        conn.execute("DELETE FROM muted_names WHERE norm_name = ?", (normalize_name(query_value(query, "name")),))
    return 200, {"ok": True}


def api_add_artist(req, m, query):
    body = req._body()
    artist_id = as_int(body.get("id"), "id")
    name = str(body.get("name") or "")[:200]
    if not name:
        raise BadRequest("name is required")
    with db() as conn:
        if conn.execute("SELECT 1 FROM artists WHERE id = ?", (artist_id,)).fetchone():
            return 409, {"error": "already tracked"}
    releases = cached_releases(artist_id)       # network calls: no database connection is held meanwhile
    photo = cached_photo(artist_id)
    try:
        with db() as conn:
            save_artist_with_baseline(conn, artist_id, name, releases, photo=photo)
            # tracking an artist un-mutes their name
            conn.execute("DELETE FROM muted_names WHERE norm_name = ?", (normalize_name(name),))
    except sqlite3.IntegrityError:              # added by another request while the lookups ran
        return 409, {"error": "already tracked"}
    own, features = split_by_kind(artist_id, name, releases)
    return 200, {"added": name, "baseline": len(own), "features": len(features)}


def api_untrack(req, m, query):
    with db() as conn:
        conn.execute("DELETE FROM artists WHERE id = ?", (int(m.group(1)),))
    return 200, {"ok": True}


def api_favorite(req, m, query):
    favorite = 1 if req._body().get("favorite") else 0
    with db() as conn:
        conn.execute("UPDATE artists SET favorite = ? WHERE id = ?", (favorite, int(m.group(1))))
    return 200, {"ok": True, "favorite": bool(favorite)}


def api_remove_release(req, m, query):
    removed = 0 if req._body().get("undo") else 1      # {"undo": true} restores it
    with db() as conn:
        conn.execute("UPDATE releases SET removed = ? WHERE artist_id = ? AND id = ?",
                     (removed, int(m.group(1)), int(m.group(2))))
    return 200, {"ok": True}


def api_listen_later(req, m, query):
    body = req._body()
    artist_id = as_int(body.get("artist_id"), "artist_id")
    with db() as conn:
        if not set_listen_later(conn, int(m.group(1)), artist_id, bool(body.get("later"))):
            return 404, {"error": "release not found"}
    return 200, {"ok": True}


def api_ignore(req, m, query):
    ignored = 0 if req._body().get("undo") else 1      # {"undo": true} un-hides it
    with db() as conn:
        conn.execute("UPDATE releases SET ignored = ? WHERE id = ?", (ignored, int(m.group(1))))
        conn.execute("UPDATE listen_later SET hidden = ?, hidden_at = ? WHERE id = ?",
                     (ignored, now_iso() if ignored else None, int(m.group(1))))
    return 200, {"ok": True}


def api_ignore_many(req, m, query):
    body = req._body()
    raw = body.get("ids")
    if not isinstance(raw, list) or len(raw) > EXPORT_MAX_RELEASES:
        raise BadRequest(f"ids must be a list of at most {EXPORT_MAX_RELEASES} releases")
    ids = [(as_int(i, "release id"),) for i in raw]
    ignored = 0 if body.get("undo") else 1
    with db() as conn:          # one transaction: all of them, or none
        conn.executemany(f"UPDATE releases SET ignored = {ignored} WHERE id = ?", ids)
        stamp = now_iso() if ignored else None
        conn.executemany("UPDATE listen_later SET hidden = ?, hidden_at = ? WHERE id = ?",
                         [(ignored, stamp, i) for (i,) in ids])
    return 200, {"ok": True, "count": len(ids)}


ROUTES = [(method, re.compile(pattern), handler) for method, pattern, handler in (
    ("GET",    r"/api/status",                              api_status),
    ("GET",    r"/api/version",                             api_version),
    ("GET",    r"/api/search",                              api_search),
    ("GET",    r"/api/artists/(\d+)/catalog",               api_catalog),
    ("GET",    r"/api/preview",                             api_preview),
    ("GET",    r"/api/muted",                               api_muted_list),
    ("GET",    r"/api/feed",                                api_feed),
    ("GET",    r"/api/artists",                             api_artists_list),
    ("POST",   r"/api/hello",                               api_hello),
    ("POST",   r"/api/bye",                                 api_bye),
    ("POST",   r"/api/quit",                                api_quit),
    ("POST",   r"/api/refresh",                             api_refresh),
    ("POST",   r"/api/export",                              api_export),
    ("POST",   r"/api/previews",                            api_previews),
    ("POST",   r"/api/muted",                               api_mute),
    ("POST",   r"/api/artists",                             api_add_artist),
    ("POST",   r"/api/artists/(\d+)/favorite",              api_favorite),
    ("POST",   r"/api/artists/(\d+)/releases/(\d+)/remove", api_remove_release),
    ("POST",   r"/api/listen-later/(\d+)",                  api_listen_later),
    ("POST",   r"/api/releases/(\d+)/ignore",               api_ignore),
    ("POST",   r"/api/releases/ignore",                     api_ignore_many),
    ("DELETE", r"/api/muted",                               api_unmute),
    ("DELETE", r"/api/artists/(\d+)",                       api_untrack),
)]


class Handler(BaseHTTPRequestHandler):
    # Keep-alive: one connection carries many requests (the page loads dozens of thumbnails). That only works
    # if each request's body is read in full before the next request is parsed, which _handle takes care of.
    protocol_version = "HTTP/1.1"
    timeout = 30            # an idle connection is closed after this many seconds, freeing its thread
    MAX_BODY = 1_000_000

    def log_message(self, *args):
        pass

    def _send(self, status, body, ctype="application/json"):
        if not isinstance(body, bytes):
            body = json.dumps(proxify(body)).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _serve_image(self, url):
        """Serve an Apple artwork URL from the on-disk cache (fetching it once if needed).
        Only mzstatic.com URLs are allowed through, so this can't be used as an open proxy."""
        if not _is_apple_image_url(url):
            return self._send(400, {"error": "not an allowed image url"})
        try:
            data, ctype = fetch_cached_image(url)
        except RuntimeError as e:
            return self._send(502, {"error": str(e)})
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):
        return self.headers.get("Host", "") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def _body(self):
        """The JSON object sent with the request ({} if none)."""
        try:
            data = json.loads(self._raw_body) if self._raw_body else {}
        except ValueError:
            raise BadRequest("the body is not valid JSON") from None
        if not isinstance(data, dict):
            raise BadRequest("the body must be a JSON object")
        return data

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")

    def _handle(self, method):
        if not self._host_ok():
            self.close_connection = True        # the body, if there is one, is not read
            return self._send(403, {"error": "forbidden"})
        self._raw_body = b""
        if method != "GET":                     # always read the body, whatever the route does with it
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > self.MAX_BODY:
                self.close_connection = True
                return self._send(413 if length > 0 else 400, {"error": "bad or oversized request body"})
            self._raw_body = self.rfile.read(length) if length else b""
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)

        if method == "GET" and path == "/":
            try:
                return self._send(200, INDEX_PATH.read_bytes(), "text/html")
            except FileNotFoundError:
                return self._send(500, {"error": "index.html not found next to releases.py"})

        if method == "GET" and path == "/img":
            return self._serve_image(query_value(query, "u"))

        # State-changing requests must be JSON: blocks cross-site form posts.
        if method != "GET" and "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._send(415, {"error": "JSON required"})

        try:
            status, payload = self._route(method, path, query)
        except BadRequest as e:         # the caller's mistake
            status, payload = 400, {"error": f"bad request: {e}"}
        except RuntimeError as e:       # upstream API problems
            status, payload = 502, {"error": str(e)}
        except Exception as e:          # a bug here: say so, and keep the traceback
            log(f"unexpected error handling {method} {self.path}:\n{traceback.format_exc()}")
            status, payload = 500, {"error": f"{type(e).__name__}: {e}"}
        self._send(status, payload)

    def _route(self, method, path, query):
        for route_method, pattern, handler in ROUTES:
            if route_method == method:
                m = pattern.fullmatch(path)
                if m:
                    return handler(self, m, query)
        return 404, {"error": "not found"}


def backup_database():
    """
    Copy releases.db into backups/ as releases-YYYY-MM-DD.db: the first launch of each day, before anything
    else touches the file, so it holds the state as of the end of the last session. The newest BACKUPS_KEPT
    copies are kept. The Listen later queue, hidden releases, favorites, mutes and removals exist only in
    that file. A failed backup is logged and never stops the tracker from starting.
    """
    try:
        if not DB_PATH.exists():
            return
        BACKUP_DIR.mkdir(exist_ok=True)
        target = BACKUP_DIR / f"releases-{datetime.now().strftime('%Y-%m-%d')}.db"
        if target.exists():
            return
        partial = target.with_suffix(".partial")
        src = sqlite3.connect(str(DB_PATH), timeout=15)
        try:
            dst = sqlite3.connect(str(partial))
            try:
                src.backup(dst)         # a consistent copy even if something else has the file open
            finally:
                dst.close()
        finally:
            src.close()
        partial.replace(target)
        for old in sorted(BACKUP_DIR.glob("releases-*.db"))[:-BACKUPS_KEPT]:
            old.unlink()
        log(f"database backed up to backups/{target.name}")
    except Exception as e:
        log(f"database backup failed: {type(e).__name__}: {e}")


def _running_instance_is_current():
    """True if the server already on our port is running this exact code."""
    try:
        with urllib.request.urlopen(f"http://{HOST}:{PORT}/api/version", timeout=3) as r:
            return json.load(r).get("stamp") == CODE_STAMP
    except Exception:
        return False  # unreachable, or an older version that has no /api/version


def _stop_running_instance():
    """Ask a stale tracker on our port to quit, then wait briefly for the port to free."""
    req = urllib.request.Request(
        f"http://{HOST}:{PORT}/api/quit", data=b"{}", method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:
        pass  # nothing answered: it may already be gone
    for _ in range(20):  # up to ~2 seconds
        time.sleep(0.1)
        try:
            socket.create_connection((HOST, PORT), timeout=0.2).close()
        except OSError:
            return


def rotate_log_if_large(path, max_bytes=LOG_MAX_BYTES):
    """
    If the log has grown past max_bytes, move it to path.old (replacing whatever was there) and start
    fresh, so tracker.log doesn't grow forever. Keeping one rotated copy (rather than truncating outright)
    means whatever led up to the rollover is still around in .old, not just wiped. A failure here (e.g.
    the file is open elsewhere) is swallowed -- worst case the log just keeps growing until next launch.
    """
    try:
        if path.exists() and path.stat().st_size > max_bytes:
            old = path.with_suffix(path.suffix + ".old")
            old.unlink(missing_ok=True)
            path.rename(old)
    except OSError:
        pass


def main():
    global _server
    if sys.stdout is None or not sys.stdout.isatty():  # no console (pythonw, Python Launcher): log to a file
        log_path = HERE / "tracker.log"
        rotate_log_if_large(log_path)
        sys.stdout = sys.stderr = open(log_path, "a", buffering=1, encoding="utf-8")
    url = f"http://{HOST}:{PORT}/"
    try:
        # noinspection PyTypeChecker  (PyCharm false positive on http.server handler classes)
        _server = TrackerServer((HOST, PORT), Handler)
    except OSError:
        if _running_instance_is_current():
            print(f"Tracker already running on port {PORT}. Opening it.")
            webbrowser.open(url)
            return
        # Something is on our port that isn't this version of the tracker. There can be
        # more than one (older versions could start side by side), so keep going until
        # the port is free and we manage to bind it.
        print("An older tracker is still running; replacing it with this version.")
        _server = None
        for _ in range(6):
            _stop_running_instance()
            try:
                # noinspection PyTypeChecker
                _server = TrackerServer((HOST, PORT), Handler)
                break
            except OSError:
                time.sleep(0.3)
        if _server is None:
            print(f"Could not free port {PORT}. Quit the other program and try again.")
            webbrowser.open(url)
            return
    print(f"{now_iso()} Release tracker started (pid {os.getpid()}) at {url}", flush=True)
    backup_database()       # before the page opens and starts using the database
    webbrowser.open(url)
    try:
        _server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        _server.server_close()


if __name__ == "__main__":
    main()
