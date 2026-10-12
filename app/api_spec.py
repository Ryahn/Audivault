"""OpenAPI document for /api-docs and /openapi.json.

FastAPI already lists every route. This module fills in the parts the
signatures cannot: which routes need a bearer token, how requests are
grouped, and the JSON fields each handler actually reads.
"""

from fastapi import FastAPI

from .catalog import library
from .core.auth import DEFAULT_PERMISSIONS

DESCRIPTION = """
Audivault implements the Audiobookshelf API used by audiobook apps, and adds its own routes under `/api/abstream/` for import, organize, duplicates, trash, and live updates.

## Authentication

Click **Authorize** and paste a token. Send it on every request as `Authorization: Bearer <token>`.

- **Access token.** `POST /login` with the header `x-return-tokens: true`. The token is `user.accessToken` in the response.
- **API key.** An admin creates one under **Settings → API keys**. The secret is returned once, as `apiKey.apiKey`, and is sent the same way as an access token.
- **Refresh.** `POST /auth/refresh` with the header `x-refresh-token`.

The web app signs in with httpOnly cookies and sends `X-CSRF-Token` on requests that change data. API clients send the bearer token and no CSRF header.

These routes accept anonymous calls: `GET /status`, `GET /ping`, `GET /healthcheck`, `POST /init`, `POST /login`, `POST /auth/refresh`, `POST /logout`, cover and author images, and `GET /public/session/{id}/track/{n}` (the session id is the credential).

## Errors

Failures on API routes are plain text, with an HTTP status of 400, 401, 403, 404, 423, 429, or 500. **423** means a library or folder lock refused the change.

## Specification

The machine-readable document is [`/openapi.json`](/openapi.json). Request bodies list the fields the server reads. Extra fields are ignored.
""".strip()

TAGS = [
    {"name": "Server", "description": "Status and health. No authentication."},
    {"name": "Authentication", "description": "First-run setup, login, refresh, logout, and session check."},
    {"name": "Libraries", "description": "Library folders, items, filters, search, and shelves."},
    {"name": "Items", "description": "One book: metadata, chapters, cover, files, and playback."},
    {"name": "Search", "description": "Metadata providers (Audible, Google Books, iTunes, Open Library, AudiobookCovers)."},
    {"name": "Authors and series", "description": "Author and series records, including author photos."},
    {"name": "Collections", "description": "Named sets of books inside one library."},
    {"name": "Playlists", "description": "Per-user playlists."},
    {"name": "Account", "description": "The signed-in user: profile, progress, bookmarks, and listening history."},
    {"name": "Playback", "description": "Play sessions, progress sync, and the public audio stream."},
    {"name": "Administration", "description": "Users, API keys, and server settings. Admin only."},
    {"name": "Audivault", "description": "Import, organize, duplicates, trash, folder watching, plugins, and live updates. Most of these require an admin. Template preview needs any signed-in user, and excluding an audio file needs the update permission."},
]

_PUBLIC = {
    ("get", "/status"),
    ("get", "/ping"),
    ("get", "/healthcheck"),
    ("post", "/init"),
    ("post", "/login"),
    ("post", "/auth/refresh"),
    ("post", "/logout"),
    ("get", "/api/items/{item_id}/cover"),
    ("get", "/api/authors/{author_id}/image"),
    ("get", "/api/narrators/{narrator_id}/image"),
    ("get", "/public/session/{session_id}/track/{index}"),
    ("get", "/api/abstream/events"),
}

_FLAG = "1, true, or yes. Any other value is false."

_PARAM_DOCS = {
    "expanded": "1, true, or yes includes listening progress. The item itself is always returned in full.",
    "minified": "1, true, or yes returns collapsed book objects. Any other value includes full media metadata.",
    "hard": f"{_FLAG} When set, the book files are deleted from disk. Otherwise the item is removed from the library and the files stay.",
    "force": f"{_FLAG} When set, every book is re-read. Otherwise only files that changed are re-read.",
    "collapseseries": f"{_FLAG} When set, each series becomes one row (the first unread book). Ignored while filtering by series.",
    "desc": f"{_FLAG} When set, the sort is descending.",
    "analyze": f"{_FLAG} When set, each group includes a keep/discard recommendation.",
    "all": f"{_FLAG} When set, books whose folder already matches the template are included.",
    "filter": "Audiobookshelf filter, `group.value`. `value` may be URL-encoded; a base64 value is decoded when it is valid text. Groups: authors, series (or `no-series`), genres, tags, narrators, publishers, languages, publishedDecades, progress (`finished`, `in-progress`, `not-started`, `not-finished`), missing (`asin`, `isbn`, `subtitle`, `authors`, `publishedYear`, `series`, `genres`, `tags`, `narrators`, `publisher`, `language`, `cover`, `description`), tracks (`single`, `multi`), ebooks (`ebook`, `no-ebook`), issues, abridged, explicit.",
    "q": "Search text.",
    "limit": "Page size. 0 returns every row.",
    "page": "Zero-based page index.",
    "width": "Pixel width of a JPEG thumbnail. Omit it to receive the stored image.",
    "ticket": "One-time ticket from POST /api/abstream/events/ticket. EventSource cannot send an Authorization header.",
}

