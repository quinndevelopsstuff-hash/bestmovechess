# bestmovechess API

The same app that serves the web page serves a small JSON API.

- **Base URL:** wherever you run it, e.g. `http://127.0.0.1:8000` on the machine itself
  or `https://<machine>.<tailnet>.ts.net` through `tailscale serve`.
- **Authentication:** none. Keep the app on localhost or a private network such as your
  tailnet, and don't expose it to the internet.
- **Format:** responses are JSON. Errors always look like
  `{"error": "<code>", "detail": "<human-readable message>"}`.
- **Interactive docs:** `/docs` (Swagger UI) and `/openapi.json` are served by the app,
  and you can try both endpoints from `/docs`, including photo uploads.

| Method | Path       | Purpose                                     |
|--------|------------|---------------------------------------------|
| GET    | `/move`    | Best move for a position given as FEN       |
| POST   | `/move`    | Recognise a board photo, then best move     |
| GET    | `/img`     | PNG/SVG diagram of a position and best move |
| GET    | `/img/error` | PNG/SVG image explaining an error code    |
| GET    | `/healthz` | Liveness check                              |

## Conventions

- **Moves** are in UCI notation in `best`: from-square + to-square, e.g. `e2e4`.
  Castling is the king's move (`e1g1`), and promotions add the piece (`e7e8q`).
  `san` is the same move in standard algebraic notation (`Nf3`, `O-O`, `Qxf7#`).
- **`eval`** is in pawns from **White's** point of view: `+1.5` means White is ahead by
  about a pawn and a half, whichever side is to move.
- **`mate`**: when the engine finds a forced mate, `eval` is `null` and `mate` is the
  number of moves to mate, positive if White mates and negative if Black mates.
  Otherwise `mate` is `null`.
- **Squares** are named `a1`–`h8`. Piece letters follow FEN: uppercase is White,
  lowercase is Black (`K Q R B N P` / `k q r b n p`), and `.` means an empty square.

## GET /move

Best move for a position.

| Query param | Required | Default | Meaning                                       |
|-------------|----------|---------|-----------------------------------------------|
| `fen`       | yes      |         | Full FEN (all six fields recommended)         |
| `depth`     | no       | `15`    | Search depth, 1 to `MAX_DEPTH` (default 25)   |

Each search is also capped at `ENGINE_MAX_SECONDS` (default 10 s), so on slow hardware
the returned `depth` can be lower than requested. Remember to URL-encode the FEN: its
spaces and slashes need encoding, so use `--data-urlencode` with curl.

```sh
curl -G http://127.0.0.1:8000/move \
  --data-urlencode 'fen=rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1' \
  -d depth=12
```

```json
{"best": "e7e5", "san": "e5", "eval": 0.31, "mate": null, "depth": 12,
 "fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
 "image_url": "https://<your-host>/img?fen=rnbqkbnr%2Fpppppppp%2F...+b+KQkq+-+0+1&best=e7e5&turn=b&eval=0.31"}
```

A forced mate:

```json
{"best": "a1a8", "san": "Ra8#", "eval": null, "mate": 1, "depth": 10,
 "fen": "6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1", "image_url": "https://<your-host>/img?...&mate=1"}
```

