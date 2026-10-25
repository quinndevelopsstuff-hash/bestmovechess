"""Photo -> board position. Works on in-memory bytes only; never writes the image anywhere."""


class RecognitionUnavailable(RuntimeError):
    pass


def recognize(image: bytes) -> tuple[str, float]:
    """Return (FEN piece placement, confidence 0-1) for a photo of a board."""
    raise RecognitionUnavailable("photo recognition is not installed yet")