_INCLUDE_DOCS = {
    "/api/items/{item_id}": "Comma-separated. `progress` adds listening progress (also implied by expanded). `authors` expands author objects. `rssfeed` and `share` are accepted and returned as null.",
    "/api/libraries/{library_id}": "`filterdata` wraps the library with filter lists, the missing-item count, and the user's playlist count.",
    "/api/libraries/{library_id}/items": "Comma-separated. `progress` adds listening progress. `rssfeed` and `share` are accepted and returned as null.",
    "/api/libraries/{library_id}/series": "Echoed back. Book detail is controlled by minified.",
    "/api/libraries/{library_id}/personalized": "Accepted for client compatibility. Continue shelves include a progress timestamp.",
    "/api/authors/{author_id}": "`items` adds the author's books. `series` adds those books grouped by series.",
    "/api/series/{series_id}": "`progress` adds finished-book ids. `rssfeed` is accepted and returned as null.",
}

_SORT_DOCS = {
    "/api/libraries/{library_id}/items": "Default `media.metadata.title`. Also `media.metadata.authorName`, `media.metadata.authorNameLF`, `media.metadata.publishedYear`, `addedAt`, `updatedAt`, `size`, `media.duration`, `media.numTracks`, `birthtimeMs`, `mtimeMs`, `progress`, `progress.finishedAt`, `progress.createdAt`, `random`, and `sequence` (when filtering one series).",
    "/api/libraries/{library_id}/series": "`name` (default), `numBooks`, `addedAt`, `lastBookAdded`, `lastBookUpdated`, `totalDuration`, or `random`.",
    "/api/libraries/{library_id}/authors": "`name` (default), `lastFirst`, `numBooks`, `addedAt`, or `updatedAt`.",
}

_SETTING_DOCS = {
    "namingTemplate": "Folder pattern. Tokens: {author} {series} {sequence} {title} {subtitle} {year} {narrator} {asin}. Square brackets mark a group that is dropped when any token inside it is empty.",
    "importFolder": "Server path scanned by the Import page. In Docker this is the container path, such as /import.",
    "importMode": "Default import operation.",
    "autoImport": "Import new books from the import folder when the match is confident.",
    "organizeOnImport": "Place imports with the naming template. When false, they keep the name they had in the import folder.",
    "organizeOnScan": "After a scan, rename folders that do not match the template.",
    "renameAudioFiles": "When organizing, rename audio files to the book title.",
    "writeMetadataJson": "Write edits to metadata.json in the book folder.",
    "storeCoverWithItem": "Keep a copy of the cover in the book folder.",
    "preferFolderMetadata": "Folder names win over embedded tags.",
    "useCueChapters": "A .cue file in the book folder supplies the chapters.",
    "metadataProvider": "Provider used by match and auto-import.",
    "audibleRegion": "Audible store region, such as us.",
    "scanIntervalMinutes": "Repeat a scan on this interval. 0 disables the timer.",
    "watchMode": "How the folder watcher detects changes.",
    "watchImportFolder": "Watch the import folder for new books.",
    "locked": "The library is a source of truth: files in it are never deleted.",
    "markAsFinishedTimeRemaining": "Seconds left at which playback counts as finished.",
    "markAsFinishedPercentComplete": "Fraction complete (0–1) at which playback counts as finished. Null uses the time remaining.",
    "hideSingleBookSeries": "Series shelves hide series that contain one book.",
    "metadataPrecedence": "Metadata source order, lowest priority first.",
}


def _str(description: str | None = None, **extra) -> dict:
    schema = {"type": "string", **extra}
    if description:
        schema["description"] = description
    return schema


