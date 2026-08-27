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

-- Everyone who cooks from this library. There is no login: a browser simply
-- says which of these people it is, and the server keeps their private things
-- (favourites) apart while the recipes themselves stay shared.
CREATE TABLE IF NOT EXISTS users (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    colour     TEXT NOT NULL DEFAULT '',
    position   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

-- Favourites are the one thing that is NOT shared, so they live beside the
-- recipe rather than inside it.
CREATE TABLE IF NOT EXISTS favourites (
    user_id    TEXT NOT NULL,
    recipe_id  TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, recipe_id)
);

-- The shared "what shall we cook?" board. Every entry carries its author, so
-- the answer to "who put that there" is visible at a glance.
CREATE TABLE IF NOT EXISTS wishlist (
    id         TEXT PRIMARY KEY,
    text       TEXT NOT NULL DEFAULT '',
    note       TEXT NOT NULL DEFAULT '',
    recipe_id  TEXT,
    user_id    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_recipes_updated ON recipes(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_favourites_user ON favourites(user_id);
CREATE INDEX IF NOT EXISTS idx_wishlist_created ON wishlist(created_at DESC);
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


DEFAULT_USER_NAME = os.getenv("MORDORCOOK_DEFAULT_USER", "Me").strip() or "Me"

# Kept in step with the swatches offered in the app's settings screen.
USER_COLOURS = ["sage", "clay", "plum", "sky", "amber", "rose"]


def create_user(conn: sqlite3.Connection, name: str, colour: str = "") -> str:
    """Add a person and return their id."""
    uid = uuid.uuid4().hex
    position = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 AS p FROM users").fetchone()["p"]
    if colour not in USER_COLOURS:
        colour = USER_COLOURS[position % len(USER_COLOURS)]
    conn.execute(
        "INSERT INTO users (id, name, colour, position, created_at) VALUES (?,?,?,?,?)",
        (uid, name, colour, position, now_iso()),
    )
    return uid


def init_storage() -> None:
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(SCHEMA)

        # A library always belongs to at least one person. On the first run
        # after this feature landed, the favourites that were a property of the
        # recipe become that first person's favourites, so nothing is lost.
        if conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 0:
            uid = create_user(conn, DEFAULT_USER_NAME)
            ts = now_iso()
            conn.executemany(
                "INSERT OR IGNORE INTO favourites (user_id, recipe_id, created_at) VALUES (?,?,?)",
                [(uid, r["id"], ts)
                 for r in conn.execute("SELECT id FROM recipes WHERE favorite=1").fetchall()],
            )


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


class UserIn(BaseModel):
    name: str = Field(default="", max_length=40)
    colour: str = Field(default="", max_length=24)


class UserPatch(BaseModel):
    name: Optional[str] = Field(default=None, max_length=40)
    colour: Optional[str] = Field(default=None, max_length=24)


class FavouriteIn(BaseModel):
    favorite: bool = True


class WishIn(BaseModel):
    text: str = Field(default="", max_length=200)
    note: str = Field(default="", max_length=400)
    recipe_id: Optional[str] = None


class PhotoImportIn(BaseModel):
    url: str
    attribution: str = ""
    link: str = ""
    source: str = "web"


def row_to_recipe(row: sqlite3.Row, fav_ids: Optional[set[str]] = None) -> dict[str, Any]:
    """Shape a recipe row for the API.

    ``fav_ids`` is the set of recipes the person asking has favourited, so the
    same recipe is returned with a different ``favorite`` flag for each of them.
    """
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
        "favorite": row["id"] in fav_ids if fav_ids is not None else bool(row["favorite"]),
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


def requested_user(request: Request) -> str:
    """The person this browser says it is, unverified.

    There is no login here: the app is for one household on its own network,
    and a header is enough to keep two people's favourites from mixing.
    """
    return (request.headers.get("X-User-Id") or request.query_params.get("user") or "").strip()


def resolve_user(conn: sqlite3.Connection, raw: str) -> str:
    """Turn that claim into a real user id, falling back to the first person."""
    if raw:
        row = conn.execute("SELECT id FROM users WHERE id=?", (raw,)).fetchone()
        if row is not None:
            return row["id"]
    row = conn.execute("SELECT id FROM users ORDER BY position, created_at LIMIT 1").fetchone()
    if row is not None:
        return row["id"]
    return create_user(conn, DEFAULT_USER_NAME)


def favourite_ids(conn: sqlite3.Connection, user_id: str) -> set[str]:
    return {r["recipe_id"]
            for r in conn.execute("SELECT recipe_id FROM favourites WHERE user_id=?", (user_id,))}


def set_favourite(conn: sqlite3.Connection, user_id: str, recipe_id: str, on: bool) -> None:
    if on:
        conn.execute(
            "INSERT OR IGNORE INTO favourites (user_id, recipe_id, created_at) VALUES (?,?,?)",
            (user_id, recipe_id, now_iso()),
        )
    else:
        conn.execute("DELETE FROM favourites WHERE user_id=? AND recipe_id=?", (user_id, recipe_id))
    # The recipe's own column is no longer the truth, but keeping it as
    # "somebody likes this" means older backups still round-trip sensibly.
    anyone = conn.execute(
        "SELECT COUNT(*) AS c FROM favourites WHERE recipe_id=?", (recipe_id,)
    ).fetchone()["c"]
    conn.execute("UPDATE recipes SET favorite=? WHERE id=?", (1 if anyone else 0, recipe_id))


def row_to_user(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "name": row["name"], "colour": row["colour"],
            "position": row["position"], "created_at": row["created_at"]}


@app.get("/api/users")
def list_users() -> list[dict[str, Any]]:
    with db() as conn:
        if conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 0:
            create_user(conn, DEFAULT_USER_NAME)
        rows = conn.execute("SELECT * FROM users ORDER BY position, created_at").fetchall()
    return [row_to_user(r) for r in rows]


@app.post("/api/users", status_code=201)
def add_user(payload: UserIn) -> dict[str, Any]:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Give this person a name")
    with db() as conn:
        uid = create_user(conn, name, payload.colour.strip())
        row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return row_to_user(row)


@app.patch("/api/users/{user_id}")
def patch_user(user_id: str, payload: UserPatch) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="No such person")
        name = row["name"] if payload.name is None else payload.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Give this person a name")
        colour = row["colour"] if payload.colour is None else payload.colour.strip()
        if colour not in USER_COLOURS:
            colour = row["colour"]
        conn.execute("UPDATE users SET name=?, colour=? WHERE id=?", (name, colour, user_id))
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return row_to_user(row)


@app.delete("/api/users/{user_id}", status_code=204)
def delete_user(user_id: str) -> None:
    """Remove a person, along with their favourites and their wishlist entries."""
    with db() as conn:
        if conn.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="No such person")
        if conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] < 2:
            raise HTTPException(status_code=400, detail="The library needs at least one person")
        conn.execute("DELETE FROM favourites WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM wishlist WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        conn.execute(
            """UPDATE recipes SET favorite = CASE
                   WHEN EXISTS (SELECT 1 FROM favourites WHERE recipe_id = recipes.id)
                   THEN 1 ELSE 0 END"""
        )


