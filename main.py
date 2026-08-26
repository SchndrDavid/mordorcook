"""mordorcook - a personal recipe book with a hands-free cooking mode.

The service keeps every recipe, photo and shopping-list entry in a single
SQLite database so that a phone in the kitchen and a desktop browser always
see the same data. Only transient preferences (appearance, text size,
running timers) live in the browser.
"""

from __future__ import annotations

import ipaddress
import json
import mimetypes
import os
import re
import socket
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

SERVICE_NAME = "mordorcook"

# Everything the app writes lives under this directory. In the container it
# is a bind mount, so the data survives image rebuilds.
DATA_DIR = Path(os.getenv("MORDORCOOK_DATA_DIR", "./data"))
PHOTO_DIR = DATA_DIR / "photos"
DB_PATH = Path(os.getenv("MORDORCOOK_DB_PATH", str(DATA_DIR / "mordorcook.db")))

# Uploaded and imported photos are stored as they arrive, so cap them to keep
# the volume from filling up with 40-megapixel originals.
MAX_PHOTO_BYTES = int(os.getenv("MORDORCOOK_MAX_PHOTO_BYTES", str(12 * 1024 * 1024)))

ALLOWED_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
}

HTTP_TIMEOUT = httpx.Timeout(12.0, connect=6.0)

# Wikimedia Commons refuses requests whose User-Agent carries no way to reach
# the operator, so that provider stays dark until a contact is configured.
# The other photo sources work either way.
CONTACT = os.getenv("MORDORCOOK_CONTACT", "").strip()
USER_AGENT = "mordorcook/1.0 (self-hosted recipe app%s)" % ("; " + CONTACT if CONTACT else "")

SCHEMA = """
CREATE TABLE IF NOT EXISTS recipes (
    id             TEXT PRIMARY KEY,
    title          TEXT NOT NULL,
    summary        TEXT NOT NULL DEFAULT '',
    category       TEXT NOT NULL DEFAULT '',
    cuisine        TEXT NOT NULL DEFAULT '',
    tags           TEXT NOT NULL DEFAULT '[]',
    servings       INTEGER NOT NULL DEFAULT 2,
    prep_minutes   INTEGER NOT NULL DEFAULT 0,
    cook_minutes   INTEGER NOT NULL DEFAULT 0,
    difficulty     TEXT NOT NULL DEFAULT 'easy',
    photo_id       TEXT,
    ingredients    TEXT NOT NULL DEFAULT '[]',
    steps          TEXT NOT NULL DEFAULT '[]',
    notes          TEXT NOT NULL DEFAULT '',
    source         TEXT NOT NULL DEFAULT '',
    favorite       INTEGER NOT NULL DEFAULT 0,
    cooked_count   INTEGER NOT NULL DEFAULT 0,
    last_cooked_at TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS photos (
    id          TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,
    mime        TEXT NOT NULL,
    bytes       INTEGER NOT NULL,
    source      TEXT NOT NULL DEFAULT 'upload',
    attribution TEXT NOT NULL DEFAULT '',
    link        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shopping (
    id         TEXT PRIMARY KEY,
    text       TEXT NOT NULL,
    checked    INTEGER NOT NULL DEFAULT 0,
    recipe     TEXT NOT NULL DEFAULT '',
    position   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_recipes_updated ON recipes(updated_at DESC);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    """Open a short-lived connection.

    FastAPI runs sync endpoints in a thread pool, so a connection per call is
    both simpler and safer than sharing one across threads.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_storage() -> None:
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(SCHEMA)


class Ingredient(BaseModel):
    amount: Optional[float] = None
    unit: str = ""
    item: str = ""
    note: str = ""
    group: str = ""


class Step(BaseModel):
    text: str = ""
    seconds: int = 0
    photo_id: Optional[str] = None


class RecipeIn(BaseModel):
    title: str = Field(default="Untitled recipe", max_length=200)
    summary: str = ""
    category: str = ""
    cuisine: str = ""
    tags: list[str] = Field(default_factory=list)
    servings: int = 2
    prep_minutes: int = 0
    cook_minutes: int = 0
    difficulty: str = "easy"
    photo_id: Optional[str] = None
    ingredients: list[Ingredient] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    notes: str = ""
    source: str = ""
    favorite: bool = False


class ShoppingIn(BaseModel):
    text: str
    recipe: str = ""


