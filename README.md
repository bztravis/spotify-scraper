# Spotify Playlist Cleaner

A local command-line app that reads a playlist you own, previews duplicate removals, and creates a new **private** playlist with unique songs in their original order. The source playlist is never modified.

Requires Python 3.10 or newer on macOS or Linux. No packages to install, hosted server, client secret, or Soloist key required.

## One-time Spotify setup

1. Use a Spotify Premium account and open the [Developer Dashboard](https://developer.spotify.com/dashboard).
2. Create an app with **Web API** selected.
3. Register this exact redirect URI in its settings:
   ```text
   http://127.0.0.1:8888/callback
   ```
4. Make sure your account is allowed to use the development-mode app in its Users and Access settings. Development mode is limited to five authorized users.
5. Copy the app's **Client ID**, then run from this directory:
   ```bash
   python3 playlist_cleaner.py setup
   python3 playlist_cleaner.py login
   ```

Login opens Spotify in your browser. Approve playlist access and return to the terminal. PKCE is used with a random state check, and the local callback listener closes after login or a three-minute timeout. Future commands refresh access automatically. If the app's permissions change, run `login` again.

On macOS the refresh token is stored in Keychain under `spotify-playlist-cleaner`. On Linux it is stored in a file restricted to your user. Access tokens stay in memory. Client configuration, reports, and recovery records live in `.playlist-cleaner/`, which is ignored by Git. Reports contain song and playlist metadata, so treat them as personal data.

## Use

Song loading defaults to one worker per remaining page after the initial page establishes the total. For example, 1,000 songs takes one initial request and then 19 concurrent page requests. Single-page sources need no worker pool. Results are assembled in source order even when requests finish out of order. This applies to playlist and Liked Songs reads for `histogram`, `preview`, and `create`; playlist creation and additions remain sequential.

Set `--workers` to any positive integer on those commands (`1` fetches sequentially). There is no configured upper limit. The app uses the smaller of your requested worker count and the number of remaining pages, and never requests offsets past the total song count. For example, 125 songs with `--workers 100` uses two workers after the first 50-song page, fetching 50 and 25 songs:

```bash
python3 playlist_cleaner.py histogram liked --workers 4 --ignore-cache --open
python3 playlist_cleaner.py create 'PLAYLIST_URL' --workers 2
```

Each page still contains at most 50 songs. More workers can reduce network waiting but do not reduce the number of API requests or bypass Spotify quotas. Workers share a rate-limit cooldown and synchronized token refresh. Use fewer workers if Spotify frequently rate-limits requests. Cached histogram runs reuse their source data regardless of worker count.

List playlists you own:

```bash
python3 playlist_cleaner.py playlists
```

Preview using a playlist URL, URI, or ID:

```bash
python3 playlist_cleaner.py preview 'https://open.spotify.com/playlist/YOUR_PLAYLIST_ID'
```

Create a new playlist after reviewing an interactive preview:

```bash
python3 playlist_cleaner.py create 'https://open.spotify.com/playlist/YOUR_PLAYLIST_ID'
```

Customize its name or run without the confirmation prompt:

```bash
python3 playlist_cleaner.py create 'PLAYLIST_URL' --name 'My clean playlist' --yes
```

By default, duplicates are repeated Spotify track IDs. Relinked IDs are handled when Spotify provides the original identifier. Keep the first occurrence and preserve order. This intentionally retains different releases that have different Spotify IDs.

To additionally match the same recording across releases using ISRC when available:

```bash
python3 playlist_cleaner.py preview 'PLAYLIST_URL' --mode recording
python3 playlist_cleaner.py create 'PLAYLIST_URL' --mode recording
```

ISRC matching depends on Spotify's metadata and is not guaranteed to identify every equivalent recording. Songs without ISRC still use ID matching. The app does not guess equivalence from titles, so live versions and remixes are not accidentally merged by title matching.

Removed/unavailable items, local files, non-song items, and explicitly unplayable tracks are reported and skipped. The full preview report is saved to `.playlist-cleaner/last-preview.json`. If there are no transferable tracks, no playlist is created.

## Plot songs added over time

### Liked Songs

Use `liked` instead of a playlist link. First authorize the additional `user-library-read` permission (existing users must log in again):

```bash
python3 playlist_cleaner.py login
python3 playlist_cleaner.py histogram liked --open
python3 playlist_cleaner.py histogram liked --interval year --open
```

The histogram uses each currently liked song's saved date. Unliked songs are absent. The app fetches every page of your saved tracks. Avoid liking/unliking songs during fetching: Spotify provides no library snapshot identifier, so changes during pagination cannot reliably be detected.

You can also preview duplicates or create a separate deduplicated playlist from Liked Songs:

```bash
python3 playlist_cleaner.py preview liked --mode recording
python3 playlist_cleaner.py create liked --mode recording
```

This creates a regular private playlist named `Liked Songs — Deduplicated` and does not change your likes. It keeps the order returned by Spotify's saved-tracks endpoint.

### Regular playlists

Plot a playlist's song addition dates, using monthly bins by default:

```bash
python3 playlist_cleaner.py histogram 'PLAYLIST_URL' --open
```

Choose daily, weekly, monthly, or yearly bins and an optional output path:

```bash
python3 playlist_cleaner.py histogram 'PLAYLIST_URL' --interval week --output additions.svg --open
```

The x-axis now shows up to roughly 48 date labels automatically, including the first and last bins. To label every month explicitly, use `--interval month --label-every 1`. Use `--label-every 3` for every third bin. The SVG expands to keep denser labels readable; zoom or scroll in your viewer for long timelines.

Filter by addition date with either or both bounds:

```bash
python3 playlist_cleaner.py histogram liked --min-date 2024-01-01 --max-date 2024-12-31 --open
```

Dates must be `YYYY-MM-DD` and are inclusive in UTC: `--max-date 2024-12-31` includes the entire day. Filtering happens before binning, so edge bins count only songs inside your range. Explicit bounds also extend the axis to include empty periods; month/week/year labels still indicate the start of each bin. Out-of-range songs are counted separately in the report. Changing date bounds reuses the same five-minute source cache.

Histogram source data is cached for **five minutes** after a successful fetch, for both playlists and `liked`. Changing bins, label spacing, or output paths reuses the same source cache. The command prints whether it uses cached data or downloads fresh data. It still verifies your Spotify account with one API request; cached data is separated by account and source.

To bypass the cache and replace it with fresh Spotify data:

```bash
python3 playlist_cleaner.py histogram liked --ignore-cache --open
```

Expired or invalid cache files are refetched automatically. Cache files are stored in `.playlist-cleaner/histogram-cache/` with the same restricted file permissions as other local app data. Delete that directory to clear them. Preview and creation always fetch fresh data and do not use the histogram cache.

The command uses your existing Spotify login and does not modify playlists. It saves an SVG chart (viewable in your browser) and a JSON file with exact bin counts. By default these are `.playlist-cleaner/histogram.svg` and `.playlist-cleaner/histogram.json`; repeat runs overwrite these outputs. Hover over a bar to see the date and count.

Dates come from each entry's `added_at`, interpreted in UTC. Weeks start on Monday and empty periods appear with zero counts. Repeated song entries count separately. Songs with missing/invalid dates are reported separately; removed/unavailable entries and non-song items are excluded. Local songs with known dates can be counted even though they cannot be copied by the deduplication command.

This shows the addition dates of songs **currently in the playlist**, not a complete activity history. Previously removed songs cannot be reconstructed. A newly created deduplicated copy has new addition dates, so use the original playlist to examine its earlier additions.

## Recovery

Creation prints a run ID before writing to Spotify. If interrupted, run:

```bash
python3 playlist_cleaner.py resume RUN_ID
```

The app reads the destination before appending, resumes after its existing matching prefix, and verifies the final complete track sequence. Completed runs can be resumed without creating another playlist. A fresh `create` intentionally starts a new run and creates a separate playlist.

If creation itself timed out, the app looks for the unique run marker in your playlists. If the outcome remains uncertain it stops instead of creating a second playlist; wait and resume again. If the playlist never appears, inspect Spotify before starting a fresh run. Do not edit the destination while a run is active. Concurrent invocations are blocked locally.

Source changes during fetching abort the preview. The copy uses the captured source contents; edits made to the source afterward are not included. Destination edits are detected on resume or final verification. Spotify offers no transaction covering the entire copy, so avoid concurrent edits while it runs.

Rate limits honor `Retry-After` up to 60 seconds per wait. Longer limits stop with a retry message. Writes with uncertain outcomes are never blindly retried. A failed write can leave a partially populated playlist; use `resume` to finish it.

## Troubleshooting

- **Command appears stuck:** commands now print the current stage and page counts (`Loaded 50 / 2500 items`). Liked Songs requires one request per 50 songs. If it stops at Keychain, check for a macOS permission dialog or unlock your login keychain; Keychain operations time out after 30 seconds. Network requests also have a 30-second timeout. Stop an older process with Ctrl+C and retry to use the updated progress messages.
- **403:** confirm the source is owned by your account, your account is allowed in the developer app, and Premium is active. Run `login` again if permissions changed.
- **Login failure:** verify Client ID and the exact redirect URI; use `127.0.0.1`, not `localhost`.
- **Port 8888 occupied:** stop the application using that port, then retry login.
- **Forget local authorization:** run `python3 playlist_cleaner.py logout`. Revoke access in Spotify account settings to invalidate authorization on Spotify's side too.

## Development

```bash
python3 -m unittest discover -s tests -v
```

Tests exercise deduplication, relinking, pagination, skipped items, rate limits, source changes, interrupted creation, accepted writes followed by timeouts, and repeated resumes. Tests use a fake Spotify service and do not modify your account. A live end-to-end run requires your developer app and browser authorization.

The app uses the current `/me/playlists` and `/playlists/{id}/items` endpoints. See [PKCE](https://developer.spotify.com/documentation/web-api/tutorials/code-pkce-flow), [playlist items](https://developer.spotify.com/documentation/web-api/reference/get-playlists-items), and [quota modes](https://developer.spotify.com/documentation/web-api/concepts/quota-modes).