@app.get("/api/recipes")
def list_recipes(request: Request, q: str = "", tag: str = "", favorite: bool = False,
                 sort: str = "updated") -> list[dict[str, Any]]:
    with db() as conn:
        user_id = resolve_user(conn, requested_user(request))
        favs = favourite_ids(conn, user_id)
        rows = conn.execute("SELECT * FROM recipes").fetchall()

    recipes = [row_to_recipe(r, favs) for r in rows]

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
def create_recipe(payload: RecipeIn, request: Request) -> dict[str, Any]:
    return store_recipe(payload, requested_user(request))


def store_recipe(payload: RecipeIn, raw_user: str = "") -> dict[str, Any]:
    """Insert a recipe. Shared with the importers, which have no request body."""
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
                0,
                ts,
                ts,
            ),
        )
        user_id = resolve_user(conn, raw_user)
        if payload.favorite:
            set_favourite(conn, user_id, rid, True)
        favs = favourite_ids(conn, user_id)
        row = conn.execute("SELECT * FROM recipes WHERE id=?", (rid,)).fetchone()
    return row_to_recipe(row, favs)


@app.get("/api/recipes/{recipe_id}")
def get_recipe(recipe_id: str, request: Request) -> dict[str, Any]:
    with db() as conn:
        favs = favourite_ids(conn, resolve_user(conn, requested_user(request)))
        row = conn.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Recipe not found")
    return row_to_recipe(row, favs)