def _num(description: str | None = None, *, integer: bool = False) -> dict:
    schema = {"type": "integer" if integer else "number"}
    if description:
        schema["description"] = description
    return schema


def _bool(description: str | None = None) -> dict:
    schema = {"type": "boolean"}
    if description:
        schema["description"] = description
    return schema


def _obj(properties: dict, required: list[str] | None = None, description: str | None = None) -> dict:
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    if description:
        schema["description"] = description
    return schema


def _arr(items: dict, description: str | None = None) -> dict:
    schema = {"type": "array", "items": items}
    if description:
        schema["description"] = description
    return schema


def _author_name() -> dict:
    return _obj({"name": _str()}, required=["name"])


def _series_ref() -> dict:
    return _obj({"name": _str(), "sequence": _str("Series position, such as 1 or 1.5.")}, required=["name"])


def _media_metadata() -> dict:
    return _obj({
        "title": _str(),
        "subtitle": _str(),
        "authors": _arr(_author_name(), "Author names. A plain string is also accepted in place of {name}."),
        "series": _arr(_series_ref()),
        "narrators": _arr(_str()),
        "genres": _arr(_str()),
        "publisher": _str(),
        "publishedYear": _str(),
        "publishedDate": _str(),
        "description": _str(),
        "isbn": _str(),
        "asin": _str(),
        "language": _str(),
        "explicit": _bool(),
        "abridged": _bool(),
    })


def _media_update() -> dict:
    return _obj({
        "metadata": _media_metadata(),
        "tags": _arr(_str()),
        "chapters": _arr(_obj({
            "start": _num("Start time in seconds."),
            "end": _num("End time in seconds."),
            "title": _str(),
        }, required=["start"])),
    }, description="Audiobookshelf media update. Only the fields you send are changed.")


def _chapter() -> dict:
    return _obj({
        "start": _num("Start time in seconds."),
        "end": _num("End time in seconds."),
        "title": _str(),
    }, required=["start"])


def _progress() -> dict:
    return _obj({
        "currentTime": _num("Playback position in seconds."),
        "duration": _num("Length in seconds."),
        "progress": _num("Fraction complete, from 0 to 1. Computed from currentTime when omitted."),
        "isFinished": _bool(),
        "ebookLocation": _str(),
        "ebookProgress": _num("Ebook fraction complete, from 0 to 1."),
        "hideFromContinueListening": _bool(),
        "lastUpdate": _num("Client timestamp in milliseconds.", integer=True),
        "startedAt": _num("Used when this is the first progress row.", integer=True),
        "libraryItemId": _str("Required on the batch route."),
    })


def _device() -> dict:
    return _obj({
        "deviceId": _str(), "clientName": _str(), "clientVersion": _str(), "manufacturer": _str(),
        "model": _str(), "osName": _str(), "osVersion": _str(), "sdkVersion": _str(),
        "browserName": _str(), "deviceName": _str(),
    })


def _local_session() -> dict:
    return _obj({
        "id": _str("Client-generated session id."),
        "libraryItemId": _str(),
        "currentTime": _num("Position in seconds."),
        "duration": _num("Length in seconds."),
        "timeListening": _num("Seconds listened in this session."),
        "startTime": _num("Position where playback started."),
        "startedAt": _num("Start time in milliseconds.", integer=True),
        "updatedAt": _num("Last update in milliseconds.", integer=True),
        "displayTitle": _str(),
        "displayAuthor": _str(),
        "playMethod": _num(integer=True),
        "mediaPlayer": _str(),
        "deviceInfo": _device(),
    }, required=["id", "libraryItemId"])


def _library_settings() -> dict:
    properties = {}
    for key, default in library.DEFAULT_LIBRARY_SETTINGS.items():
        description = _SETTING_DOCS.get(key)
        if key == "importMode":
            properties[key] = _str(description, enum=["move", "copy"])
        elif key == "watchMode":
            properties[key] = _str(description, enum=["auto", "native", "polling"])
        elif key == "markAsFinishedPercentComplete":
            properties[key] = {"anyOf": [{"type": "number"}, {"type": "null"}], "description": description}
        elif key == "autoScanCronExpression":
            properties[key] = {"anyOf": [{"type": "string"}, {"type": "null"}], "description": "Cron expression. Null disables it. The in-app timer uses scanIntervalMinutes."}
        elif isinstance(default, bool):
            properties[key] = _bool(description)
        elif isinstance(default, int):
            properties[key] = _num(description, integer=True)
        elif isinstance(default, str):
            properties[key] = _str(description)
        elif isinstance(default, list):
            properties[key] = _arr(_str(), description)
        else:
            properties[key] = _str(description)
    return _obj(properties, description="Unknown keys are ignored. On update, omitted keys keep their current value.")


