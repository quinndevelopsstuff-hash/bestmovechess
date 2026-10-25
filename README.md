# bestmovechess

Self-hosted "next best move" tool for chess. One small FastAPI app serves both the
web page and the API, and asks a local [Stockfish](https://stockfishchess.org/) for
the best move.

Runs well on a Raspberry Pi 5, and on any Linux/macOS machine or in Docker.

Screenshots of a chess app are read by [chessfen](https://github.com/sunfmin/chessfen)
(pure pixel math, no ML model). Camera photos of a real board are read by a small
pretrained CNN running on ONNX Runtime, which finds the board and corrects for
perspective. Neither needs a GPU. See [How recognition works](#how-recognition-works).

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

Under "Castling, en passant, image type, depth" you can set what a photo can't show:
castling and en passant default to none, and castling options that the position rules
out are disabled. **Image type** is normally auto-detected; set it to Screenshot or
Camera photo to force one recognizer. With no photo, enter a FEN or tap **Set up a
position by hand** and edit the board directly.

For screenshots, chessfen guesses which side is at the bottom, and the correction board
is drawn the same way round. If the guess is wrong, tick **Board is shown from Black's
side** and read the board again (or use Rotate).

Tick **Show diagram** to also show a server-drawn picture of the position and best
move under the result (using `img_out`, see the API). The picture is redrawn for
positions you've edited, and an explanatory image appears when something goes wrong.

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

# Same, plus image_url: a diagram of the detected board with the best move drawn
curl -F image=@board.jpg -F turn=w -F img_out=true https://<your-host>/move
# {...,"image_url":"https://<your-host>/img?fen=...&best=c6d5&turn=w&eval=1.74"}

# Draw any position yourself (PNG by default, or format=svg)
curl -G https://<your-host>/img -o move.png \
  --data-urlencode 'fen=r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3' -d best=f1b5 -d eval=0.45

# An image explaining an error, with the offending squares marked when a FEN is given
curl -G https://<your-host>/img/error -o error.png -d code=missing_king \
  --data-urlencode 'fen=8/8/8/8/8/8/8/K7 w - - 0 1'
```

`img_out` (`true`/`false`, default `false`) adds `image_url` to the `POST /move`
response. If the request fails, the JSON error gets an `image_url` pointing at
`/img/error` instead. Links are absolute. They use `PUBLIC_BASE_URL` if it's set;
otherwise they're built from the `X-Forwarded-Proto`/`X-Forwarded-Host` headers that
`tailscale serve` sends, or from the `Host` header.

Errors are JSON: `{"error": "invalid_fen", "detail": "..."}`. There is no
authentication, so keep the app on localhost or a private network.

## Setup

### Raspberry Pi (Raspberry Pi OS / Debian / Ubuntu)

Screenshot recognition (chessfen) needs **Python 3.13 or newer**. Raspberry Pi OS
"Trixie" has it; the older "Bookworm" has Python 3.11. Check with `python3 --version`.
On 3.11 everything else works: pip skips chessfen, and screenshots are read by the
photo model instead (much less accurately).

```sh
sudo apt update
sudo apt install -y git python3-venv stockfish libcairo2 fonts-dejavu-core   # git is needed to install chessfen

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

Install Stockfish and the Cairo library used to draw PNG diagrams
(`brew install stockfish cairo`, or `apt install stockfish libcairo2 fonts-dejavu-core`;
you can also download Stockfish and set `STOCKFISH_PATH`), then:

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

Pass settings with `-e`, e.g. `-e ENGINE_THREADS=4`. The image (Python 3.13) builds on
both amd64 and arm64, and downloads the model during the build.

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
| `PUBLIC_BASE_URL`    | unset                       | Base for absolute links such as `image_url`, e.g. `https://chess.example.ts.net` |
| `LOG_LEVEL`          | `INFO`                      | Python log level                         |

## How recognition works

There are two recognizers. With the default `source=auto`, the app picks one per upload:
an image that contains a flat digital board (a screenshot) goes to chessfen, and anything
else goes to the photo model. If chessfen can't read a screenshot, the photo model gets
it. `source=screenshot` or `source=camera` forces one.

**Is it a screenshot?** (`app/vision/board_finder.py`). The app searches for an
axis-aligned 8×8 grid whose same-coloured squares are practically identical, with
crisp grid lines. A camera photo never passes this test: none of 104 real photos did,
because lighting and perspective always vary. Screenshots pass, including
heavily compressed JPEGs.

**Screenshots** (`app/vision/screenshot.py`):
[chessfen](https://github.com/sunfmin/chessfen) by sunfmin, which uses pure pixel math
and no ML model. The board found by the checker search is cropped out first, with a
small margin, so avatars, move lists and buttons stay out of the way. If the crop can't
be read, chessfen gets the whole screenshot. chessfen matches each square's piece
silhouette against python-chess's piece shapes and decides the colour by comparing the
piece with the light around it. Squares it isn't sure about come back in `uncertain`
(and with a confidence below 0.6 in `squares`), and they're shaded amber on the
correction board and in error images. A doubtful reading is never hidden or swapped
for the photo model's, because the photo model reads flat screenshots far worse. Only
the piece placement is taken from chessfen. The FEN's side to move, castling rights
(none unless you tick them) and en passant come from your choices. chessfen guesses
whether Black is at the bottom from where each side's pieces stand; the `flip` field
overrides it (response: `flipped`, and `orientation` is `chessfen` or `requested`).

On a chess.com screenshot (1.e4 e5, dark theme) it read 56 of 64 squares, and all 8
mistakes were among the squares it flagged as doubtful. chessfen's templates are the
"cburnett" pieces (lichess's default), so other piece sets are read less reliably;
always check the doubtful squares. It takes about 1–2 seconds per screenshot on a
desktop CPU, and more on a Pi.

**Photos** (`app/vision/recognizer.py`). This is the pretrained model:

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

### Measuring recognition

`python -m app.evaluate` runs the recognizer over local fixtures and prints per-square
accuracy, accuracy by piece type, a confusion table, and how many wrong squares were
flagged as uncertain. Add `--source screenshot` or `--source camera` to test one
recognizer. A fixture is an image in
`tests/fixtures/` plus a `.fen` sidecar with the expected piece placement; see
`tests/fixtures/README.md`. Fixtures are gitignored and never committed, since
screenshots can contain third-party artwork.

### Credits

- Screenshot recognition: [chessfen](https://github.com/sunfmin/chessfen) by sunfmin,
  GPL-3.0, installed by pip from a pinned commit (`requirements.txt`).
- [python-chess](https://github.com/niklasf/python-chess) by Niklas Fiekas,
  GPL-3.0-or-later: board logic, FEN handling and the diagrams.
- Photo recognition model and the detection/cropping code in `app/vision/`:
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

## License

bestmovechess is free software under the **GNU General Public License, version 3 or
(at your option) any later version** (GPL-3.0-or-later); see `LICENSE`. In short: you
can use, change and self-host it freely, and if you distribute a modified version, you
must make its source available under the same license. It builds on chessfen (GPL-3.0)
and python-chess (GPL-3.0-or-later); `NOTICE` credits these and the other third-party
parts.

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

Screenshots are handed to chessfen as in-memory arrays. It doesn't read or write files
while recognizing, and the tests fail if it ever does.

Output images are drawn from the position on request and are never stored. Diagrams
from `/img` and `/img/error` are generated in memory from the FEN alone. They never
use, crop or draw on your photo, and the `/img` endpoints don't read or write files
(apart from system fonts).

Logs contain only the request method, path, status code, and timing. Query strings
and request bodies are never logged (uvicorn's own access log is disabled with
`--no-access-log`).
