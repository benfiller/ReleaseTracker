# Release Tracker

A personal Apple Music release tracker desktop app: track artists you choose and get notified of their new releases, with links that open straight in the Apple Music app.

Just a small local Python server plus one HTML browser page; nothing to install beyond Python itself.

## Features

- Track any number of artists and show their **new releases** as they come out
- **Features** tab separate from tracked artists' own releases (Apple Music's "Appears on")
- **Upcoming** releases show up as soon as Apple lists them, then move to **New releases** once they drop
- **Duplicate detection:** remasters, explicit/clean copies, and reissues that don't add tracks are hidden automatically
- Any release can be added to a **Listen later** queue, collected under a separate tab in the feed
- **Favorite** an artist to have their releases show up first in the feed
- **Mute** an artist to hide any releases they are credited on across the entire feed
- **Playlist export:** build a CSV or plain-text song list from a feed tab, ready to import into [Soundiiz](https://soundiiz.com) and sync to Apple Music (Apple's API has no *free* way to create playlists directly)
- Artist photos and artwork are cached to disk so thumbnails load instantly even after the browser cache clears
- Daily local backups of the database, dark/light theme, responsive layout

## Requirements

- Python 3.9+
- Any modern browser

## Running it

1. Download or clone this repo.
2. Double-click `run.pyw` to start the app.
3. A local server starts at `http://127.0.0.1:8765/` and a browser tab opens automatically.
4. The server runs as long as the tab stays open and shuts itself down a few seconds after you close the last open tab.

- `releases.db` (your data) and `backups/`, `image_cache/`, `tracker.log` are created automatically next to the script the first time you run it.
- To pin a shortcut with a custom icon: right-click run.pyw → Create shortcut → drag it to your Start Menu or taskbar → right-click the shortcut → Properties → Change Icon → select icon.ico.

## Files

| File | Description|
|---|---|
| `releases.py` | Server side: SQLite database, iTunes API calls, refresh logic, the HTTP server. Standard library only. |
| `index.html` | The whole interface: HTML, CSS, and JS in one file. |
| `run.pyw` | Double-click launcher. |
| `icon.ico` | Icon file for optional shortcut. |
| `backups/` | Folder containing the 7 most recent `releases.db` backups, created automatically on first run. |
| `image_cache/` | Folder containing tracked artist photos and release artwork, created automatically on first run. |
| `tracker.log` | Debug logging, created automatically on first run. |

## Using the app

- Track an artist by searching their name or entering their Apple Music link under the **Artists** tab.
- **Feed** shows all new releases from tracked artists, refreshed every time you open the app (if not opened in the last 6 hours). Can be refreshed on demand via "Refresh all".
- **Features** show new releases with a tracked artist credited as a feature.
- When you add an artist, their entire existing catalog is stored in `releases.db` as a baseline so only releases that appear *afterwards* show up as new. Anything not out yet shows under **Upcoming** right away.
- Click on any artist card to show their Apple Music catalog; any release can be hidden with **Edit Catalog** or added to **Listen later**.

## Limitations

- Apple's iTunes Search API caps a catalog lookup at 200 entries and doesn't support real pagination, so very large catalogs (200+ releases) are periodically re-checked to catch anything that fell outside the lookup window. The app flags an artist's catalog as possibly incomplete when this applies.
- Mostly tested on Windows with Firefox, should still work on other OSes/browsers.

## License
<div>
  <a href="https://www.paypal.com/donate/?business=AGHTUY36VURVW&no_recurring=0&item_name=I+appreciate+you+visiting+this+page%21+Thank+you%21&currency_code=USD">
    <img align="right" height="72" alt="paypal-donate-button" src="https://github.com/user-attachments/assets/affd4de6-0740-461f-978d-8db0f116ea1c" />
  </a>

  Release Tracker is licensed under the <a href="LICENSE">MIT</a> License.
</div>

**Disclaimer:** PayPal is a registered trademark of PayPal, Inc. The PayPal logo is a trademark of PayPal, Inc.