def _folder() -> dict:
    return _obj({
        "fullPath": _str("Absolute path on the server. In Docker this is the container path, such as /audiobooks."),
        "locked": _bool("When true, files in this folder are never deleted."),
    }, required=["fullPath"])


def _permissions() -> dict:
    return _obj({key: _bool() for key in DEFAULT_PERMISSIONS}, description="Omitted keys keep the default for the user's type.")


def _import_metadata() -> dict:
    return _obj({
        "title": _str(),
        "subtitle": _str(),
        "authors": {"description": "A list of names, or one string such as 'Author One, Author Two'.", "oneOf": [_arr(_str()), _str()]},
        "series": _str("Series name."),
        "sequence": _str(),
        "narrators": _arr(_str()),
        "publishedYear": _str(),
        "asin": _str(),
        "isbn": _str(),
        "description": _str(),
        "publisher": _str(),
        "genres": _arr(_str()),
        "language": _str(),
    })


def _user_fields() -> dict:
    return {
        "username": _str(),
        "password": _str("At least 4 characters."),
        "type": _str(enum=["admin", "user", "guest"]),
        "permissions": _permissions(),
        "librariesAccessible": _arr(_str(), "Library ids. Used when accessAllLibraries is false."),
    }


def _login() -> dict:
    return _obj({"username": _str(), "password": _str()}, required=["username", "password"])


def _json_body(schema: dict, *, required: bool = True, description: str | None = None, form: bool = False) -> dict:
    content = {"application/json": {"schema": schema}}
    if form:
        content["application/x-www-form-urlencoded"] = {"schema": schema}
    body = {"required": required, "content": content}
    if description:
        body["description"] = description
    return body


