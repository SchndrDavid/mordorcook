# mordorcook

A personal recipe book with a hands-free cooking mode: write recipes down, then
follow them one step at a time with timers running and the screen kept awake.

Everything is stored on the server, so the phone propped against the kettle and
the browser on your desk always show the same library. Several people can share
that library while keeping their own favourites, and a shared wishlist answers
"what shall we cook today?" without anyone having to be in the same room.

## What it does

- **Recipes** — ingredients with amounts, sections and notes, a numbered method,
  photos per step, tags, times and a source. Full-text search across titles and
  ingredients.
- **Add by link** — paste the address of a recipe page and it comes in whole:
  ingredients, method, times, servings and the photo. Most recipe sites publish
  their recipes in a machine-readable form, and that is what gets read; a page
  without one says so rather than guessing. There is also a search over a free
  recipe database for when you have no link.
- **Cooking mode** — one step at a time in large type, a progress bar, the
  ingredient list a tap away, and the screen held awake while you cook. It
  remembers the step you were on, so an interruption does not cost your place.
- **Timers** — per-step timers started from the recipe, plus standalone ones.
  They keep correct time while the tab is in the background or the page is
  reloaded, and they chime and notify when they finish.
- **Photos** — upload from the device, take one with the camera, or search
  Openverse, Wikimedia Commons and TheMealDB from inside the app. A chosen
  picture is copied to this server, so a recipe never breaks when someone
  else's URL goes away.
- **People** — add everyone who cooks under Settings, and pick which of them
  this device is. The recipes, the shopping list and the wishlist are shared;
  favourites are private to each person, so one person hearting a recipe does
  not fill up somebody else's list.
- **Wishlist** — a shared board of what people fancy eating. Add a line of
  text, or a recipe straight from the library, and every entry shows who put
  it there and when. It is meant to change daily, so emptying the whole board
  is one button.
- **Shopping list** — send a whole recipe's ingredients to the list at the
  servings you actually plan to cook.
- **Scaling** — change the servings and every amount rescales, written the way
  a cook would (1½, not 1.5).
- **Hard to lose work** — leaving a half-edited recipe asks first, and a
  deleted recipe can be brought back from the message that confirms it.

## On a phone

Open the app and use your browser's *Add to Home Screen*. It then launches
without browser chrome, with its own icon.

Where the app is served over HTTPS, it also installs a worker that keeps the
recipes you have already opened readable when the network drops — which is
usually the moment you need them. Over plain `http://` browsers do not permit
this, so the app simply needs the network, and says plainly when it is offline
rather than failing silently.

## Running it

```bash
docker compose up -d --build
```

Then open `http://<host>:8105/`.

## Configuration

Copy `.env.example` to `.env` to change any of these. Every variable has a
working default, so the service also starts with no `.env` at all.

| Variable | Default | What it does |
|---|---|---|
| `MORDORCOOK_PORT` | `8105` | Host port the container is published on. |
| `MORDORCOOK_UID` | `1000` | UID that owns `./data`. Match it to the host user. |
| `MORDORCOOK_GID` | `1000` | GID that owns `./data`. |
| `MORDORCOOK_DEFAULT_USER` | `Me` | Name given to the first person, created automatically the first time the app starts. Rename them under Settings at any time. |
| `MORDORCOOK_CONTACT` | empty | An email address or repository URL, sent in the `User-Agent` of outgoing requests. Wikimedia Commons rejects clients that offer no way to contact their operator, so photo search quietly falls back to its other sources while this is empty. |

## Storage

The app writes to `./data`, mounted into the container at `/data`:

- `data/mordorcook.db` — SQLite database holding recipes, photo metadata, the
  people, their favourites, the shopping list and the wishlist.
- `data/photos/` — the image files themselves, one per uploaded or imported
  photo.

Back the whole directory up and you have backed up everything. There is also an
in-app JSON export of every recipe under Settings, though it does not include
the image files.

Nothing is stored in the browser except appearance, text size, which of the
people this device is, any timers currently running, which ingredients you have
ticked off, and the step you had reached in cooking mode. All of that is
per-device by design.

## Photo sources and licensing

Photo search returns results from Openverse, Wikimedia Commons and TheMealDB.
The credit line each source supplies is stored alongside the picture. These
are other people's photographs under their own licences — check the licence
before using one anywhere beyond your own kitchen.

## No authentication

There is none. Anyone who can reach the port can read and edit every recipe.
Switching between people is a choice this browser makes, not a login: it keeps
two people's favourites apart, and nothing more. Run it on a private network,
not on the open internet.

## License

Released under the MIT License — see [LICENSE](LICENSE).
