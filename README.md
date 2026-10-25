# bestmovechess

Self-hosted "next best move" tool for chess. One small FastAPI app serves both the
web page and the API, and asks a local [Stockfish](https://stockfishchess.org/) for
the best move.

Runs well on a Raspberry Pi 5, and on any Linux/macOS machine or in Docker.

Photo recognition finds the board, corrects for perspective, and classifies each
square with a small pretrained CNN running on ONNX Runtime. It takes about a second
per photo and needs no GPU. See [How recognition works](#how-recognition-works).

## Web page

Open `http://127.0.0.1:8000/` (or your Tailscale URL).

1. Take a photo with **Take photo**, or pick one with **Upload image**. Choose the side
   to move, then tap **Read the board**.
2. **Check the detected position.** The page shows the recognised board, its
   confidence, and your photo with the detected board outlined. Squares the model
   is unsure about are dashed. Tap any square to choose a different piece; the
   model's next-best guesses are marked with percentages. **Rotate** turns the
   position 90° if the board was photographed from the side or from Black's end.
   **Reset** undoes your changes.
3. Tap **Get best move**. If you didn't change anything, the answer appears straight
   away (it was computed with the photo). The move is drawn as an arrow.

Under "Castling, en passant, depth" you can set what a photo can't show: castling
and en passant default to none, and castling options that the position rules out
are disabled. With no photo, enter a FEN or tap **Set up a position by hand** and
edit the board directly.

The live camera view needs HTTPS (or `localhost`); over plain HTTP the button opens
the phone's camera app instead. Photos are shrunk to 1600 px and re-encoded as JPEG
in the browser before upload, which also strips EXIF data such as location.

## API

Full reference: **[docs/API.md](docs/API.md)**. Interactive docs are at `/docs`.

```sh
# Best move for a FEN
curl -G http://127.0.0.1:8000/move --data-urlencode 'fen=rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1'
# {"best":"c7c5","san":"c5","eval":0.32,"mate":null,"depth":15,"fen":"..."}

# Best move for a photo (plus the detected position and its confidence)
curl -F image=@board.jpg -F turn=w http://127.0.0.1:8000/move
# {"best":"c6d5","san":"Bd5",...,"detected":"1q1r4/r3b1k1/...","confidence":1.0,...}
```

Errors are JSON: `{"error": "invalid_fen", "detail": "..."}`. There is no
authentication, so keep the app on localhost or a private network.

## Setup

### Raspberry Pi (Raspberry Pi OS / Debian / Ubuntu)

```sh
sudo apt update
sudo apt install -y git python3-venv stockfish

sudo git clone https://github.com/<you>/bestmovechess.git /opt/bestmovechess
cd /opt/bestmovechess
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo .venv/bin/python scripts/download_model.py   # 3.4 MB, checksum-verified

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
.venv/bin/python scripts/download_model.py
.venv/bin/uvicorn app.main:app --port 8000 --no-access-log
```

Run the tests with `.venv/bin/pip install pytest httpx && .venv/bin/python -m pytest`.

### Docker

```sh
docker build -t bestmovechess .
docker run -d --name bestmovechess --restart unless-stopped -p 127.0.0.1:8000:8000 bestmovechess
```

Pass settings with `-e`, e.g. `-e ENGINE_THREADS=4`. The image builds on both
amd64 and arm64, and downloads the model during the build.

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
| `MODEL_PATH`         | `models/square_cnn.onnx`    | Recognition model file                   |
| `VISION_THREADS`     | `2`                         | ONNX Runtime threads                     |
| `MAX_IMAGE_PIXELS`   | `50000000`                  | Refuse to decode larger images           |
| `LOG_LEVEL`          | `INFO`                      | Python log level                         |

## How recognition works

`app/vision/` runs on the uploaded bytes in memory:

1. **Board detection.** It finds the "X-corners" where four squares meet and fits
   them to an 8×8 grid with RANSAC. That gives a perspective homography, so photos
   taken at an angle work. If that fails, it falls back to quadrilateral contours.
2. **Square crops.** It cuts an upright 32×64 crop for each square, tall enough to
   include the piece.
3. **Classification.** A 0.84M-parameter CNN (ONNX) labels each crop as empty or one
   of the 12 pieces.
4. **Orientation.** h1 is a light square, and White is assumed to be nearer the
   camera.
5. **Legality repair.** Using the model's probabilities, it makes the cheapest
   changes that give one king per side, no pawns on the back ranks, and at most 8
   pawns per side. Any change is reported in `repairs`.

On 104 real photos the model was not trained on (the held-out part of
[samryan18/chess-dataset](https://github.com/samryan18/chess-dataset)), it got 99.3%
of squares and 92% of whole boards right. Every board scoring `confidence` ≥ 0.8 was
fully correct, 92% of those between 0.5 and 0.8 were, and 56% of those below 0.5 were.
The thresholds were tuned on those same photos, so treat them as a guide. Accuracy is
lower with unusual piece sets, steep angles, glare, or a board rotated diagonally in
the frame. **Always check the detected position before trusting the move.**

Tips: shoot from above or from White's side, fill most of the frame with the board,
and avoid strong reflections.

### Credits

- Recognition model and the detection/cropping code in `app/vision/`:
  [ChessLens](https://github.com/zeljko1611/ChessLens) by zeljko1611, MIT License
  (see `app/vision/LICENSE`). The model file is downloaded from a pinned commit and
  checked against its SHA-256.
- ChessLens was trained on renders from [chesscog](https://osf.io/xf3ka/) (Wölflein &
  Arandjelović, CC BY 4.0) and photos from
  [samryan18/chess-dataset](https://github.com/samryan18/chess-dataset) (MIT).
- Piece shapes: [Noto Sans Symbols 2](https://github.com/notofonts/symbols) outlines
  (SIL Open Font License 1.1, see `static/pieces-OFL.txt`). They are drawn as SVG
  paths, so they look the same on every device and dark mode can't recolour them.
- [Stockfish](https://stockfishchess.org/) (GPLv3) is installed separately and run as
  a separate program.

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
files to a temporary file on disk. The image is decoded with OpenCV straight from
memory, and nothing in the recognition pipeline writes files. Image bytes are never
logged. The tests fail if an upload ever touches a temporary file.

Logs contain only the request method, path, status code, and timing. Query strings
and request bodies are never logged (uvicorn's own access log is disabled with
`--no-access-log`).