# (method, path) -> requestBody. Paths match the OpenAPI templates.
def _bodies() -> dict[tuple[str, str], dict]:
    media = _media_update()
    progress = _progress()
    playlist_item = {"oneOf": [_str(), _obj({"libraryItemId": _str()}, required=["libraryItemId"])]}
    return {
        ("post", "/init"): _json_body(_obj({
            "username": _str(),
            "password": _str(),
            "newRoot": _obj({"username": _str(), "password": _str()}, required=["username", "password"],
                            description="Audiobookshelf wraps the root user this way. A flat username and password is also accepted."),
        }, description="Send either newRoot or a flat username and password.")),
        ("post", "/login"): _json_body(_login(), form=True, description="JSON or a form body. Send header x-return-tokens: true to receive tokens in the JSON body."),
        ("post", "/api/libraries"): _json_body(_obj({
            "name": _str(),
            "folders": _arr(_folder()),
            "settings": _library_settings(),
            "icon": _str("Icon name. Defaults to audiobookshelf."),
        }, required=["name"])),
        ("patch", "/api/libraries/{library_id}"): _json_body(_obj({
            "name": _str(),
            "folders": _arr(_folder(), "Replaces the folder list. Each fullPath must be an existing directory."),
            "settings": _library_settings(),
            "displayOrder": _num(integer=True),
            "icon": _str(),
        })),
        ("post", "/api/libraries/order"): _json_body(_arr(_obj({"id": _str(), "newOrder": _num(integer=True)}, required=["id"]))),
        ("patch", "/api/items/{item_id}/media"): _json_body(media),
        ("post", "/api/items/{item_id}/chapters"): _json_body(_obj({"chapters": _arr(_chapter())}, required=["chapters"])),
        ("post", "/api/items/{item_id}/match"): _json_body(_obj({
            "provider": _str("Provider id from GET /api/search/providers. Defaults to the library's metadata provider."),
            "title": _str(),
            "author": _str(),
            "asin": _str(),
            "isbn": _str(),
            "result": _obj({}, description="A hit from GET /api/search/books. When set, that hit is applied and no search is run."),
            "overrideDefaults": _bool("Replace existing metadata. Defaults to true when result is sent, otherwise false."),
            "updateCover": _bool("Download the matched cover. Used when result is sent. Defaults to true."),
        }), required=False),
        ("post", "/api/items/{item_id}/cover"): {
            "required": True,
            "description": "Upload a JPEG, PNG, WEBP, or GIF (at most 8 MB) as the form file `cover`, or send JSON with a url to download.",
            "content": {
                "multipart/form-data": {"schema": _obj({"cover": {"type": "string", "format": "binary"}}, required=["cover"])},
                "application/json": {"schema": _obj({"url": _str()}, required=["url"])},
            },
        },
        ("patch", "/api/items/{item_id}/cover"): _json_body(_obj({
            "cover": _str("Path of an image that already belongs to this book."),
        }, required=["cover"])),
        ("post", "/api/items/{item_id}/play"): _json_body(_obj({
            "mediaPlayer": _str(),
            "deviceInfo": _device(),
        }), required=False),
        ("post", "/api/items/{item_id}/play/{episode_id}"): _json_body(_obj({
            "mediaPlayer": _str(),
            "deviceInfo": _device(),
        }), required=False, description="episode_id is accepted and ignored. Podcasts are not supported."),
        ("post", "/api/items/batch/get"): _json_body(_obj({"libraryItemIds": _arr(_str())}, required=["libraryItemIds"])),
        ("post", "/api/items/batch/update"): _json_body(_arr(_obj({
            "id": _str("Library item id."),
            "mediaPayload": media,
        }, required=["id"]))),
        ("post", "/api/items/batch/delete"): _json_body(_obj({"libraryItemIds": _arr(_str())}, required=["libraryItemIds"])),
        ("post", "/api/items/batch/quickmatch"): _json_body(_obj({
            "libraryItemIds": _arr(_str()),
            "options": _obj({
                "provider": _str(),
                "overrideDefaults": _bool("When true, matched metadata replaces what the book already has."),
            }),
        }, required=["libraryItemIds"])),
        ("patch", "/api/authors/{author_id}"): _json_body(_obj({
            "name": _str("Renaming onto an author that already exists merges the two."),
            "description": _str(),
            "asin": _str(),
            "imagePath": _str("Send an empty string to clear the photo."),
        })),
        ("post", "/api/authors/{author_id}/match"): _json_body(_obj({
            "region": _str("Audible region. Defaults to us."),
            "asin": _str(),
            "q": _str("Name to search, when it differs from the stored name."),
        }), required=False),
        ("post", "/api/authors/{author_id}/image"): _json_body(_obj({"url": _str("http or https URL of the photo.")}, required=["url"])),
        ("patch", "/api/series/{series_id}"): _json_body(_obj({"name": _str(), "description": _str()})),
        ("post", "/api/collections"): _json_body(_obj({
            "libraryId": _str(),
            "name": _str(),
            "description": _str(),
            "books": _arr(_str(), "Library item ids, in order."),
        }, required=["libraryId", "name"])),
        ("patch", "/api/collections/{cid}"): _json_body(_obj({
            "name": _str(),
            "description": _str(),
            "books": _arr(_str(), "Replaces the book list when sent."),
        })),
        ("post", "/api/collections/{cid}/book"): _json_body(_obj({"id": _str("Library item id.")}, required=["id"])),
        ("post", "/api/collections/{cid}/batch/add"): _json_body(_obj({"books": _arr(_str())}, required=["books"])),
        ("post", "/api/collections/{cid}/batch/remove"): _json_body(_obj({"books": _arr(_str())}, required=["books"])),
        ("post", "/api/playlists"): _json_body(_obj({
            "libraryId": _str(),
            "name": _str(),
            "description": _str(),
            "items": _arr(playlist_item),
        }, required=["libraryId", "name"])),
        ("patch", "/api/playlists/{pid}"): _json_body(_obj({
            "name": _str(),
            "description": _str(),
            "items": _arr(playlist_item, "Replaces the playlist when sent."),
        })),
        ("post", "/api/playlists/{pid}/item"): _json_body(_obj({"libraryItemId": _str()}, required=["libraryItemId"])),
        ("post", "/api/playlists/{pid}/batch/add"): _json_body(_obj({"items": _arr(playlist_item)}, required=["items"])),
        ("post", "/api/playlists/{pid}/batch/remove"): _json_body(_obj({"items": _arr(playlist_item)}, required=["items"])),
        ("patch", "/api/me/password"): _json_body(_obj({
            "password": _str("Current password."),
            "newPassword": _str("At least 4 characters. Other sessions are signed out."),
        }, required=["password", "newPassword"])),
        ("patch", "/api/me/progress/batch/update"): _json_body(_arr(progress)),
        ("patch", "/api/me/progress/{item_id}"): _json_body(progress),
        ("patch", "/api/me/progress/{item_id}/{episode_id}"): _json_body(progress, description="episode_id is accepted and ignored."),
        ("post", "/api/me/item/{item_id}/bookmark"): _json_body(_obj({
            "time": _num("Position in seconds."),
            "title": _str(),
        }, required=["time"])),
        ("patch", "/api/me/item/{item_id}/bookmark"): _json_body(_obj({
            "time": _num("Position in seconds. Matches a bookmark within half a second."),
            "title": _str(),
        }, required=["time"])),
        ("post", "/api/session/{session_id}/sync"): _json_body(_obj({
            "currentTime": _num("Position in seconds."),
            "timeListened": _num("Seconds listened since the previous sync. Values outside 0–6 hours are ignored."),
            "duration": _num("Length in seconds."),
        }), required=False),
        ("post", "/api/session/{session_id}/close"): _json_body(_obj({
            "currentTime": _num("Position in seconds."),
            "timeListened": _num("Seconds listened since the previous sync."),
            "duration": _num("Length in seconds."),
        }), required=False, description="Optional final sync, then the session is closed."),
        ("post", "/api/session/local"): _json_body(_local_session()),
        ("post", "/api/session/local-all"): _json_body(_obj({"sessions": _arr(_local_session())}, required=["sessions"])),
        ("post", "/api/users"): _json_body(_obj(_user_fields(), required=["username", "password"])),
        ("patch", "/api/users/{user_id}"): _json_body(_obj(_user_fields() | {"isActive": _bool()})),
        ("post", "/api/api-keys"): _json_body(_obj({
            "name": _str(),
            "userId": _str("User the key acts as. Defaults to the admin creating it."),
            "expiresIn": _num("Lifetime in seconds. Omit it for a key that does not expire.", integer=True),
        }, required=["name"]), description="The response field apiKey.apiKey is the secret. It is shown only this once."),
        ("patch", "/api/settings"): _json_body(_obj({
            "sortingIgnorePrefix": _bool("Ignore a leading 'the' or 'a' when sorting titles."),
        })),
        ("post", "/api/abstream/duplicates/resolve-batch"): _json_body(_obj({
            "plans": _arr(_obj({
                "keepId": _str(),
                "discardIds": _arr(_str()),
            }, required=["keepId", "discardIds"])),
            "mode": _str("trash (default) or delete.", enum=["trash", "delete"]),
        }, required=["plans"])),
        ("post", "/api/abstream/duplicates/ignore"): _json_body(_obj({"itemIds": _arr(_str())}, required=["itemIds"])),
        ("post", "/api/abstream/duplicates/resolve"): _json_body(_obj({
            "keepId": _str(),
            "discards": _arr(_obj({"id": _str()}, required=["id"])),
            "mode": _str("trash (default) or delete.", enum=["trash", "delete"]),
        }, required=["keepId"])),
        ("patch", "/api/abstream/trash/settings"): _json_body(_obj({
            "retentionDays": _num("Days before discarded files are deleted. 0 keeps them until you empty the trash.", integer=True),
        }, required=["retentionDays"])),
        ("post", "/api/abstream/trash/restore"): _json_body(_obj({
            "folderId": _str(),
            "batch": _str(),
            "relPath": _str("Set to restore one file. Omit it to restore the whole batch."),
        }, required=["folderId", "batch"])),
        ("post", "/api/abstream/template-preview"): _json_body(_obj({
            "template": _str("Naming template. Omit it to preview the default."),
        }), required=False),
        ("post", "/api/abstream/libraries/{library_id}/import"): _json_body(_obj({
            "relPath": _str("Path of the book relative to the library's import folder."),
            "metadata": _import_metadata(),
            "folderId": _str("Library folder to import into. Defaults to the first folder."),
            "mode": _str("move or copy. Omit it to use the library's import mode.", enum=["move", "copy"]),
            "coverUrl": _str(),
        }, required=["relPath"])),
        ("post", "/api/abstream/libraries/{library_id}/organize"): _json_body(_obj({
            "itemIds": _arr(_str(), "Books to rename. Omit it to rename every book that is ready."),
            "template": _str("Naming template. Omit it to use the library's template."),
        }), required=False),
        ("post", "/api/abstream/items/{item_id}/audio-files"): _json_body(_obj({
            "exclude": _arr(_str(), "Inode values of audio files to leave out of playback. At least one file must stay included."),
        }, required=["exclude"])),
    }