**Every `GET /move` response includes `image_url`**, whatever happens. On success it
links to [`GET /img`](#get-img): a diagram of the position with the best move drawn.
On any error it links to [`GET /img/error`](#get-imgerror), with the matching code.
That covers a bad FEN, an illegal position, a finished game, a missing or bad
parameter, the engine being down, and unexpected server errors. For a position that
parses but can't be analysed, the error also includes `fen`, and the image draws the
board with the problem squares marked:

```json
{"error": "illegal_position", "detail": "Black has no king", "fen": "8/8/8/8/8/8/8/K7 w - - 0 1",
 "image_url": "https://<your-host>/img/error?code=missing_king&fen=8%2F8%2F8%2F8%2F8%2F8%2F8%2FK7+w+-+-+0+1"}
```

Links are absolute, built the same way as for `POST /move` (see `img_out` below).

| Field   | Type          | Meaning                                       |
|---------|---------------|-----------------------------------------------|
| `best`  | string        | Best move, UCI                                |
| `san`   | string        | Best move, SAN                                |
| `eval`  | number / null | Pawns, White's point of view; null when mate  |
| `mate`  | int / null    | Moves to forced mate (+ White, − Black)       |
| `depth` | int           | Depth actually reached                        |
| `fen`   | string        | The position analysed, normalised             |
| `image_url` | string    | Absolute link to a diagram of the move (always present) |

## POST /move

Recognises the position in a photo of a physical board, then returns the best move.
Send `multipart/form-data`:

| Field      | Required | Meaning                                                              |
|------------|----------|----------------------------------------------------------------------|
| `image`    | yes      | The photo: JPEG, PNG or WebP, at most 10 MB (`MAX_UPLOAD_MB`)        |
| `turn`     | yes      | `w` or `b`, the side to move (a photo can't show it)                 |
| `castling` | no       | Castling rights still available, a subset of `KQkq` in that order (e.g. `Kq`), or `-`. Default `-` |
| `ep`       | no       | En passant: the file (`a`-`h`) of a pawn that just moved two squares. Default none |
| `depth`    | no       | As for `GET`                                                         |
| `img_out`  | no       | `true`/`false` (default `false`). When true, the response also has `image_url` (see below) |
| `flip`     | no       | Screenshots: `auto` (default) lets chessfen guess whether Black is at the bottom; `true`/`false` forces it |
| `source`   | no       | `auto` (default), `screenshot` or `camera`: which recognizer to use (see below) |
| `profile`  | no       | Screenshots: a saved calibration profile's name, or `builtin` for stock chessfen. Default: the best saved profile is picked automatically (see `/profiles`) |

Castling and en passant default to none because they can't be seen in a photo.

```sh
curl -F image=@board.jpg -F turn=w -F depth=12 http://127.0.0.1:8000/move
```

```json
{
  "best": "c6d5", "san": "Bd5", "eval": 1.74, "mate": null, "depth": 12,
  "fen": "1q1r4/r3b1k1/p1B1Q1p1/1pp5/2P2P2/2N3P1/PP3P2/R5K1 w - - 0 1",
  "detected": "1q1r4/r3b1k1/p1B1Q1p1/1pp5/2P2P2/2N3P1/PP3P2/R5K1",
  "confidence": 1.0,
  "squares": {"a8": 0.954, "b8": 0.96, "...": "one entry per square"},
  "uncertain": [],
  "repairs": [],
  "alternatives": {},
  "corners": [[281.9, 137.0], [646.2, 130.6], [654.8, 499.8], [281.2, 502.3]],
  "image_size": [900, 600]
}
```

It returns every field of `GET /move`, plus:

| Field          | Type   | Meaning |
|----------------|--------|---------|
| `detected`     | string | Recognised piece placement (the first FEN field) |
| `confidence`   | number | 0–1 estimate that the **whole board** is right (photos), or the weakest square's confidence (screenshots). On held-out test photos every board at ≥ 0.8 was fully correct, and below 0.5 only about half were |
| `squares`      | object | Confidence for the piece chosen on each square. Photos: model probability (tops out near 0.95, because the model was trained that way). Screenshots: chessfen's verdict, scaled so that below 0.6 means chessfen doubted it |
| `uncertain`    | array  | The squares most worth checking: photo squares below 0.85, or the squares chessfen doubted |
| `alternatives` | object | Photos, for squares below 0.9: the model's top guesses, e.g. `{"f6": [["n", 0.74], ["b", 0.21]]}`. Empty for screenshots |
| `repairs`      | array  | Photos: changes made to keep the position legal, e.g. `{"square": "e1", "from": "Q", "to": "K", "reason": "missing_king"}`. Reasons: `pawn_on_back_rank`, `extra_king`, `missing_king`, `too_many_pawns`. Always empty for screenshots: an illegal screenshot reading comes back as an `illegal_position` error with the board, so you can correct it |
| `corners`      | array  | Pixel positions of the board's a8, h8, h1 and a1 corners in the image |
| `image_size`   | array  | `[width, height]` that `corners` refers to; images over 2000 px are shrunk first |
| `source`       | string | Which recognizer ran: `screenshot` or `camera` |
| `flipped`      | bool   | Screenshots: the board was read with Black at the bottom (`corners` then start at the bottom-right) |
| `orientation`  | string / null | Screenshots: `chessfen` (chessfen guessed from where each side's pieces stand) or `requested` (the `flip` field); `null` for photos |
| `profile`      | string / null | Screenshots: the calibration profile used (`builtin` = stock chessfen shapes); `null` for photos |

The board is assumed to be photographed with White nearer the camera. If the photo was
taken from the side or from Black's end, the detected position may be rotated.

### Correcting a recognition

Recognition can be wrong, so treat `detected` as a draft. To correct it:

1. `POST /move` with the photo.
2. Fix `detected` (show it to a person, rotate it, change squares).
3. `GET /move` with the corrected FEN: `<placement> <turn> <castling> <ep> 0 1`.

If nothing needed fixing, use the move from step 1 directly. The web page works this way.

### Photo errors still describe what was seen

If the recognised position can't be analysed (`illegal_position`, or `game_over`), the
error body also contains `fen`, `detected`, `confidence`, `squares`, `uncertain`,
`alternatives`, `repairs`, `corners` and `image_size`, so you can show the board and let
someone fix it:

```json
{"error": "illegal_position", "detail": "castling rights don't match king/rook placement",
 "fen": "1q1r4/r3b1k1/p1B1Q1p1/1pp5/2P2P2/2N3P1/PP3P2/R5K1 w KQ - 0 1",
 "detected": "1q1r4/r3b1k1/p1B1Q1p1/1pp5/2P2P2/2N3P1/PP3P2/R5K1", "confidence": 1.0, "...": "..."}
```

### `source`: screenshots and photos

There are two recognizers:

- **`screenshot`**: [chessfen](https://github.com/sunfmin/chessfen) (pure pixel math, no
  ML model), for flat digital boards such as a phone screenshot of a chess app. The board
  is first found by its checker pattern and cropped out, so the surrounding UI doesn't
  matter.
- **`camera`**: the pretrained model for photos of a physical board.

With `auto` (the default), an image containing a flat digital board, where squares of
one colour are practically identical, goes to chessfen. Everything else goes to the photo
model, as does a screenshot chessfen can't read (no board found, or an error). The
response's `source` says which recognizer ran.

For screenshots, only the piece placement comes from chessfen. Side to move, castling
(default none) and en passant come from your fields. Squares chessfen doubted are listed
in `uncertain`, however many there are; the reading is never swapped for the photo
model's, which reads flat screenshots much worse.

chessfen needs Python 3.13+. On older Pythons it isn't installed: `auto` sends
everything to the photo model, and `source=screenshot` returns
`501 recognition_unavailable`.

### `flip`: board shown from Black's side

For screenshots, chessfen guesses which side is at the bottom from where each colour's
pieces stand. `flip=true` (Black at the bottom) or `flip=false` overrides it. The
response's `flipped` and `orientation` say what was used. Camera photos ignore `flip`,
because the photo model has its own orientation logic.

### `img_out`: a diagram of the result

With `img_out=true` the response gets one more field, `image_url`. It's an absolute
link to [`GET /img`](#get-img) that draws the detected board with the best move:

```sh
curl -F image=@board.jpg -F turn=w -F img_out=true https://<your-host>/move
```

```json
{"best": "c6d5", "san": "Bd5", "...": "...",
 "image_url": "https://<your-host>/img?fen=1q1r4%2Fr3b1k1%2F...+w+-+-+0+1&best=c6d5&turn=w&eval=1.74"}
```

If the request fails, the JSON error gets an `image_url` pointing at
[`/img/error`](#get-imgerror) with the matching code. For position problems it
includes the detected FEN, and the code says what's wrong (for example
`missing_king` rather than just `illegal_position`). Errors that happen before the
form is read (`too_large`, `unsupported_media_type`, a malformed body) have no
`image_url`, because `img_out` hasn't been seen yet.

Links use `PUBLIC_BASE_URL` if it's set. Otherwise they're built from the
`X-Forwarded-Proto` and `X-Forwarded-Host` headers (which `tailscale serve` sends),
falling back to the `Host` header. Set `PUBLIC_BASE_URL` if you use another proxy
that doesn't send them, or serve the app under a sub-path.

### Privacy

Uploaded images are processed in memory and never stored or logged. Diagrams are drawn
from the position on request, never stored, and never use your photo. See the README.

## /profiles: calibration for screenshots

A profile holds 12 piece shapes cut from a screenshot of your own start position, which
makes screenshots from that app/theme/device far more accurate (see the README). Profiles
are tiny files in `PROFILE_DIR`; the screenshot you send is processed in memory only.
There is no login, so as with the rest of the app, keep it on your private network.

| Request | Does |
|---------|------|
| `GET /profiles` | `{"profiles": ["iphone-green"], "available": true}` |
| `POST /profiles` | Multipart: `name` (1-40 chars: lowercase letters, digits, `-`, `_`; not `builtin`) and `image` (a clean screenshot of the start position, White at the bottom or top). Returns `{"name", "profiles"}`. Reusing a name replaces that profile; at most 12 profiles |
| `DELETE /profiles/{name}` | Deletes one. `404 unknown_profile` if it doesn't exist |

Errors: `422 calibration_failed` (not a clean start position, or no board found),
`409 too_many_profiles`, `415 invalid_image`, `501 recognition_unavailable` (needs Python 3.13+).

## GET /img

Draws a position as a clean diagram: the best move as an arrow, its from and to
squares highlighted, and a caption such as **"Best: Nf3 · +0.45"** and "White to
move" underneath. The board is shown from the side to move. It's stateless: the image
is generated from the query parameters each time, and nothing is stored.

| Param    | Required | Default | Meaning |
|----------|----------|---------|---------|
| `fen`    | yes      |         | Position. A full FEN, or just the piece placement |
| `best`   | no       |         | Move to draw, UCI (e.g. `e2e4`); must be legal in the position |
| `turn`   | no       | from FEN | `w` or `b`: overrides the FEN's side to move (and the orientation) |
| `eval`   | no       |         | Evaluation for the caption, pawns from White's view (−1000 to 1000) |
| `mate`   | no       |         | Mate-in-N for the caption (+ White mates, − Black mates); takes precedence over `eval` |
| `format` | no       | `png`   | `png` or `svg` |
| `size`   | no       | `480`   | Board width in pixels, 160–1024. The caption adds some height |

```sh
curl -G https://<your-host>/img -o move.png \
  --data-urlencode 'fen=r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3' \
  -d best=f1b5 -d eval=0.45
```

Without `best`, the board is drawn on its own. A finished game is captioned
"Checkmate: Black wins." or "Stalemate: …".

**Invalid input gets an error image, not JSON.** The response is still an image (in the
requested format), so it shows up wherever the link is used:

| Status | Image says | When |
|-------:|------------|------|
| 400 | Invalid FEN | `fen` can't be parsed |
| 422 | Missing king, Too many kings, Pawn on back rank, Wrong side in check, … | The position is impossible; the board is drawn with the offending squares in red |
| 422 | Illegal move | `best` isn't a legal move in the position; its squares are marked |
| 422 | Bad request | Bad `format`, `size`, `turn`, `eval` or `mate`, or an overlong parameter |

Responses are `image/png` or `image/svg+xml`, with `Cache-Control: public, max-age=86400`
(the same URL always gives the same picture). If the server lacks the Cairo library,
PNG requests are answered with SVG.

## GET /img/error

An image with a short message explaining an error, for showing in place of a diagram.

| Param    | Required | Default | Meaning |
|----------|----------|---------|---------|
| `code`   | no       | `error` | One of the codes below; anything else shows "Something went wrong" |
| `fen`    | no       |         | The detected position. For position problems the board is drawn with the offending squares highlighted in red |
| `uncertain` | no    |         | Comma-separated squares (e.g. `e8,d8`) to shade amber as hard to read. `image_url` from `POST /move` includes it |
| `format` | no       | `png`   | `png` or `svg` |
| `size`   | no       | `480`   | Image width, 160–1024 |

```sh
curl -G https://<your-host>/img/error -o error.png -d code=missing_king \
  --data-urlencode 'fen=8/8/8/8/8/8/8/K7 w - - 0 1'
```

| `code` | Title |
|--------|-------|
| `board_not_found` | No board found |
| `invalid_image` | Image unreadable |
| `recognition_unavailable` | Board unreadable |
| `illegal_position` | Illegal position (with a FEN, the specific problem is named) |
| `missing_king`, `too_many_kings`, `pawn_on_back_rank`, `too_many_pieces`, `opposite_check`, `impossible_check`, `bad_castling`, `bad_en_passant` | The specific position problem |
| `game_over` | No legal moves (with a FEN: "Checkmate: … wins." or "Stalemate: …") |
| `illegal_move` | Illegal move |
| `invalid_fen` | Invalid FEN |
| `engine_unavailable` | Engine error |
| `invalid_request` | Bad request |
| `too_large` | Image too large |

The message text comes from a fixed table, and nothing from the request is drawn as
text. It always returns status 200.

## GET /healthz

```json
{"ok": true}
```

It reports that the web server is up. It doesn't check Stockfish or the model.

## Errors

| Status | `error`                   | When |
|-------:|---------------------------|------|
| 400    | `invalid_fen`             | The FEN can't be parsed |
| 400    | `invalid_request`         | Malformed multipart body or `Content-Length` |
| 404    | `not_found`               | Unknown path |
| 405    | `method_not_allowed`      | Wrong method for the path |
| 413    | `too_large`               | Upload over `MAX_UPLOAD_MB`, or a text field over 1 KB |
| 415    | `unsupported_media_type`  | `POST /move` body isn't `multipart/form-data` |
| 415    | `invalid_image`           | The upload isn't a decodable JPEG/PNG/WebP, or exceeds `MAX_IMAGE_PIXELS` |
| 422    | `invalid_request`         | Missing or invalid parameter (`detail` names it, e.g. `depth: must be between 1 and 25`) |
| 500    | `internal_error`          | `GET /move` only: an unexpected server error (still with `image_url`) |
| 422    | `illegal_position`        | Valid FEN syntax but impossible position; `detail` says why (e.g. `Black has no king`) |
| 422    | `game_over`               | Checkmate or stalemate: no move to suggest |
| 422    | `board_not_found`         | No chessboard found in the photo |
| 422    | `unknown_profile`         | `profile` names a calibration profile that doesn't exist |
| 422    | `calibration_failed`      | `POST /profiles`: the screenshot isn't a clean start position |
| 409    | `too_many_profiles`       | `POST /profiles`: the limit of 12 is reached |
| 501    | `recognition_unavailable` | Model file missing (`python scripts/download_model.py`) |
| 503    | `engine_unavailable`      | Stockfish missing or crashed (it restarts automatically on the next request) |

## Limits and performance

- Requests share one Stockfish process, so searches run one at a time. A slow,
  deep request delays the others.
- Search time is at most `ENGINE_MAX_SECONDS` (default 10 s). Lower `depth` for faster
  answers.
- Recognising a photo took 0.5–1 s per image on a desktop-class CPU in testing; expect
  it to be slower on a Raspberry Pi.
- Uploads are limited to 10 MB and 50 megapixels (`MAX_UPLOAD_MB`, `MAX_IMAGE_PIXELS`).
  For speed, send photos around 1600 px on the long side, like the web page does.

## Client examples

Python (`pip install requests`):

```python
import requests

BASE = "http://127.0.0.1:8000"

r = requests.get(f"{BASE}/move", params={"fen": "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5Q2/PPPP1PPP/RNB1K1NR w KQkq - 4 4"})
print(r.json()["san"])  # Qxf7#

with open("board.jpg", "rb") as f:
    r = requests.post(f"{BASE}/move", files={"image": f}, data={"turn": "w"})
body = r.json()
if r.ok:
    print(body["detected"], body["confidence"], body["san"])
else:
    print(body["error"], body["detail"])
```

JavaScript (browser or Node 18+):

```js
const BASE = "http://127.0.0.1:8000";

const fen = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 1";
const res = await fetch(`${BASE}/move?${new URLSearchParams({ fen, depth: 12 })}`);
console.log(await res.json());

const form = new FormData();
form.append("image", fileOrBlob, "board.jpg");
form.append("turn", "w");
const photo = await (await fetch(`${BASE}/move`, { method: "POST", body: form })).json();
console.log(photo.error ?? `${photo.san} (confidence ${photo.confidence})`);
```

The app sends no CORS headers, so browser code must be served from the app itself.
Scripts, servers and command-line tools can call it from anywhere they can reach it.
