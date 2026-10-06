<div align="center">
  <a href="https://github.com/benfiller/ReleaseTracker">
    <img height="84" alt="icon" src="https://github.com/user-attachments/assets/6f812287-7dfd-41e8-bc1c-96ef8c81f91f" />
  </a>
  <h1 align="center">Release Tracker</h1>
  <h4>Track artists you choose and get notified of their new releases, with links that open straight in the Apple Music app.</h4>
  <h6>Implemented as a small local Python server plus one HTML browser page; nothing to install beyond Python itself.</h6>
  <h2></h2>
</div>

<img width="1866" height="835" alt="feed-view" src="https://github.com/user-attachments/assets/7087776d-5ed0-4946-9971-cc186ddda986" />

## Features

- Track any number of artists and show their **new releases** as they come out
- **Features** tab separate from tracked artists' own releases (Apple Music's "Appears on")
- **Upcoming** releases show up as soon as Apple lists them, then move to **New releases** once they drop
- **Duplicate detection:** remasters, explicit/clean copies, and reissues that don't add tracks are hidden automatically
- Any release can be added to a **Listen later** queue, collected under a separate tab in the feed
- **Favorite** an artist to have their releases show up first in the feed
- **Mute** an artist to hide any releases they are credited on across the entire feed
- **Playlist export:** build a CSV song list from a feed tab, ready to import into [Soundiiz](https://soundiiz.com) and sync to Apple Music
- Artist photos and artwork are cached to disk so thumbnails load instantly even after the browser cache clears
- Daily local backups of the database, dark/light theme, responsive layout

## Requirements

- Python 3.8 or newer: [python.org/downloads](https://www.python.org/downloads/)

## Running it

### Windows

1. **Code** → Download ZIP or clone this repo.
2. Extract the contents.
3. Double-click `run.pyw`.

### Mac

1. **Code** → Download ZIP or clone this repo.
2. Extract the contents.
3. Double-click `run.pyw`. If it opens in IDLE or another editor instead, right-click it, choose **Open With**, pick **Python Launcher**, and tick **Always Open With**.

If you see a `CERTIFICATE_VERIFY_FAILED` error, open your Python folder in Applications and double-click **Install Certificates.command** once.

### What happens

A local server starts at `http://127.0.0.1:8765/` and a browser tab opens automatically. The server runs as long as the tab is open and shuts itself down a few seconds after you close it. `releases.db`, `backups/`, `image_cache/`, and `tracker.log` are created on first run and live next to the script in the project folder.

## Using the app

- Track an artist by entering their name or Apple Music link under the **Artists** tab.
- **Feed** shows all new releases from tracked artists, refreshed every time you open the app (if not opened in the last 6 hours). Can be refreshed on demand via "Refresh all".
- **Features** show new releases with a tracked artist credited as a feature.
- When you add an artist, their existing catalog is stored in `releases.db` as a baseline so only releases that appear *afterwards* show up as new. Anything not out yet shows under **Upcoming** right away.
- Click on any artist card to show their Apple Music catalog; any release can be hidden with **Edit Catalog** or added to **Listen later**.

## Files

| File | Description|
|---|---|
| `releases.py` | Server side: SQLite database, iTunes API calls, refresh logic, the HTTP server. Standard library only. |
| `index.html` | The whole interface: HTML, CSS, and JS in one file. |
| `run.pyw` | Double-click launcher. |
| `icon.ico` | Icon file for optional shortcut. |
| `releases.db` | SQLite database of tracked artists, release catalogs, and user state (favorites, muted artists, hidden releases, Listen later queue). |
| `backups/` | Folder containing the 7 most recent daily `releases.db` backups. |
| `image_cache/` | Folder containing tracked artist photos and release artwork for faster page loading. |
| `tracker.log` | Debug log, written when launched via `run.pyw`. |

## Limitations

- Apple's iTunes Search API caps a catalog lookup at 200 entries and doesn't support real pagination, so very large catalogs (200+ releases) are periodically re-checked to catch anything that fell outside the lookup window. The app flags an artist's catalog as possibly incomplete when this applies.
- Playlist importing is done through [Soundiiz](https://soundiiz.com) as the Apple Music API doesn't have a *free* way to create a playlist directly.
- Primarily tested on Firefox/Win11.

## License/Disclaimer
<div>
  <a href="https://www.paypal.com/donate/?business=AGHTUY36VURVW&no_recurring=0&item_name=I+appreciate+you+visiting+this+page%21+Thank+you%21&currency_code=USD">
    <img align="right" height="67" alt="paypal-donate-button" src="https://github.com/user-attachments/assets/affd4de6-0740-461f-978d-8db0f116ea1c" />
  </a>

  Release Tracker is licensed under the <a href="LICENSE">MIT</a> License.
</div>

PayPal is a registered trademark of PayPal, Inc. The PayPal logo is a trademark of PayPal, Inc.