_NOTES = {
    ("get", "/healthcheck"): "The body is the plain text OK.",
    ("post", "/login"): "With x-return-tokens: true, user.accessToken and user.refreshToken are set and the access token is not stored in a cookie. Without that header, both token fields are null and the session is stored in httpOnly cookies.",
    ("post", "/auth/refresh"): "Send the refresh token in the x-refresh-token header (API clients) or in the refresh_token cookie (the web app). The header response includes new tokens. The cookie response rotates the cookies and leaves the token fields null.",
    ("get", "/api/libraries/{library_id}/recent-episodes"): "Podcasts are not supported. This always returns an empty list.",
    ("get", "/api/items/{item_id}/cover"): "No authentication, matching Audiobookshelf. Image tags in apps do not send a token.",
    ("get", "/api/authors/{author_id}/image"): "No authentication.",
    ("get", "/api/narrators/{narrator_id}/image"): "Narrator photos are not stored. This responds 404.",
    ("get", "/api/items/{item_id}/download"): "Authorize with a bearer token, or with the dl query value from GET /api/items/{id}/download-url. The body is a zip of the book folder.",
    ("get", "/api/items/{item_id}/file/{ino}/download"): "Authorize with a bearer token, or with the dl query value from GET /api/items/{id}/download-url.",
    ("get", "/api/items/{item_id}/file/{ino}"): "Audio bytes for one file. Supports HTTP Range. The content type is the file's mime type.",
    ("get", "/public/session/{session_id}/track/{index}"): "No Authorization header. The session id is the credential, and it works for 24 hours after the session's last update.",
    ("get", "/api/abstream/events"): "A Server-Sent Events stream. Authorize with the ticket query parameter, not a bearer token.",
    ("post", "/api/api-keys"): "The response field apiKey.apiKey is the secret. It is shown only this once.",
    ("delete", "/api/items/{item_id}"): "hard=1 deletes the files. Otherwise the item is removed from the library and the files stay.",
}

