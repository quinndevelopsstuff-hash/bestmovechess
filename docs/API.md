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
{"best": "c7c5", "san": "c5", "eval": 0.32, "mate": null, "depth": 12,
 "fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"}
```

A forced mate:

```json
{"best": "a1a8", "san": "Ra8#", "eval": null, "mate": 1, "depth": 10,
 "fen": "6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1"}
```

| Field   | Type          | Meaning                                       |
|---------|---------------|-----------------------------------------------|
| `best`  | string        | Best move, UCI                                |
| `san`   | string        | Best move, SAN                                |
| `eval`  | number / null | Pawns, White's point of view; null when mate  |
| `mate`  | int / null    | Moves to forced mate (+ White, − Black)       |
| `depth` | int           | Depth actually reached                        |
| `fen`   | string        | The position analysed, normalised             |

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
| `confidence`   | number | 0–1 estimate that the **whole board** is right. On held-out test photos every board at ≥ 0.8 was fully correct, and below 0.5 only about half were |
| `squares`      | object | Model confidence for the piece chosen on each square. Values top out near 0.95, because the model was trained that way |
| `uncertain`    | array  | Squares below 0.85, the ones most worth checking |
| `alternatives` | object | For squares below 0.9: the model's top guesses, e.g. `{"f6": [["n", 0.74], ["b", 0.21]]}` |
| `repairs`      | array  | Changes made to keep the position legal, e.g. `{"square": "e1", "from": "Q", "to": "K", "reason": "missing_king"}`. Reasons: `pawn_on_back_rank`, `extra_king`, `missing_king`, `too_many_pawns` |
| `corners`      | array  | Pixel positions of the board's a8, h8, h1 and a1 corners in the image |
| `image_size`   | array  | `[width, height]` that `corners` refers to; images over 2000 px are shrunk first |

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

### Privacy

Uploaded images are processed in memory and never stored or logged. See the README.

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
| 422    | `invalid_request`         | Missing or invalid parameter (`detail` names it, e.g. `depth: Input should be less than or equal to 25`) |
| 422    | `illegal_position`        | Valid FEN syntax but impossible position; `detail` says why (e.g. `Black has no king`) |
| 422    | `game_over`               | Checkmate or stalemate: no move to suggest |
| 422    | `board_not_found`         | No chessboard found in the photo |
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
