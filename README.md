# bestmovechess

Self-hosted "next best move" tool for chess. One small FastAPI app serves both the
web page and the API, and asks a local [Stockfish](https://stockfishchess.org/) for
the best move.

Runs well on a Raspberry Pi 5, and on any Linux/macOS machine or in Docker.

> **Status:** FEN API and web page work. Photo recognition is in progress, so photo
> uploads currently return `501 recognition_unavailable`.

## Web page

Open `http://127.0.0.1:8000/` (or your Tailscale URL). Take a photo with **Take photo**
or pick one with **Upload image**, choose the side to move, and tap **Find best move**.
The answer is drawn on a board with an arrow. Under "Castling, en passant, depth" you
can set what a photo can't show. **Or enter a FEN** analyses a typed position.

The live camera view needs HTTPS (or `localhost`); over plain HTTP the button opens
the phone's camera app instead. Photos are shrunk to 1600 px and re-encoded as JPEG
in the browser before upload, which also strips EXIF data such as location.

## API

### `GET /move?fen=<FEN>&depth=<1-25>`

`depth` is optional (default 15; the maximum is set by `MAX_DEPTH`).

```json
{"best": "e7e5", "san": "e5", "eval": 0.2, "mate": null, "depth": 18,
 "fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"}
```

- `eval` is in pawns from White's point of view. When the engine finds a forced
  mate, `eval` is `null` and `mate` is the number of moves to mate
  (positive = White mates, negative = Black mates).
- Each search is also capped by wall-clock time (`ENGINE_MAX_SECONDS`), so on slow
  hardware the returned `depth` can be lower than requested.

### `POST /move` (photo)

`multipart/form-data` with:

| Field      | Required | Meaning                                                         |
|------------|----------|-----------------------------------------------------------------|
| `image`    | yes      | Photo of the board, at most 10 MB (`MAX_UPLOAD_MB`)             |
| `turn`     | yes      | `w` or `b`, the side to move                                    |
| `castling` | no       | Castling rights still available, e.g. `KQkq`, `Kq`; default `-` |
| `ep`       | no       | En passant file `a`-`h` if the last move was a double pawn push |
| `depth`    | no       | As for `GET`                                                    |

Returns the same fields as `GET /move`, plus `detected` (the recognised piece
placement) and `confidence` (0-1). If the recognised position is illegal, the 422
error also includes `fen`, `detected` and `confidence` so the page can show it.

```sh
curl -F image=@board.jpg -F turn=w http://127.0.0.1:8000/move
```

### Errors

Errors are always JSON: `{"error": "<code>", "detail": "<message>"}`.

| Status | `error`              | When                                              |
|-------:|----------------------|---------------------------------------------------|
| 400    | `invalid_fen`        | The FEN can't be parsed                           |
| 422    | `illegal_position`   | Parses, but isn't a legal position (e.g. no king) |
| 422    | `game_over`          | Checkmate or stalemate, so there's no move        |
| 422    | `invalid_request`    | Missing or out-of-range parameter                 |
| 413    | `too_large`          | Upload bigger than `MAX_UPLOAD_MB`                |
| 415    | `unsupported_media_type` | `POST` body isn't `multipart/form-data`       |
| 501    | `recognition_unavailable` | Photo recognition isn't available           |
| 503    | `engine_unavailable` | Stockfish isn't installed or crashed              |

`GET /healthz` returns `{"ok": true}`.

## Setup

### Raspberry Pi (Raspberry Pi OS / Debian / Ubuntu)

```sh
sudo apt update
sudo apt install -y git python3-venv stockfish

sudo git clone https://github.com/<you>/bestmovechess.git /opt/bestmovechess
cd /opt/bestmovechess
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt

# Optional: change settings
sudo cp deploy/bestmovechess.env.example /etc/bestmovechess.env

sudo cp deploy/bestmovechess.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bestmovechess
systemctl status bestmovechess
journalctl -u bestmovechess -f      # logs
```

The service listens on `127.0.0.1:8000` only and runs as an unprivileged,
throwaway user (`DynamicUser=yes`). To update: `cd /opt/bestmovechess && sudo git pull
&& sudo .venv/bin/pip install -r requirements.txt && sudo systemctl restart bestmovechess`.

On a Pi 5, `ENGINE_THREADS=3` or `4` makes searches faster at the cost of
keeping more cores busy.

### Any other machine (development)

Install Stockfish (`brew install stockfish`, `apt install stockfish`, or download it
and set `STOCKFISH_PATH`), then:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --port 8000 --no-access-log
```

Run the tests with `.venv/bin/pip install pytest httpx && .venv/bin/python -m pytest`.

### Docker

```sh
docker build -t bestmovechess .
docker run -d --name bestmovechess --restart unless-stopped -p 127.0.0.1:8000:8000 bestmovechess
```

Pass settings with `-e`, e.g. `-e ENGINE_THREADS=4`. The image builds on both
amd64 and arm64.

### Configuration

All settings are environment variables (see `deploy/bestmovechess.env.example`):

| Variable             | Default                     | Meaning                                  |
|----------------------|-----------------------------|------------------------------------------|
| `HOST` / `PORT`      | `127.0.0.1` / `8000`        | Listen address (systemd unit only)       |
| `STOCKFISH_PATH`     | `stockfish` on `PATH`, else `/usr/games/stockfish` | Engine binary     |
| `ENGINE_THREADS`     | `2`                         | Stockfish threads                        |
| `ENGINE_HASH_MB`     | `64`                        | Stockfish hash table size                |
| `DEFAULT_DEPTH`      | `15`                        | Depth when the request doesn't give one  |
| `MAX_DEPTH`          | `25`                        | Highest depth a request may ask for      |
| `ENGINE_MAX_SECONDS` | `10`                        | Wall-clock cap per search                |
| `MAX_UPLOAD_MB`      | `10`                        | Upload size limit                        |
| `LOG_LEVEL`          | `INFO`                      | Python log level                         |

## Optional: private access with Tailscale

To reach the app from your own devices without opening it to the internet, run on
the machine hosting it:

```sh
tailscale serve --bg 8000
tailscale serve status     # shows the https://<machine>.<tailnet>.ts.net URL
```

This proxies HTTPS on your tailnet to `127.0.0.1:8000`, and the setting persists
across reboots. HTTPS also lets phone browsers use the camera. Use `tailscale serve`,
**not** `tailscale funnel`: funnel makes the app public on the internet. To turn it
off: `tailscale serve reset`.

## Privacy

Uploaded images are processed in memory only and never stored. The upload is parsed
in memory rather than with the framework's default parser, which would write large
files to a temporary file on disk. Image bytes are never logged.

Logs contain only the request method, path, status code, and timing. Query strings
and request bodies are never logged (uvicorn's own access log is disabled with
`--no-access-log`).