_SUMMARIES = {
    ("get", "/api/me"): "Current user",
    ("get", "/public/session/{session_id}/track/{index}"): "Stream a session track",
    ("get", "/api/libraries/{library_id}/recent-episodes"): "Recent episodes",
    ("post", "/init"): "Create the root user",
}

_BINARY = {
    ("get", "/api/items/{item_id}/cover"): "image/*",
    ("get", "/api/authors/{author_id}/image"): "image/*",
    ("get", "/api/items/{item_id}/file/{ino}"): "application/octet-stream",
    ("get", "/api/items/{item_id}/file/{ino}/download"): "application/octet-stream",
    ("get", "/api/items/{item_id}/ebook"): "application/octet-stream",
    ("get", "/api/items/{item_id}/ebook/{file_id}"): "application/octet-stream",
    ("get", "/api/items/{item_id}/download"): "application/zip",
    ("get", "/public/session/{session_id}/track/{index}"): "application/octet-stream",
}

_PLAIN = {
    ("get", "/healthcheck"),
    ("patch", "/api/me/progress/batch/update"),
    ("delete", "/api/me/progress/{progress_id}"),
    ("post", "/api/session/{session_id}/sync"),
    ("post", "/api/session/{session_id}/close"),
    ("post", "/api/session/local"),
    ("delete", "/api/sessions/{session_id}"),
}

_DL_PATHS = {
    ("get", "/api/items/{item_id}/download"),
    ("get", "/api/items/{item_id}/file/{ino}/download"),
}


def _tag_for(path: str) -> str:
    if path in ("/status", "/ping", "/healthcheck"):
        return "Server"
    if path in ("/init", "/login", "/logout") or path.startswith("/auth/") or path == "/api/authorize":
        return "Authentication"
    if path.startswith("/api/abstream"):
        return "Audivault"
    if path.startswith("/api/libraries"):
        return "Libraries"
    if path.startswith("/api/search"):
        return "Search"
    if path.startswith("/api/items"):
        return "Items"
    if path.startswith(("/api/authors", "/api/narrators", "/api/series")):
        return "Authors and series"
    if path.startswith("/api/collections"):
        return "Collections"
    if path.startswith("/api/playlists"):
        return "Playlists"
    if path.startswith("/api/me"):
        return "Account"
    if path.startswith(("/api/session", "/api/sessions", "/public/session")):
        return "Playback"
    if path.startswith(("/api/users", "/api/api-keys", "/api/settings")):
        return "Administration"
    return "Other"


