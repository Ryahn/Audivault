# Audivault

Audivault is an open-source, self-hosted audiobook server with Audiobookshelf API compatibility. Clients like [AudioBooth](https://github.com/AudioBooth/AudioBooth) can connect and stream your audiobook collection.

It also includes tools for importing, organizing, renaming, and de-duplicating audiobooks, so you can keep your library structured without having to manually sort through folders. Libraries are watched for changes, and the web interface updates live when scans and other jobs finish.

Books can be organized by author, series, and title, with customizable naming templates to match how you prefer to manage your collection.

## Contents

- [Features](#features)
- [Installation](#installation)
- [Docker installation](#docker-installation)
- [Library settings](#library-settings)
- [Folder naming](#folder-naming)
- [Connecting audiobook apps](#connecting-audiobook-apps)
- [Data and backups](#data-and-backups)
- [Troubleshooting](#troubleshooting)
- [Testing](#testing)
- [Releases](#releases)

## Features

### Audiobook support

Audivault supports most common audiobook and audio formats, including M4B, M4A, MP3, FLAC, OGG/Opus, WAV, AAC, and WMA. EPUB and PDF files in a book folder are picked up as the book's ebook.

Metadata is read with Mutagen. Chapters come from:

- QuickTime chapter tracks and Nero chapters in M4B/M4A files
- ID3 `CHAP` frames in MP3 files
- `.cue` sheets, when the library enables them (see [Chapters from cue sheets](#chapters-from-cue-sheets))
- one chapter per file, for books split into several files without chapter markers

If `ffprobe` is installed (it is in the Docker image), it is used as a fallback for files Mutagen can't read.

### Library organization

Audivault follows Audiobookshelf's folder naming conventions, including:

- Books organized by author and series
- Volume numbers, publication years, narrators, and ASIN identifiers in folder names
- Multi-disc audiobooks stored in folders such as `CD 1`, `Disc 2`, or `Part 3`
- Individual audiobook files stored directly in the library root

A library can contain several folders, for example a main collection and a second drive.

Metadata is collected from these sources, lowest priority first:

1. Folder and file names
2. Embedded audio tags (title, author, narrator, series, ASIN…)
3. `desc.txt` and `reader.txt`
4. `.opf` files
5. `metadata.json` (Audiobookshelf format)

Embedded tags override folder names by default; the **Prefer folder names over embedded tags** setting reverses that. Edits made in the app are saved to the database and, by default, written back to `metadata.json` in the book's folder.

Hidden folders (names starting with `.`) are skipped, and so is any folder containing a file named `.ignore`.

### Chapters from cue sheets

Each library has a **Use .cue files for chapters** setting. When it's on and a book folder contains a `.cue` file, the cue sheet's tracks become the book's chapters, taking priority over embedded chapters and `metadata.json`.

Single-file cues (one `.m4b` or `.mp3` with a `.cue` beside it) and multi-file cues (one `FILE` entry per disc or part) are both supported. If the cue refers to a different file name than the audio (for example the original `.wav` rip), it is matched by name and then by order.

Turning the setting on or off refreshes the chapters of existing books straight away; no rescan is needed. Chapters you edit by hand in the app are kept until the audio or the cue file changes.

### Automatic scanning and live updates

Audivault watches each library folder and import folder for changes:

- **Partial scans:** when files change, only the affected top-level folder (usually one author) is rescanned. It waits until files stop changing, so a book that is still copying isn't picked up half-finished.
- **Full scans:** walk the whole library, but only re-read books whose files changed. Use **Scan** in the library settings. A library can also scan on a timer.
- **Force rescan:** re-reads metadata for every book.

Watching uses file system events, or polling for network shares and Docker Desktop on Windows/macOS, where file events don't reach the server. You can choose per library, or let `ABSTREAM_WATCH_POLLING` decide.

The web interface receives live updates over Server-Sent Events:

- Notifications when scans, renames, quick matches, chapter refreshes, and duplicate clean-ups finish, with counts such as "3 added, 1 missing"
- A notification with a link when new books land in the import folder
- A notification with a link when a scan finds new duplicates
- Home and Library pages refresh themselves when their library changes

**Settings → Activity** shows the live connection status, which folders are being watched and how, running and recent tasks, and an activity log.

If a library folder is unreachable (an unmounted share or a disconnected drive), its books are left alone instead of being marked missing.

### Metadata and book matching

Audivault can search several providers to find missing or updated book information:

- Audible, including its catalog API and Audnexus
- Google Books
- iTunes
- Open Library
- AudiobookCovers.com

Depending on what's available from the provider, it can retrieve book descriptions, cover artwork, author information, author photos, and chapter lists. A book can be matched from its **Edit → Match** tab, or several at once with **Select → Quick match** on the library page.

### Importing audiobooks

Each library can have its own import folder.

Place new audiobooks in that folder (book folders or single audio files) and they appear on the **Import** page, with metadata guessed from their names, tags, and ASINs in file names such as `[B0CMYLRB39]`. Use **Find metadata** to fill in the details from a provider, check the target folder shown under each book, and import.

**When importing** sets the library's default operation: **move** the files into the library, or **copy** them and leave the originals in the import folder. The Import page starts with that choice, and automatic imports always use it.

This gives you a way to review and organize new additions before they become part of your collection.

#### Automatic import

With **Import automatically** on, new books in the import folder are imported without opening the Import page:

1. **Wait for copying to finish.** A book is only touched once its files have stopped changing and its newest file is at least a minute old, so a book that is still being copied is never moved halfway.
2. **Match it.** The book is looked up with the library's metadata provider. It is imported only on a confident match: the same ASIN, or a closely matching title **and** author (and, when the provider knows the runtime, a length within 5%).
3. **Import it** with the default operation, using the matched metadata and cover.

Books without a confident match stay on the Import page marked **Needs review**, with the reason (for example *"author doesn't match"* or *"length differs from the match"*). Import them by hand after checking the details. Automatic import runs when files arrive (through folder watching), at startup, and when you switch it on; **Run auto-import** on the Import page runs it on demand.

Audivault remembers each book it has dealt with, so in copy mode the originals left behind aren't imported twice, and a book needing review isn't looked up again until its files change. Books imported by hand in copy mode are remembered the same way.

### Renaming and organizing

If you update a book's metadata, Audivault can rename its folders to match.

The **Organize** page previews the proposed changes for a whole library, with a status for every book (ready, capitalization, unchanged, conflict, missing, leftover), and applies the ones you select. A rename that only changes capitalization is included. A missing entry whose files are already in the library under another path is a leftover: selecting it removes that entry and keeps listening progress on the copy that is still there. A single book can be renamed from its page with **⋯ → Rename folder**. Any empty directories left behind after the move are cleaned up automatically.

Two library settings control when this happens automatically:

- **Organize on import** (on by default): imported books are placed using the naming template. When off, they keep the folder or file name they had in the import folder.
- **Organize on scan** (off by default): after every scan, including automatic ones from folder watching, books whose folder doesn't match the template are renamed, including a change that only fixes capitalization. Books that would land in a folder already used by something else, books whose folder is really gone, leftover entries, and books with no author are skipped. Turning it on shows how many folders would move and asks you to confirm. Leftover entries are removed from the Organize page.

The **Rename audio files** setting also renames the audio files when organizing, to `Title.m4b` for single-file books or `Title - Part 01.mp3` for multi-file books.

### Duplicate manager

The **Duplicates** page (with a count in the top bar) lists books that share an ASIN, or the same title and author, across all libraries and folders.

#### Choosing which copy to keep

Every group comes with a recommendation and a one-line reason, for example *"Keep the copy in /audiobooks_audible: 115 kbps vs 31 kbps (3.7× the bitrate), includes opening/end credits, has ASIN."* Each copy shows which library folder it lives in, its length, size, format and chapter count, and a checklist of what was found:

- **Audio quality:** average bitrate, rated high (96 kbps and up), medium, or low. Low-bitrate copies are usually re-encodes of the original.
- **Length:** compared with the official Audible runtime (looked up by ASIN, including an ASIN found in file names like `[B0CRBN23Q2]`), or with the length most copies agree on.
- **Same audio saved more than once:** a folder holding the same book in several encodes (for example `Part 01` at 129 kbps and `Part 02` at 31 kbps). When the best audio is inside such a folder, the group suggests excluding the duplicate track instead, with a one-click fix; the excluded file stays on disk.
- **Broken copies:** no playable audio.
- **Chapters:** named or only numbered, and whether opening and end credits are included.
- **Metadata:** ASIN, series number, cover, cue sheet, description, and whether the folder already matches the naming template.
- **Identical audio:** when copies have byte-for-byte the same audio, only metadata and organization differ, and either copy is fine.

#### Resolving a group

Pick the copy to keep and choose **Resolve…**. For each copy being discarded you get a file-by-file comparison:

- Files only the discarded copy has can be copied over. Sidecar files (metadata, text, ebooks, and cue sheets for identical audio) are preselected; audio never is.
- Files that are larger in the discarded copy, such as a fuller `metadata.json`, can replace the kept copy's version.
- A `.cue` sheet from a different encode is never preselected, because its chapter times may not line up with the kept audio.

Listening progress, bookmarks, collections, and playlists move to the kept copy. If metadata files were brought over, the kept book's metadata is re-read from them.

Discarded copies go to `.abstream-trash/<date-time>/` inside their library folder, where they can be recovered from **Settings → Trash**. The trash is never scanned, and it is emptied automatically after 30 days by default (see [Trash](#trash)). You can also choose to delete discarded copies permanently, which asks for an extra confirmation.

**Select recommendations** picks the recommended copy in every group, and **Resolve selected** resolves them all in one go using the default file selections, moving discarded copies to the trash.

Groups that are different editions rather than duplicates can be hidden with **Not duplicates**. If another copy appears later, the group shows up again.

### Locked libraries and folders

A whole library, or any of its folders, can be **locked** to make it a source of truth, for example a folder that an Audible downloader keeps up to date. Audivault never deletes or overwrites a file in a locked folder:


| Action                     | When locked                                                                                                                                                                                                                                                  |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Delete files** on a book | Not available ("Remove from library", which only touches the database, still works)                                                                                                                                                                          |
| Duplicate manager          | A locked copy is always the recommended keep and can never be discarded. Files missing from it can still be added from a duplicate, but its existing files are never replaced. A group with two locked copies can't be resolved until one folder is unlocked |
| Trash                      | Trash inside a locked folder is never emptied, automatically or by hand. Restoring still works                                                                                                                                                               |
| Covers                     | Setting a new cover never deletes the old cover file; the new one is stored in the app's metadata folder                                                                                                                                                     |
| Organize                   | Renaming and moving still work. Clean-up only removes truly empty folders (no `desktop.ini` or `Thumbs.db` is deleted)                                                                                                                                       |


Lock a whole library with **Lock the whole library** in its settings, or a single folder with the lock button next to it in the folder list. Locked books, copies, and trash show a lock badge. Anything that would break a lock is refused with HTTP `423 Locked`, also when it comes from the API.

### Trash

**Settings → Trash** lists everything in the trash: each batch of discarded copies, the books in it, their size, and when they will be deleted for good. From there you can:

- **Restore** a book: it is moved back to its original place in the library and scanned back in.
- **Delete** a single book or a whole batch right away.
- **Empty trash now.**

The trash is emptied automatically once batches are older than the retention period: **30 days** by default, checked at startup and then every hour. Set it to `0` to keep the trash until you empty it yourself. The period can be changed in the app, or with the `ABSTREAM_TRASH_DAYS` environment variable (`TRASH_DAYS` in Docker Compose). When the variable is set, it wins and the setting in the app is locked.

### Web interface

The web interface is built with Alpine.js and Bootstrap and includes:

- A home page with personalized shelves (continue listening, continue series, recently added, recent series, discover, listen again, newest authors)
- Library browsing with search, filters, sorting, and collapsible series, plus series, author, and collection views
- Book pages with chapters, files, progress, and actions (match, rename folder, rescan, download as ZIP, add to collection, remove, delete files)
- An editor for details, matching, covers, chapters (including chapter lookup from Audible by ASIN), and tracks (exclude files from playback)
- **Import**, **Organize**, and **Duplicates** pages, and a **Trash** page to restore or delete discarded copies
- A player with chapter navigation, playback speed, a sleep timer (including *end of chapter*), and lock-screen/media-key controls
- Live notifications and an activity page
- User, permission, and API key management
- Light and dark themes, with matching scrollbars and layouts that work on phones

Keyboard shortcuts while the player is open: **Space** plays/pauses, **←** jumps back 10 seconds, **→** jumps forward 30 seconds. **Escape** closes the topmost dialog.

## Installation

Audivault requires **Python 3.11 or newer**.

### Running directly with Python

First, create a virtual environment:

```bash
python -m venv .venv
```

Install the required dependencies:

```bash
.venv/Scripts/pip install -r requirements.txt
```

Start the server:

```bash
.venv/Scripts/python run.py
```

On Linux or macOS, replace `.venv/Scripts/` with `.venv/bin/`.

Once the server is running, open:

**[http://localhost:13378](http://localhost:13378)**

Create your administrator account, then open **Settings → Libraries** to add your audiobook folder.

You can also configure an import folder if you want to use the built-in import workflow.

### Environment variables

You can change the following settings through environment variables:


| Variable                  | Default               | Description                                                                                    |
| ------------------------- | --------------------- | ---------------------------------------------------------------------------------------------- |
| `ABSTREAM_PORT`           | `13378`               | HTTP port used by the server                                                                   |
| `ABSTREAM_HOST`           | `0.0.0.0`             | Network address the server listens on                                                          |
| `ABSTREAM_DATA_DIR`       | `./data`              | Base folder for the database, cached covers, and author images                                 |
| `ABSTREAM_CONFIG_DIR`     | `<data dir>/config`   | Database location                                                                              |
| `ABSTREAM_METADATA_DIR`   | `<data dir>/metadata` | Cached covers and author images                                                                |
| `ABSTREAM_ACCESS_TTL`     | `43200` (12 hours)    | Access token lifetime in seconds                                                               |
| `ABSTREAM_REFRESH_TTL`    | `2592000` (30 days)   | Refresh token lifetime in seconds                                                              |
| `ABSTREAM_WATCH_POLLING`  | off                   | Watch folders by polling instead of file system events (for libraries set to *Automatic*)      |
| `ABSTREAM_WATCH_INTERVAL` | `30`                  | Seconds between polls                                                                          |
| `ABSTREAM_WATCH_DELAY`    | `15`                  | Seconds of no changes before a partial scan starts                                             |
| `ABSTREAM_TRASH_DAYS`     | not set (app: 30)     | Days to keep the trash before emptying it (`0` = forever). When set, the app setting is locked |
| `ABSTREAM_IMPORT_SETTLE`  | `60`                  | Seconds a book's newest file must be untouched before automatic import picks it up             |


**Note:** The web interface loads Bootstrap, Bootstrap Icons, Alpine.js, and Google Fonts through external CDNs, so your browser needs an internet connection to load those resources. Connected mobile apps don't have this requirement. Looking up metadata, covers, and Audible runtimes also needs internet access on the server.

## Docker installation

If you prefer Docker, you can start Audivault with Docker Compose:

```bash
docker compose up -d --build
```

Once the container starts, open **[http://localhost:13378](http://localhost:13378)** to complete the initial setup.

### Upgrading

Compose recreates the container when the service name changes (`audiobookstream` to `audivault`). The database and metadata volumes are host folders, so they stay as long as those mounts are unchanged. Do not rename `.abstream-trash`, `abstream.db`, or the `ABSTREAM_*` variables for the new name.

Before upgrading:

1. Stop the server so nothing is writing the database.
2. Back up with SQLite's backup API, not by copying `abstream.db` next to its `-wal` and `-shm` files. From the config directory (`docker-data/config` with Docker):

```bash
sqlite3 abstream.db ".backup 'abstream-backup.db'"
sqlite3 abstream-backup.db "PRAGMA integrity_check;"
```

`integrity_check` must print `ok` before the backup counts. Tag or otherwise keep the image you are running (`audiobookstream:latest`, or the image id from `docker image inspect`) before `docker compose up -d --build` replaces it.

Rollback is that previous image plus the backup that passed the check. Pointing an older build at a database a newer build has already opened is not a rollback: startup adds columns, and an older build is not guaranteed to understand them. Remove the old `audiobookstream` container only after the new one is healthy and the backup has passed `integrity_check`.

### Docker volumes

The included Compose configuration uses the following folders:


| Host folder              | Container path | Purpose                                 |
| ------------------------ | -------------- | --------------------------------------- |
| `./audiobooks`           | `/audiobooks`  | Main audiobook library                  |
| `./import`               | `/import`      | Optional folder for importing new books |
| `./docker-data/config`   | `/config`      | Database and application settings       |
| `./docker-data/metadata` | `/metadata`    | Cached book covers and author images    |


You can change the host paths to match your storage setup.

For example, if your audiobooks are stored at `/mnt/media/audiobooks`, update the volume mapping to:

```yaml
- /mnt/media/audiobooks:/audiobooks
```

To use more than one folder, mount each one at its own container path and add them all to a library (or to separate libraries):

```yaml
- D:/Audiobooks:/audiobooks2
- D:/Audiobooks_Audible:/audiobooks_audible
```

When adding a library through the web interface, use the **container path** (`/audiobooks`), not the host path. The folder browser in the library settings shows the container's folders.

If you're using the import feature, set the import directory to `/import`.

### Compose options

These can be set in a `.env` file next to `docker-compose.yml`:


| Variable        | Default            | Description                                                                 |
| --------------- | ------------------ | --------------------------------------------------------------------------- |
| `HOST_PORT`     | `13378`            | Port on the host                                                            |
| `TIMEZONE`      | `America/New_York` | Time zone for logs, listening stats, and the activity log                   |
| `WATCH_POLLING` | `1`                | Watch folders by polling (see below)                                        |
| `TRASH_DAYS`    | empty              | Days to keep the trash (`0` = forever). Leave empty to manage it in the app |


For example:

```text
HOST_PORT=13380
TIMEZONE=America/Chicago
```

### File permissions

The container uses `PUID` and `PGID` to control file ownership. Both default to `1000`.

During startup, the container briefly runs as root to correct ownership of `/config` and `/metadata`. It then switches to the configured user and group.

Files imported or renamed within your audiobook library will be owned by that user. Existing audiobook folders are not recursively `chown`ed.

For the fewest permission issues, set `PUID` and `PGID` to match the user who owns your audiobook directories.

### Changing the port

Audivault uses port `13378` by default, the same port used by Audiobookshelf.

If you're already running Audiobookshelf on the same machine, change the host port, either in `.env` or on the command line:

```bash
HOST_PORT=13380 docker compose up -d
```

Audivault will then be available at **[http://localhost:13380](http://localhost:13380)**.

### Folder watching in Docker

File change events from a Windows or macOS host (Docker Desktop) or from network shares don't reach the container, so the Compose file enables polling (`WATCH_POLLING=1`). On a Linux host with local disks you can turn polling off for instant detection:

```bash
WATCH_POLLING=0 docker compose up -d
```

The Docker image also includes `ffmpeg` and `ffprobe` for improved chapter detection.

## Library settings

Open **Settings → Libraries → Edit** to change these per library:


| Setting                                | Default                                      | What it does                                                                           |
| -------------------------------------- | -------------------------------------------- | -------------------------------------------------------------------------------------- |
| Library folders                        |                                              | One or more folders with audiobooks; each can be locked                                |
| Lock the whole library                 | off                                          | Never delete or overwrite files in any of its folders                                  |
| Import folder                          | none                                         | Inbox for the Import page                                                              |
| When importing                         | Move                                         | Move or copy files into the library (default for the Import page and automatic import) |
| Import automatically                   | off                                          | Import confident matches from the import folder on their own                           |
| Folder naming template                 | `{author}/[{series}/][{sequence} - ]{title}` | Used by import and organize; examples update as you type                               |
| Metadata provider                      | Audible.com                                  | Default provider for matching                                                          |
| Auto scan (min)                        | `0` (off)                                    | Full scan on a timer                                                                   |
| Finished when (s) left                 | `10`                                         | Marks a book finished when this little is left                                         |
| Organize on import                     | on                                           | Place imported books using the template                                                |
| Organize on scan                       | off                                          | Rename non-matching folders after every scan                                           |
| Use .cue files for chapters            | off                                          | Cue sheets supply chapters                                                             |
| Watch the import folder                | on                                           | Notify when new books arrive                                                           |
| Write metadata.json into book folders  | on                                           | Keep your edits with the files                                                         |
| Save covers inside book folders        | on                                           | Otherwise covers go to the metadata folder                                             |
| Rename audio files when organizing     | off                                          | `Title - Part 01.mp3` style names                                                      |
| Parse subtitle from folder name        | off                                          | Split `Title - Subtitle` folder names                                                  |
| Prefer folder names over embedded tags | off                                          | Folder names win over tags                                                             |
| Hide single-book series                | off                                          | In series views and collapsed series                                                   |
| Watch library folders for changes      | on                                           | Partial scans on changes                                                               |
| Detection method                       | Automatic                                    | File system events or polling                                                          |


## Folder naming

Audivault supports customizable naming templates, letting you decide how imported and organized audiobooks should be stored.

Available tokens include:

`{author}` `{series}` `{sequence}` `{title}` `{subtitle}` `{year}` `{narrator}` `{asin}`

You can place optional parts of a template inside square brackets. If any token within a bracketed section has no value, that entire section is left out.

The default template is:

```text
{author}/[{series}/][{sequence} - ]{title}
```

For example, it creates folders like:

```text
Brandon Sanderson/
└── Mistborn/
    └── 1 - The Final Empire/

Andy Weir/
└── Project Hail Mary/
```

The scanner understands this structure and can read the relevant metadata back from the folder names. Characters that aren't allowed in file names (such as `:` or `?`) are replaced, and books with several authors use `Author One & Author Two`.

## Connecting audiobook apps

Audivault is designed to work with apps that support the Audiobookshelf API.

For example, you can connect [AudioBooth](https://github.com/AudioBooth/AudioBooth) by entering your Audivault server address:

```text
http://192.168.1.10:13378
```

Replace the IP address with the address of the machine running Audivault (and the port, if you changed it). The server reports itself as Audiobookshelf-compatible, so apps that discover servers on the local network can find it.

### Authentication

There are three supported ways to sign in:

- **Username and password:** Uses JWT access and refresh tokens, including refresh token rotation.
- **API key:** Generate a key from **Settings → API keys**.
- **Legacy user token:** Supported for compatibility with clients that use the older authentication method.

### API compatibility

Audivault implements the portions of the Audiobookshelf API needed for library browsing, playback, account management, and supported client integrations.

The implemented endpoints and features include:


| Area           | Supported functionality                                                                     |
| -------------- | ------------------------------------------------------------------------------------------- |
| Authentication | Server status, login, token refresh, logout, and authorization                              |
| Libraries      | Items, filters, sorting, pagination, collapsed series, search, and library statistics       |
| Browsing       | Authors, narrators, series, collections, and playlists                                      |
| Home           | Personalized shelves and library overview                                                   |
| Audiobooks     | Expanded item details, covers, downloads, metadata matching, chapters, and batch operations |
| Streaming      | Audio file streaming with HTTP Range support                                                |
| Playback       | Play sessions, tracking, synchronization, session closing, and local session management     |
| User accounts  | Profiles, listening progress, bookmarks, and listening history                              |
| Statistics     | Listening statistics and yearly activity                                                    |
| Administration | User management and API keys                                                                |


This includes routes such as `/status`, `/login`, `/auth/refresh`, `/logout`, `/api/authorize`, `/api/me`, and `/public/session/:id/track/:n`, along with the relevant library, item, user, and playback session endpoints. Features specific to Audivault (import, organize, duplicates, live events) live under `/api/abstream/`.

Interactive docs (Swagger UI) are at `/api-docs`. The OpenAPI document is at `/openapi.json`. Authorize there with an access token from `POST /login` (header `x-return-tokens: true`) or with an API key from **Settings → API keys**.

### Features not currently supported

Audivault does not currently implement every Audiobookshelf feature.

The following are not supported:

- Podcasts
- OpenID Connect (OIDC) login
- Audio transcoding or HLS streaming
- RSS feeds
- Sending books to e-readers by email
- Socket.io events (the web interface uses Server-Sent Events instead)

The main focus is audiobook library management, direct streaming, and compatibility with supported Audiobookshelf clients.

## Data and backups

Everything Audivault knows lives in two places:

- **The database** (`abstream.db` in the config folder, `docker-data/config` with Docker): users, libraries, listening progress, bookmarks, collections, playlists, and settings. Back this folder up to keep your listening history.
- **The metadata folder** (`docker-data/metadata` with Docker): cached covers and author photos. It can be rebuilt.

Book metadata you edit is also written to `metadata.json` in each book's folder (unless that setting is off), so a fresh install can read it back.

Stop the server (or container) before copying the database, or copy `abstream.db` together with its `-wal` and `-shm` files.

## Troubleshooting

- **Port already in use:** another program, often Audiobookshelf, is using `13378`. Set `HOST_PORT` (Docker) or `ABSTREAM_PORT` to a free port.
- **New books aren't picked up automatically:** check **Settings → Activity → Watched folders**. On Docker Desktop and network shares, use polling (`WATCH_POLLING=1` or the library's *Detection method*). Polling checks every 30 seconds, and a scan starts once no change has been seen for a full polling interval (15 seconds with file system events).
- **Auto-import skipped a book:** it is on the Import page marked *Needs review*, with the reason. Fix the metadata there and import it by hand.
- **A folder shows as missing:** books are marked missing when their folder disappears. If it moved, the next scan finds it again; otherwise use **⋯ → Remove missing items** in the library settings.
- **Library folder not found:** in Docker, use the container path (`/audiobooks`), not the host path.
- **Chapters look wrong:** try **Use .cue files for chapters** if the folder has a cue sheet, or look up chapters from Audible in **Edit → Chapters**.

## Testing

Audivault includes three end-to-end tests. Each one starts from generated test data and talks to a running server over HTTP.


| Test                       | What it covers                                                                                                                                                      |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `tests/api_test.py`        | The Audiobookshelf API as AudioBooth uses it; responses are checked against AudioBooth's Swift data models (missing fields, timestamp formats, response structures) |
| `tests/live_test.py`       | Cue sheet chapters, folder watching with partial scans, and the live event stream                                                                                   |
| `tests/dup_test.py`        | Organize on import and on scan, the duplicate manager, and its recommendations                                                                                      |
| `tests/trash_test.py`      | Trash listing, retention-based emptying, restore, delete, and the `ABSTREAM_TRASH_DAYS` lock; run it right after `dup_test.py`                                      |
| `tests/lock_test.py`       | Locked libraries and folders: refused deletes, duplicates, trash, covers, and organize                                                                              |
| `tests/autoimport_test.py` | The default import operation and automatic import (needs internet access; start the server with `ABSTREAM_IMPORT_SETTLE=3`)                                         |


Generate test data:

```bash
.venv/Scripts/python tests/make_testdata.py ./testdata
```

Start a separate Audivault instance on port `13399` with an empty data folder and a short watch delay. On Linux or macOS:

```bash
ABSTREAM_PORT=13399 ABSTREAM_DATA_DIR=./testdata-server ABSTREAM_WATCH_DELAY=2 .venv/bin/python run.py
```

Then run `api_test.py` first, followed by **one** of the others (each expects the state `api_test.py` leaves behind, so regenerate the test data and use a fresh server between them):

```bash
.venv/Scripts/python tests/api_test.py ./testdata
```

```bash
.venv/Scripts/python tests/live_test.py ./testdata
```

```bash
.venv/Scripts/python tests/dup_test.py ./testdata
```

To test a server running in Docker, set `ABSTREAM_TEST_URL` to its address and `ABSTREAM_TEST_SERVER_ROOT` to the container path where the test data is mounted.

`tests/compat_test.py` and `tests/boundary_test.py` do not need that server. They check token refresh, range requests, progress, bookmarks, collection and playlist membership, opening a database that is missing a later column, and that `app/core` does not import the other packages. Connecting a third-party Audiobookshelf app (such as AudioBooth) and playing a book is a manual check.

---

## Releases

Each push is tagged `vMAJOR.MINOR.PATCH`, matching `__version__` in `app/__init__.py`. The [releases page](https://github.com/Ryahn/Audivault/releases) lists those tags. The notes on a release are the commit messages since the previous tag.

---

## About the project

Audivault is intended for people who want a simple, self-hosted audiobook server without giving up organized libraries or compatibility with existing audiobook apps.

It combines audiobook streaming, metadata management, a structured import workflow, and duplicate clean-up in one application, while keeping your files and library under your control.