class ShoppingPatch(BaseModel):
    text: Optional[str] = None
    checked: Optional[bool] = None


class PhotoImportIn(BaseModel):
    url: str
    attribution: str = ""
    link: str = ""
    source: str = "web"


def row_to_recipe(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "summary": row["summary"],
        "category": row["category"],
        "cuisine": row["cuisine"],
        "tags": json.loads(row["tags"]),
        "servings": row["servings"],
        "prep_minutes": row["prep_minutes"],
        "cook_minutes": row["cook_minutes"],
        "difficulty": row["difficulty"],
        "photo_id": row["photo_id"],
        "ingredients": json.loads(row["ingredients"]),
        "steps": json.loads(row["steps"]),
        "notes": row["notes"],
        "source": row["source"],
        "favorite": bool(row["favorite"]),
        "cooked_count": row["cooked_count"],
        "last_cooked_at": row["last_cooked_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


app = FastAPI(title="mordorcook", docs_url=None, redoc_url=None)


@app.on_event("startup")
def _startup() -> None:
    init_storage()


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME}


@app.get("/api/recipes")
def list_recipes(q: str = "", tag: str = "", favorite: bool = False, sort: str = "updated") -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM recipes").fetchall()

    recipes = [row_to_recipe(r) for r in rows]

    if q:
        needle = q.casefold()

        def matches(recipe: dict[str, Any]) -> bool:
            haystack = " ".join(
                [recipe["title"], recipe["summary"], recipe["category"], recipe["cuisine"]]
                + recipe["tags"]
                + [i.get("item", "") for i in recipe["ingredients"]]
            )
            return needle in haystack.casefold()

        recipes = [r for r in recipes if matches(r)]

    if tag:
        recipes = [r for r in recipes if tag in r["tags"]]
    if favorite:
        recipes = [r for r in recipes if r["favorite"]]

    keys = {
        "updated": lambda r: r["updated_at"],
        "created": lambda r: r["created_at"],
        "title": lambda r: r["title"].casefold(),
        "cooked": lambda r: r["cooked_count"],
        "time": lambda r: r["prep_minutes"] + r["cook_minutes"],
    }
    recipes.sort(key=keys.get(sort, keys["updated"]), reverse=sort in ("updated", "created", "cooked"))
    return recipes


@app.post("/api/recipes", status_code=201)
def create_recipe(payload: RecipeIn) -> dict[str, Any]:
    rid = uuid.uuid4().hex
    ts = now_iso()
    with db() as conn:
        conn.execute(
            """INSERT INTO recipes (id, title, summary, category, cuisine, tags, servings,
                    prep_minutes, cook_minutes, difficulty, photo_id, ingredients, steps,
                    notes, source, favorite, cooked_count, last_cooked_at, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,?,?)""",
            (
                rid,
                payload.title,
                payload.summary,
                payload.category,
                payload.cuisine,
                json.dumps(payload.tags),
                payload.servings,
                payload.prep_minutes,
                payload.cook_minutes,
                payload.difficulty,
                payload.photo_id,
                json.dumps([i.model_dump() for i in payload.ingredients]),
                json.dumps([s.model_dump() for s in payload.steps]),
                payload.notes,
                payload.source,
                int(payload.favorite),
                ts,
                ts,
            ),
        )
        row = conn.execute("SELECT * FROM recipes WHERE id=?", (rid,)).fetchone()
    return row_to_recipe(row)


@app.get("/api/recipes/{recipe_id}")
def get_recipe(recipe_id: str) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Recipe not found")
    return row_to_recipe(row)


@app.put("/api/recipes/{recipe_id}")
def update_recipe(recipe_id: str, payload: RecipeIn) -> dict[str, Any]:
    with db() as conn:
        if conn.execute("SELECT id FROM recipes WHERE id=?", (recipe_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Recipe not found")
        conn.execute(
            """UPDATE recipes SET title=?, summary=?, category=?, cuisine=?, tags=?,
                   servings=?, prep_minutes=?, cook_minutes=?, difficulty=?, photo_id=?,
                   ingredients=?, steps=?, notes=?, source=?, favorite=?, updated_at=?
               WHERE id=?""",
            (
                payload.title,
                payload.summary,
                payload.category,
                payload.cuisine,
                json.dumps(payload.tags),
                payload.servings,
                payload.prep_minutes,
                payload.cook_minutes,
                payload.difficulty,
                payload.photo_id,
                json.dumps([i.model_dump() for i in payload.ingredients]),
                json.dumps([s.model_dump() for s in payload.steps]),
                payload.notes,
                payload.source,
                int(payload.favorite),
                now_iso(),
                recipe_id,
            ),
        )
        updated = conn.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone()
    return row_to_recipe(updated)


@app.delete("/api/recipes/{recipe_id}", status_code=204)
def delete_recipe(recipe_id: str) -> None:
    with db() as conn:
        if conn.execute("DELETE FROM recipes WHERE id=?", (recipe_id,)).rowcount == 0:
            raise HTTPException(status_code=404, detail="Recipe not found")


@app.post("/api/recipes/{recipe_id}/cooked")
def mark_cooked(recipe_id: str) -> dict[str, Any]:
    """Record that the recipe was cooked once more, from the cooking mode."""
    with db() as conn:
        if conn.execute("SELECT id FROM recipes WHERE id=?", (recipe_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Recipe not found")
        conn.execute(
            "UPDATE recipes SET cooked_count = cooked_count + 1, last_cooked_at=? WHERE id=?",
            (now_iso(), recipe_id),
        )
        updated = conn.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone()
    return row_to_recipe(updated)


def store_photo(data: bytes, mime: str, source: str, attribution: str, link: str) -> dict[str, Any]:
    if mime not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=415, detail=f"Unsupported image type: {mime or 'unknown'}")
    if len(data) > MAX_PHOTO_BYTES:
        raise HTTPException(status_code=413, detail="That image is larger than the size limit")

    pid = uuid.uuid4().hex
    filename = pid + ALLOWED_IMAGE_TYPES[mime]
    (PHOTO_DIR / filename).write_bytes(data)
    with db() as conn:
        conn.execute(
            """INSERT INTO photos (id, filename, mime, bytes, source, attribution, link, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (pid, filename, mime, len(data), source, attribution, link, now_iso()),
        )
    return {"id": pid, "url": f"api/photos/{pid}", "attribution": attribution, "link": link}


@app.post("/api/photos", status_code=201)
async def upload_photo(
    file: UploadFile = File(...),
    attribution: str = Form(""),
    link: str = Form(""),
) -> dict[str, Any]:
    data = await file.read(MAX_PHOTO_BYTES + 1)
    mime = (file.content_type or "").split(";")[0].strip().lower()
    return store_photo(data, mime, "upload", attribution, link)


# Registered before /api/photos/{photo_id}: routes match in the order they
# are declared, so the literal "search" path has to come first or it is
# read as a photo id.
@app.get("/api/photos/search")
async def photo_search(q: str = Query(..., min_length=2), limit: int = 24) -> dict[str, Any]:
    """Search several free photo sources at once.

    Requests are proxied here rather than made from the browser so that one
    slow or rate-limited provider cannot break the whole picker, and so the
    page needs no API keys of its own.
    """
    limit = max(1, min(limit, 40))
    per_provider = max(4, limit // 2)
    providers = (
        ("TheMealDB", search_mealdb_photos),
        ("Openverse", search_openverse),
        ("Wikimedia Commons", search_wikimedia),
    )
    results: list[dict[str, Any]] = []
    unavailable: list[str] = []

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        for name, fn in providers:
            try:
                results.extend(await fn(client, q, per_provider))
            except Exception:
                # A provider being down or rate limited is normal; the others
                # still answer, so the picker degrades instead of failing.
                unavailable.append(name)

    seen: set[str] = set()
    unique = []
    for item in results:
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        unique.append(item)

    return {"query": q, "results": unique[:limit], "unavailable": unavailable}


FRACTION_GLYPHS = {"½": 0.5, "¼": 0.25, "¾": 0.75, "⅓": 1 / 3, "⅔": 2 / 3, "⅛": 0.125, "⅜": 0.375}


def parse_measure(measure: str) -> tuple[Optional[float], str]:
    """Split a free-text measure such as "1 1/2 tbsp" into amount and unit."""
    text = measure.strip()
    if not text:
        return None, ""
    for glyph, value in FRACTION_GLYPHS.items():
        text = text.replace(glyph, " %s " % value)
    match = re.match(
        r"\s*(\d+(?:[.,]\d+)?)(?:\s*/\s*(\d+(?:[.,]\d+)?))?"
        r"(?:\s+(\d+(?:[.,]\d+)?)\s*/\s*(\d+(?:[.,]\d+)?))?",
        text,
    )
    if not match:
        return None, text
    amount = float(match.group(1).replace(",", "."))
    if match.group(2):
        amount /= float(match.group(2).replace(",", "."))
    if match.group(3) and match.group(4):
        amount += float(match.group(3).replace(",", ".")) / float(match.group(4).replace(",", "."))
    return round(amount, 3), text[match.end():].strip()


def detect_duration(text: str) -> int:
    """Find a cooking duration in a step so its timer can be pre-filled."""
    match = re.search(
        r"(\d+)\s*(?:-|to|\u2013)?\s*(\d+)?\s*(hours?|hrs?|minutes?|mins?|seconds?|secs?)\b",
        text,
        re.IGNORECASE,
    )
    if not match:
        return 0
    value = int(match.group(2) or match.group(1))
    unit = match.group(3).lower()
    if unit.startswith("h"):
        return value * 3600
    if unit.startswith("m"):
        return value * 60
    return value


def mealdb_to_recipe(meal: dict[str, Any]) -> dict[str, Any]:
    """Turn one TheMealDB record into this app's recipe shape."""
    ingredients = []
    for n in range(1, 21):
        item = (meal.get("strIngredient%d" % n) or "").strip()
        if not item:
            continue
        amount, unit = parse_measure((meal.get("strMeasure%d" % n) or "").strip())
        # A measure with no number in it ("to taste", "as required") is a note
        # about the ingredient, not a unit of it.
        note = ""
        if amount is None and unit:
            note, unit = unit, ""
        ingredients.append({"amount": amount, "unit": unit, "item": item, "note": note, "group": ""})

    raw = (meal.get("strInstructions") or "").replace("\r\n", "\n")
    chunks = [c.strip() for c in re.split(r"\n\s*\n", raw) if c.strip()]
    if len(chunks) <= 1:
        chunks = [c.strip() for c in re.split(r"\n", raw) if c.strip()]
    steps = []
    for chunk in chunks:
        text = re.sub(r"^STEP\s*\d+\s*:?\s*", "", chunk, flags=re.IGNORECASE).strip()
        if text:
            steps.append({"text": text, "seconds": detect_duration(text), "photo_id": None})

    return {
        "title": meal.get("strMeal") or "Imported recipe",
        "summary": "",
        "category": meal.get("strCategory") or "",
        "cuisine": meal.get("strArea") or "",
        "tags": [t.strip() for t in (meal.get("strTags") or "").split(",") if t.strip()],
        "servings": 4,
        "prep_minutes": 0,
        # Left for the cook to fill in. Adding up the durations detected in the
        # steps looks precise but is only ever a lower bound - it reported one
        # minute for a dish that takes half an hour - and a wrong number is
        # worse than a blank one on a draft that gets edited anyway.
        "cook_minutes": 0,
        "difficulty": "medium",
        "ingredients": ingredients,
        "steps": steps,
        "notes": "",
        "source": meal.get("strSource") or "https://www.themealdb.com/meal/" + str(meal.get("idMeal")),
        "photo_url": meal.get("strMealThumb") or "",
    }


@app.get("/api/photos/{photo_id}")
def get_photo(photo_id: str) -> FileResponse:
    with db() as conn:
        row = conn.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    path = PHOTO_DIR / row["filename"]
    if not path.exists():
        raise HTTPException(status_code=404, detail="Photo file is missing")
    # Photo bytes never change once written, so they can be cached hard.
    return FileResponse(
        path,
        media_type=row["mime"],
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.delete("/api/photos/{photo_id}", status_code=204)
def delete_photo(photo_id: str) -> None:
    with db() as conn:
        row = conn.execute("SELECT filename FROM photos WHERE id=?", (photo_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Photo not found")
        conn.execute("DELETE FROM photos WHERE id=?", (photo_id,))
    (PHOTO_DIR / row["filename"]).unlink(missing_ok=True)


def assert_public_http_url(raw: str) -> str:
    """Reject anything that is not a plain http(s) URL to a public address.

    The import endpoint fetches a URL chosen by the browser, so it must not
    become a way to probe hosts on the same network as this container.
    """
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status_code=400, detail="Only http(s) URLs can be imported")
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        raise HTTPException(status_code=400, detail="Could not resolve that host")
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
            raise HTTPException(status_code=400, detail="That address is not allowed")
    return raw


@app.post("/api/photos/import", status_code=201)
async def import_photo(payload: PhotoImportIn) -> dict[str, Any]:
    """Copy a photo found through search into local storage.

    The picture is downloaded once and kept, so a recipe never depends on a
    third-party URL that may disappear.
    """
    url = assert_public_http_url(payload.url)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        try:
            resp = await client.get(url, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
        except httpx.HTTPError:
            raise HTTPException(status_code=502, detail="Could not download that image")
    mime = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    return store_photo(resp.content, mime, payload.source, payload.attribution, payload.link)


async def search_openverse(client: httpx.AsyncClient, q: str, limit: int) -> list[dict[str, Any]]:
    resp = await client.get(
        "https://api.openverse.org/v1/images/",
        params={"q": q, "page_size": limit, "mature": "false"},
        headers={"User-Agent": USER_AGENT},
    )
    resp.raise_for_status()
    out = []
    for r in resp.json().get("results", []):
        if not r.get("url"):
            continue
        out.append(
            {
                "id": "ov-" + str(r.get("id")),
                "thumb": r.get("thumbnail") or r["url"],
                "url": r["url"],
                "title": r.get("title") or q,
                "author": r.get("creator") or "",
                "attribution": r.get("attribution") or "",
                "link": r.get("foreign_landing_url") or "",
                "provider": "Openverse",
            }
        )
    return out


async def search_wikimedia(client: httpx.AsyncClient, q: str, limit: int) -> list[dict[str, Any]]:
    resp = await client.get(
        "https://commons.wikimedia.org/w/api.php",
        params={
            "action": "query",
            "generator": "search",
            "gsrsearch": q,
            "gsrnamespace": 6,
            "gsrlimit": limit,
            "prop": "imageinfo",
            "iiprop": "url|extmetadata",
            "iiurlwidth": 800,
            "format": "json",
        },
        headers={"User-Agent": USER_AGENT},
    )
    resp.raise_for_status()
    pages = resp.json().get("query", {}).get("pages", {})
    out = []
    for page in pages.values():
        info = (page.get("imageinfo") or [{}])[0]
        if not info.get("thumburl"):
            continue
        meta = info.get("extmetadata") or {}
        artist = re.sub(r"<[^>]+>", "", meta.get("Artist", {}).get("value") or "").strip()
        licence = (meta.get("LicenseShortName", {}).get("value") or "").strip()
        title = page.get("title", "").replace("File:", "", 1)
        credit = ", ".join(x for x in [artist, licence] if x)
        out.append(
            {
                "id": "wm-" + str(page.get("pageid")),
                "thumb": info["thumburl"],
                "url": info.get("url") or info["thumburl"],
                "title": title,
                "author": artist,
                "attribution": f"{title} ({credit})" if credit else title,
                "link": info.get("descriptionurl") or "",
                "provider": "Wikimedia Commons",
            }
        )
    return out


async def search_mealdb_photos(client: httpx.AsyncClient, q: str, limit: int) -> list[dict[str, Any]]:
    resp = await client.get(
        "https://www.themealdb.com/api/json/v1/1/search.php",
        params={"s": q},
        headers={"User-Agent": USER_AGENT},
    )
    resp.raise_for_status()
    out = []
    for meal in (resp.json().get("meals") or [])[:limit]:
        thumb = meal.get("strMealThumb")
        if not thumb:
            continue
        out.append(
            {
                "id": "md-" + str(meal.get("idMeal")),
                "thumb": thumb + "/preview",
                "url": thumb,
                "title": meal.get("strMeal") or q,
                "author": "TheMealDB",
                "attribution": f"{meal.get('strMeal')} via TheMealDB",
                "link": "https://www.themealdb.com/meal/" + str(meal.get("idMeal")),
                "provider": "TheMealDB",
            }
        )
    return out


@app.get("/api/import/search")
async def import_search(q: str = Query(..., min_length=2)) -> dict[str, Any]:
    """Look up recipes in TheMealDB so a new one can start from a draft."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        try:
            resp = await client.get(
                "https://www.themealdb.com/api/json/v1/1/search.php",
                params={"s": q},
                headers={"User-Agent": USER_AGENT},
            )
            resp.raise_for_status()
        except httpx.HTTPError:
            raise HTTPException(status_code=502, detail="Recipe search is unavailable right now")
    return {
        "results": [
            {
                "id": m.get("idMeal"),
                "title": m.get("strMeal"),
                "category": m.get("strCategory") or "",
                "cuisine": m.get("strArea") or "",
                "thumb": (m.get("strMealThumb") or "") + "/preview",
            }
            for m in (resp.json().get("meals") or [])
        ]
    }


# --------------------------------------------------------------------------
# Importing a recipe from any page that publishes structured data
# --------------------------------------------------------------------------

ISO_DURATION = re.compile(
    r"P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?",
    re.IGNORECASE,
)


def iso_duration_minutes(value: Any) -> int:
    """Turn an ISO 8601 duration such as PT1H30M into whole minutes."""
    if not isinstance(value, str):
        return 0
    match = ISO_DURATION.fullmatch(value.strip())
    if not match:
        return 0
    parts = {k: int(v) for k, v in match.groupdict(default="0").items()}
    return parts["days"] * 1440 + parts["hours"] * 60 + parts["minutes"] + parts["seconds"] // 60


def strip_tags(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = re.sub(r"<[^>]+>", " ", value)
    for entity, char in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                         ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " ")):
        text = text.replace(entity, char)
    text = re.sub(r"\s+", " ", text).strip()
    # Removing a tag leaves a space behind, including in front of punctuation.
    return re.sub(r"\s+([.,;:!?])", r"\1", text)


def first_text(value: Any) -> str:
    """Schema.org fields are strings, lists or nested objects, indifferently."""
    if isinstance(value, str):
        return strip_tags(value)
    if isinstance(value, list):
        for item in value:
            text = first_text(item)
            if text:
                return text
        return ""
    if isinstance(value, dict):
        for key in ("name", "text", "url", "@id"):
            if key in value:
                return first_text(value[key])
    return ""


def flatten_instructions(value: Any) -> list[str]:
    """Collect step texts from any of the shapes recipe sites publish."""
    if isinstance(value, str):
        # Split on the markup first: collapsing whitespace early would destroy
        # the very breaks that separate one step from the next.
        marked = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</div>", "\n", value)
        chunks = [c for c in (strip_tags(part) for part in re.split(r"\n+", marked)) if c]
        if len(chunks) > 1:
            return chunks
        return [c for c in (strip_tags(part)
                for part in re.split(r"\n\s*\n|(?<=[.!?])\s{2,}", value)) if c]
    if isinstance(value, dict):
        kind = str(value.get("@type", ""))
        if kind.endswith("HowToSection"):
            return flatten_instructions(value.get("itemListElement") or value.get("steps"))
        text = strip_tags(value.get("text") or value.get("name") or "")
        return [text] if text else []
    steps: list[str] = []
    if isinstance(value, list):
        for item in value:
            steps.extend(flatten_instructions(item))
    return steps


def find_recipe_node(payload: Any) -> Optional[dict[str, Any]]:
    """Walk a JSON-LD document looking for the Recipe object inside it."""
    if isinstance(payload, dict):
        kind = payload.get("@type")
        kinds = kind if isinstance(kind, list) else [kind]
        if any(isinstance(k, str) and k.lower() == "recipe" for k in kinds):
            return payload
        for key in ("@graph", "mainEntity", "mainEntityOfPage", "itemListElement"):
            found = find_recipe_node(payload.get(key))
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = find_recipe_node(item)
            if found:
                return found
    return None


COMMON_UNITS = {
    "g", "kg", "mg", "ml", "l", "dl", "cl", "oz", "lb", "lbs", "tsp", "tsps", "teaspoon",
    "teaspoons", "tbsp", "tbsps", "tablespoon", "tablespoons", "cup", "cups", "clove",
    "cloves", "slice", "slices", "pinch", "pinches", "can", "cans", "tin", "tins", "pack",
    "packs", "packet", "packets", "piece", "pieces", "sprig", "sprigs", "stick", "sticks",
    "bunch", "bunches", "handful", "handfuls", "jar", "jars", "punnet", "sheet", "sheets",
}


def split_unit(rest: str) -> tuple[str, str]:
    """Separate a leading unit word from the ingredient name."""
    words = rest.split()
    if words and words[0].lower().rstrip(".") in COMMON_UNITS:
        return words[0].rstrip("."), " ".join(words[1:])
    return "", rest


def jsonld_to_recipe(node: dict[str, Any], source: str) -> dict[str, Any]:
    ingredients = []
    raw_ingredients = node.get("recipeIngredient") or node.get("ingredients") or []
    if isinstance(raw_ingredients, str):
        raw_ingredients = [raw_ingredients]
    for line in raw_ingredients:
        text = strip_tags(line) if isinstance(line, str) else first_text(line)
        if not text:
            continue
        amount, rest = parse_measure(text)
        if amount is None:
            ingredients.append({"amount": None, "unit": "", "item": text, "note": "", "group": ""})
            continue
        unit, item = split_unit(rest)
        ingredients.append({"amount": amount, "unit": unit, "item": item, "note": "", "group": ""})

    steps = [
        {"text": text, "seconds": detect_duration(text), "photo_id": None}
        for text in flatten_instructions(node.get("recipeInstructions"))
        if text
    ]

    yield_value = node.get("recipeYield")
    servings = 0
    for candidate in (yield_value if isinstance(yield_value, list) else [yield_value]):
        digits = re.search(r"\d+", str(candidate or ""))
        if digits:
            servings = int(digits.group())
            break

    keywords = node.get("keywords") or []
    if isinstance(keywords, str):
        keywords = [k.strip() for k in keywords.split(",")]
    tags = [strip_tags(k) for k in keywords if strip_tags(k)][:8]

    return {
        "title": first_text(node.get("name")) or "Imported recipe",
        "summary": first_text(node.get("description"))[:300],
        "category": first_text(node.get("recipeCategory")),
        "cuisine": first_text(node.get("recipeCuisine")),
        "tags": tags,
        "servings": min(max(servings or 4, 1), 99),
        "prep_minutes": iso_duration_minutes(node.get("prepTime")),
        "cook_minutes": iso_duration_minutes(node.get("cookTime")),
        "difficulty": "medium",
        "ingredients": ingredients,
        "steps": steps,
        "notes": "",
        "source": first_text(node.get("url")) or source,
        "photo_url": first_text(node.get("image")),
    }


LD_JSON_BLOCK = re.compile(
    r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.S | re.I
)


@app.post("/api/import/url", status_code=201)
async def import_url(payload: PhotoImportIn) -> dict[str, Any]:
    """Import a recipe from a page that publishes schema.org Recipe data.

    Most recipe sites embed it as JSON-LD, which is far more dependable than
    trying to read their markup. A page without it is reported as such rather
    than guessed at.
    """
    url = assert_public_http_url(payload.url)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        try:
            resp = await client.get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code in (401, 402, 403):
                reason = "That site refused the request (%d). Some publishers block servers." % code
            elif code == 404:
                reason = "That page was not found (404). Check the address."
            else:
                reason = "That page returned an error (%d)." % code
            raise HTTPException(status_code=502, detail=reason)
        except httpx.HTTPError:
            raise HTTPException(status_code=502, detail="Could not reach that site")

        node = None
        for match in LD_JSON_BLOCK.finditer(resp.text):
            try:
                node = find_recipe_node(json.loads(match.group(1).strip()))
            except (json.JSONDecodeError, ValueError):
                continue
            if node:
                break

        if node is None:
            raise HTTPException(
                status_code=422,
                detail="That page does not publish a recipe this app can read. "
                       "Paste the ingredients and method instead.",
            )

        draft = jsonld_to_recipe(node, url)
        photo_url = draft.pop("photo_url", "")
        photo_id = None
        if photo_url:
            try:
                assert_public_http_url(photo_url)
                img = await client.get(photo_url, headers={"User-Agent": USER_AGENT})
                img.raise_for_status()
                mime = img.headers.get("content-type", "").split(";")[0].strip().lower()
                photo_id = store_photo(img.content, mime, "web", draft["title"], url)["id"]
            except (httpx.HTTPError, HTTPException):
                photo_id = None  # The recipe is still worth importing without it.

    return create_recipe(RecipeIn(photo_id=photo_id, **draft))


@app.post("/api/import/mealdb/{meal_id}", status_code=201)
async def import_mealdb(meal_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        try:
            resp = await client.get(
                "https://www.themealdb.com/api/json/v1/1/lookup.php",
                params={"i": meal_id},
                headers={"User-Agent": USER_AGENT},
            )
            resp.raise_for_status()
        except httpx.HTTPError:
            raise HTTPException(status_code=502, detail="Recipe import is unavailable right now")
        meals = resp.json().get("meals") or []
        if not meals:
            raise HTTPException(status_code=404, detail="That recipe no longer exists")

        draft = mealdb_to_recipe(meals[0])
        photo_url = draft.pop("photo_url", "")
        photo_id = None
        if photo_url:
            try:
                img = await client.get(photo_url, headers={"User-Agent": USER_AGENT})
                img.raise_for_status()
                mime = img.headers.get("content-type", "").split(";")[0].strip().lower()
                stored = store_photo(
                    img.content, mime, "TheMealDB", draft["title"] + " via TheMealDB", draft["source"]
                )
                photo_id = stored["id"]
            except (httpx.HTTPError, HTTPException):
                photo_id = None  # The recipe is still worth importing without its photo.

    return create_recipe(RecipeIn(photo_id=photo_id, **draft))


@app.get("/api/shopping")
def list_shopping() -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM shopping ORDER BY checked, position, created_at").fetchall()
    return [dict(r, checked=bool(r["checked"])) for r in rows]


@app.post("/api/shopping", status_code=201)
def add_shopping(payload: ShoppingIn) -> dict[str, Any]:
    sid = uuid.uuid4().hex
    with db() as conn:
        pos = conn.execute("SELECT COALESCE(MAX(position), 0) + 1 AS p FROM shopping").fetchone()["p"]
        conn.execute(
            "INSERT INTO shopping (id, text, checked, recipe, position, created_at) VALUES (?,?,0,?,?,?)",
            (sid, payload.text, payload.recipe, pos, now_iso()),
        )
        row = conn.execute("SELECT * FROM shopping WHERE id=?", (sid,)).fetchone()
    return dict(row, checked=bool(row["checked"]))


@app.patch("/api/shopping/{item_id}")
def patch_shopping(item_id: str, payload: ShoppingPatch) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute("SELECT * FROM shopping WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Item not found")
        text = row["text"] if payload.text is None else payload.text
        checked = row["checked"] if payload.checked is None else int(payload.checked)
        conn.execute("UPDATE shopping SET text=?, checked=? WHERE id=?", (text, checked, item_id))
        row = conn.execute("SELECT * FROM shopping WHERE id=?", (item_id,)).fetchone()
    return dict(row, checked=bool(row["checked"]))


@app.delete("/api/shopping/{item_id}", status_code=204)
def delete_shopping(item_id: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM shopping WHERE id=?", (item_id,))


@app.delete("/api/shopping", status_code=204)
def clear_shopping(checked_only: bool = True) -> None:
    with db() as conn:
        if checked_only:
            conn.execute("DELETE FROM shopping WHERE checked=1")
        else:
            conn.execute("DELETE FROM shopping")


@app.get("/api/backup")
def export_backup() -> JSONResponse:
    """Export every recipe as JSON. Photos stay on the server."""
    with db() as conn:
        recipes = [row_to_recipe(r) for r in conn.execute("SELECT * FROM recipes").fetchall()]
    payload = {"service": SERVICE_NAME, "version": 1, "exported_at": now_iso(), "recipes": recipes}
    filename = "mordorcook-backup-%s.json" % datetime.now().strftime("%Y-%m-%d")
    return JSONResponse(payload, headers={"Content-Disposition": 'attachment; filename="%s"' % filename})


@app.post("/api/backup")
async def import_backup(request: Request) -> dict[str, int]:
    """Restore recipes from an exported file."""
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="That file is not valid JSON")
    recipes = payload.get("recipes") if isinstance(payload, dict) else None
    if not isinstance(recipes, list):
        raise HTTPException(status_code=400, detail="That file has no recipes in it")

    imported = 0
    for raw in recipes:
        if not isinstance(raw, dict):
            continue
        try:
            model = RecipeIn(**{k: v for k, v in raw.items() if k in RecipeIn.model_fields})
        except Exception:
            continue
        create_recipe(model)
        imported += 1
    return {"imported": imported}


@app.get("/api/stats")
def stats() -> dict[str, Any]:
    with db() as conn:
        recipes = conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(cooked_count), 0) AS n FROM recipes"
        ).fetchone()
        photos = conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(bytes), 0) AS b FROM photos"
        ).fetchone()
    return {
        "recipes": recipes["c"],
        "cooked_total": recipes["n"],
        "photos": photos["c"],
        "photo_bytes": photos["b"],
    }


# Not in the platform's table on every host, and a manifest served as
# octet-stream is ignored by the browser.
mimetypes.add_type("application/manifest+json", ".webmanifest")

# The static mount is registered LAST: routes match in registration order, so
# mounting it earlier would swallow every /api/... request and return 404.
app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