def _document_parameters(path: str, operation: dict) -> None:
    for param in operation.get("parameters") or []:
        name = param.get("name")
        if name == "include":
            text = _INCLUDE_DOCS.get(path)
        elif name == "sort":
            text = _SORT_DOCS.get(path)
        else:
            text = _PARAM_DOCS.get(name)
        if text and "description" not in param:
            param["description"] = text
    if ("get", path) in _DL_PATHS and not any(p.get("name") == "dl" for p in operation.get("parameters") or []):
        operation.setdefault("parameters", []).append({
            "name": "dl",
            "in": "query",
            "required": False,
            "schema": {"type": "string"},
            "description": "Short-lived token from GET /api/items/{id}/download-url. Used instead of a bearer token.",
        })


def _document_response(method: str, path: str, operation: dict) -> None:
    key = (method, path)
    responses = operation.setdefault("responses", {})
    ok = responses.setdefault("200", {"description": "Success"})
    if key in _BINARY:
        ok["content"] = {_BINARY[key]: {"schema": {"type": "string", "format": "binary"}}}
    elif key in _PLAIN:
        ok["content"] = {"text/plain": {"schema": {"type": "string", "example": "OK"}}}
    elif key == ("get", "/api/abstream/events"):
        ok["description"] = "Event stream. Each event is a data: line of JSON."
        ok["content"] = {"text/event-stream": {"schema": {"type": "string"}}}
    else:
        # An empty schema renders as "string" in Swagger UI.
        for body in (ok.get("content") or {}).values():
            if body.get("schema") == {}:
                body["schema"] = {"type": "object", "additionalProperties": True}


def _header(name: str, description: str, schema: dict) -> dict:
    return {"name": name, "in": "header", "required": False, "description": description, "schema": schema}


def _document_headers(method: str, path: str, operation: dict) -> None:
    params = operation.setdefault("parameters", [])
    names = {p.get("name") for p in params}
    if method == "post" and path == "/login" and "x-return-tokens" not in names:
        params.append(_header("x-return-tokens", "Set to true to receive accessToken and refreshToken in the JSON body.", {"type": "string", "enum": ["true"]}))
    if method == "post" and path == "/auth/refresh" and "x-refresh-token" not in names:
        params.append(_header("x-refresh-token", "Refresh token. When omitted, the refresh_token cookie is used.", {"type": "string"}))


def _drop_unused_schemas(schema: dict) -> None:
    """Request bodies are inlined, so the generated Body_* models are unused."""
    used: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                used.add(ref.rsplit("/", 1)[-1])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema.get("paths"))
    schemas = schema.get("components", {}).get("schemas") or {}
    for name in list(schemas):
        if name not in used:
            del schemas[name]
    if not schemas:
        schema.get("components", {}).pop("schemas", None)


def enrich(schema: dict) -> dict:
    """Add auth, tags, parameters, and request bodies to a generated schema."""
    schema["info"]["description"] = DESCRIPTION
    schema["tags"] = TAGS
    components = schema.setdefault("components", {})
    components.setdefault("securitySchemes", {})["bearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
        "description": "An access token from POST /login, or an API key from Settings → API keys.",
    }
    schema["security"] = [{"bearerAuth": []}]
    bodies = _bodies()
    for path, methods in schema.get("paths", {}).items():
        for method, operation in methods.items():
            if method not in ("get", "post", "patch", "put", "delete"):
                continue
            key = (method, path)
            operation["tags"] = [_tag_for(path)]
            if key in _PUBLIC:
                operation["security"] = []
            if key in _SUMMARIES:
                operation["summary"] = _SUMMARIES[key]
            if key in _NOTES:
                operation["description"] = _NOTES[key]
            if key in bodies:
                operation["requestBody"] = bodies[key]
            _document_parameters(path, operation)
            _document_headers(method, path, operation)
            _document_response(method, path, operation)
    _drop_unused_schemas(schema)
    return schema


def install_openapi(app: FastAPI) -> None:
    """Serve the enriched schema from /openapi.json and the Swagger page."""

    def openapi() -> dict:
        schema = FastAPI.openapi(app)
        if schema.get("components", {}).get("securitySchemes", {}).get("bearerAuth"):
            return schema
        return enrich(schema)

    app.openapi = openapi
