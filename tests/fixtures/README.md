# Local recognition fixtures

Everything in this folder except this README is gitignored. Fixtures may contain
third-party artwork (for example chess.com piece sets), so they are never committed.

Add an image and a sidecar with the same name:

    tests/fixtures/chesscom-e4e5.png
    tests/fixtures/chesscom-e4e5.fen    # expected piece placement, e.g. rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR

Then run `python -m app.evaluate` (add `-v` to list every misread square).