@app.put("/api/recipes/{recipe_id}")
def update_recipe(recipe_id: str, payload: RecipeIn, request: Request) -> dict[str, Any]:
    with db() as conn:
        if conn.execute("SELECT id FROM recipes WHERE id=?", (recipe_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Recipe not found")
        conn.execute(
            """UPDATE recipes SET title=?, summary=?, category=?, cuisine=?, tags=?,
                   servings=?, prep_minutes=?, cook_minutes=?, difficulty=?, photo_id=?,
                   ingredients=?, steps=?, notes=?, source=?, updated_at=?
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
                now_iso(),
                recipe_id,
            ),
        )
        user_id = resolve_user(conn, requested_user(request))
        set_favourite(conn, user_id, recipe_id, payload.favorite)
        favs = favourite_ids(conn, user_id)
        updated = conn.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone()
    return row_to_recipe(updated, favs)


@app.delete("/api/recipes/{recipe_id}", status_code=204)
def delete_recipe(recipe_id: str) -> None:
    with db() as conn:
        if conn.execute("DELETE FROM recipes WHERE id=?", (recipe_id,)).rowcount == 0:
            raise HTTPException(status_code=404, detail="Recipe not found")
        # Nobody's favourites or wishlist should point at a recipe that is gone.
        conn.execute("DELETE FROM favourites WHERE recipe_id=?", (recipe_id,))
        conn.execute("DELETE FROM wishlist WHERE recipe_id=?", (recipe_id,))


@app.put("/api/recipes/{recipe_id}/favorite")
def put_favorite(recipe_id: str, payload: FavouriteIn, request: Request) -> dict[str, Any]:
    """Favourite or unfavourite a recipe for whoever is asking.

    A dedicated route rather than a whole-recipe PUT, so tapping the heart can
    never overwrite an edit somebody else is making at the same moment.
    """
    with db() as conn:
        if conn.execute("SELECT id FROM recipes WHERE id=?", (recipe_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Recipe not found")
        user_id = resolve_user(conn, requested_user(request))
        set_favourite(conn, user_id, recipe_id, payload.favorite)
        favs = favourite_ids(conn, user_id)
        row = conn.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone()
    return row_to_recipe(row, favs)


@app.post("/api/recipes/{recipe_id}/cooked")
def mark_cooked(recipe_id: str, request: Request) -> dict[str, Any]:
    """Record that the recipe was cooked once more, from the cooking mode."""
    with db() as conn:
        if conn.execute("SELECT id FROM recipes WHERE id=?", (recipe_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Recipe not found")
        conn.execute(
            "UPDATE recipes SET cooked_count = cooked_count + 1, last_cooked_at=? WHERE id=?",
            (now_iso(), recipe_id),
        )
        favs = favourite_ids(conn, resolve_user(conn, requested_user(request)))
        updated = conn.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone()
    return row_to_recipe(updated, favs)


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
async def import_url(payload: PhotoImportIn, request: Request) -> dict[str, Any]:
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

    return store_recipe(RecipeIn(photo_id=photo_id, **draft), requested_user(request))


@app.post("/api/import/mealdb/{meal_id}", status_code=201)
async def import_mealdb(meal_id: str, request: Request) -> dict[str, Any]:
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

    return store_recipe(RecipeIn(photo_id=photo_id, **draft), requested_user(request))


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


def row_to_wish(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "text": row["text"],
        "note": row["note"],
        "recipe_id": row["recipe_id"],
        "recipe_title": row["recipe_title"],
        "recipe_photo_id": row["recipe_photo_id"],
        "user_id": row["user_id"],
        "user_name": row["user_name"] or "Someone",
        "user_colour": row["user_colour"] or "",
        "created_at": row["created_at"],
    }


# One join, so a wish always arrives knowing who wrote it and, when it points
# at a recipe, what that recipe is called right now rather than when it was added.
WISH_SELECT = """
    SELECT w.*, u.name AS user_name, u.colour AS user_colour,
           r.title AS recipe_title, r.photo_id AS recipe_photo_id
      FROM wishlist w
      LEFT JOIN users u ON u.id = w.user_id
      LEFT JOIN recipes r ON r.id = w.recipe_id
"""


@app.get("/api/wishlist")
def list_wishlist() -> list[dict[str, Any]]:
    """Everyone's wishes, newest first. Deliberately not filtered by person:
    the whole point is seeing what the others put up there."""
    with db() as conn:
        # rowid breaks the tie when several wishes land inside the same second.
        rows = conn.execute(WISH_SELECT + " ORDER BY w.created_at DESC, w.rowid DESC").fetchall()
    return [row_to_wish(r) for r in rows]


@app.post("/api/wishlist", status_code=201)
def add_wish(payload: WishIn, request: Request) -> dict[str, Any]:
    text = payload.text.strip()
    with db() as conn:
        user_id = resolve_user(conn, requested_user(request))

        recipe_id = None
        if payload.recipe_id:
            recipe = conn.execute(
                "SELECT id, title FROM recipes WHERE id=?", (payload.recipe_id,)
            ).fetchone()
            if recipe is None:
                raise HTTPException(status_code=404, detail="Recipe not found")
            recipe_id = recipe["id"]
            if not text:
                text = recipe["title"]
            # Adding the same recipe twice from its page is a slip, not a wish.
            existing = conn.execute(
                "SELECT id FROM wishlist WHERE user_id=? AND recipe_id=?", (user_id, recipe_id)
            ).fetchone()
            if existing is not None:
                row = conn.execute(WISH_SELECT + " WHERE w.id=?", (existing["id"],)).fetchone()
                return row_to_wish(row)

        if not text:
            raise HTTPException(status_code=400, detail="Write what you fancy")

        wid = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO wishlist (id, text, note, recipe_id, user_id, created_at) VALUES (?,?,?,?,?,?)",
            (wid, text, payload.note.strip(), recipe_id, user_id, now_iso()),
        )
        row = conn.execute(WISH_SELECT + " WHERE w.id=?", (wid,)).fetchone()
    return row_to_wish(row)


@app.delete("/api/wishlist/{wish_id}", status_code=204)
def delete_wish(wish_id: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM wishlist WHERE id=?", (wish_id,))


@app.delete("/api/wishlist", status_code=204)
def clear_wishlist(request: Request, mine_only: bool = False) -> None:
    """Wipe the board. It is meant to be refilled tomorrow."""
    with db() as conn:
        if mine_only:
            conn.execute("DELETE FROM wishlist WHERE user_id=?",
                         (resolve_user(conn, requested_user(request)),))
        else:
            conn.execute("DELETE FROM wishlist")


@app.get("/api/backup")
def export_backup(request: Request) -> JSONResponse:
    """Export every recipe as JSON. Photos stay on the server.

    Recipes are shared, so the file holds all of them; the ``favorite`` flag in
    it is the one belonging to whoever asked for the export.
    """
    with db() as conn:
        favs = favourite_ids(conn, resolve_user(conn, requested_user(request)))
        recipes = [row_to_recipe(r, favs) for r in conn.execute("SELECT * FROM recipes").fetchall()]
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
        store_recipe(model, requested_user(request))
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
        people = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        wishes = conn.execute("SELECT COUNT(*) AS c FROM wishlist").fetchone()["c"]
    return {
        "recipes": recipes["c"],
        "cooked_total": recipes["n"],
        "photos": photos["c"],
        "photo_bytes": photos["b"],
        "people": people,
        "wishlist": wishes,
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
