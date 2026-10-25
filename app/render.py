"""Board diagrams drawn from a position (python-chess SVG -> PNG with cairosvg).

Everything is built in memory from validated chess data; nothing is read from or written
to disk, and no request text is ever drawn (captions and messages come from fixed tables).
"""

import logging
from xml.sax.saxutils import escape

import chess
import chess.svg

# Imported once at startup (it reads its version file on import), never during a request.
try:
    import cairosvg
except (ImportError, OSError) as exc:   # OSError: the libcairo2 system library is missing
    cairosvg = None
    logging.getLogger("bestmovechess").warning("PNG output disabled, serving SVG instead: %s", exc)

DEFAULT_SIZE = 480
MIN_SIZE, MAX_SIZE = 160, 1024
FONT = "DejaVu Sans, Verdana, Arial, sans-serif"

ARROW = "#15781B"
FROM_FILL = "#f7ec5a99"
TO_FILL = "#f7ec5acc"
BAD_FILL = "#e5484dcc"
UNSURE_FILL = "#f5a524b3"   # squares the recognizer was unsure about

# Error images: code -> (title, default detail). Unknown codes fall back to "error".
ERRORS = {
    "board_not_found": ("No board found", "Couldn't find a chessboard in the photo. Fill the frame with the board and avoid glare."),
    "invalid_image": ("Image unreadable", "The upload isn't a JPEG, PNG or WebP image."),
    "recognition_unavailable": ("Board unreadable", "Photo recognition isn't available on this server."),
    "illegal_position": ("Illegal position", "This position can't occur in a game."),
    "missing_king": ("Missing king", "Each side needs exactly one king."),
    "too_many_kings": ("Too many kings", "Each side needs exactly one king."),
    "pawn_on_back_rank": ("Pawn on back rank", "Pawns can't stand on the first or eighth rank."),
    "too_many_pieces": ("Too many pieces", "A side has more pieces or pawns than possible."),
    "opposite_check": ("Wrong side in check", "The side that just moved can't be in check."),
    "impossible_check": ("Impossible check", "No legal move could give this check."),
    "bad_castling": ("Castling doesn't fit", "Castling rights don't match the king and rook squares."),
    "bad_en_passant": ("Invalid en passant", "No pawn could have just moved two squares there."),
    "game_over": ("No legal moves", "The game is over."),
    "illegal_move": ("Illegal move", "That move isn't legal in this position."),
    "invalid_fen": ("Invalid FEN", "The position couldn't be read."),
    "engine_unavailable": ("Engine error", "The chess engine isn't available right now."),
    "invalid_request": ("Bad request", "A parameter is missing or out of range."),
    "too_large": ("Image too large", "The photo is over the upload limit."),
    "error": ("Something went wrong", "The request couldn't be completed."),
}
POSITION_ERRORS = {"illegal_position", "missing_king", "too_many_kings", "pawn_on_back_rank", "too_many_pieces",
                   "opposite_check", "impossible_check", "bad_castling", "bad_en_passant", "game_over", "illegal_move"}


def side(color: chess.Color) -> str:
    return "White" if color == chess.WHITE else "Black"


def diagnose(board: chess.Board) -> tuple[str, str, list[int]] | None:
    """For an impossible or finished position: (error code, detail, squares to highlight)."""
    status = board.status()
    pieces = lambda piece, color: list(board.pieces(piece, color))  # noqa: E731
    for color in (chess.WHITE, chess.BLACK):
        kings = pieces(chess.KING, color)
        if not kings:
            return "missing_king", f"{side(color)} has no king.", []
        if len(kings) > 1:
            return "too_many_kings", f"{side(color)} has {len(kings)} kings.", kings
    if status & chess.STATUS_PAWNS_ON_BACKRANK:
        squares = [s for s in chess.SquareSet(chess.BB_BACKRANKS) if board.piece_type_at(s) == chess.PAWN]
        return "pawn_on_back_rank", "Pawns can't stand on the first or eighth rank.", squares
    for color, flags in ((chess.WHITE, chess.STATUS_TOO_MANY_WHITE_PIECES | chess.STATUS_TOO_MANY_WHITE_PAWNS),
                         (chess.BLACK, chess.STATUS_TOO_MANY_BLACK_PIECES | chess.STATUS_TOO_MANY_BLACK_PAWNS)):
        if status & flags:
            return "too_many_pieces", f"{side(color)} has more pieces or pawns than possible.", []
    if status & chess.STATUS_OPPOSITE_CHECK:
        them = not board.turn
        king = board.king(them)
        return ("opposite_check", f"{side(them)} is in check, but it's {side(board.turn)}'s move.",
                [king, *board.attackers(board.turn, king)])
    if status & (chess.STATUS_TOO_MANY_CHECKERS | chess.STATUS_IMPOSSIBLE_CHECK | chess.STATUS_RACE_CHECK):
        king = board.king(board.turn)
        return "impossible_check", "No legal move could give this check.", [king, *board.checkers()]
    if status & chess.STATUS_BAD_CASTLING_RIGHTS:
        bad = board.castling_rights & ~board.clean_castling_rights()
        squares = list(chess.SquareSet(bad))  # the rook squares named by the bad rights
        squares += [king for king, rank in ((chess.E1, chess.BB_RANK_1), (chess.E8, chess.BB_RANK_8)) if bad & rank]
        return "bad_castling", "Castling rights don't match the king and rook squares.", squares
    if status & chess.STATUS_INVALID_EP_SQUARE:
        return "bad_en_passant", "No pawn could have just moved two squares there.", [board.ep_square] if board.ep_square is not None else []
    if status != chess.STATUS_VALID:
        return "illegal_position", "This position can't occur in a game.", []
    if board.is_checkmate():
        king = board.king(board.turn)
        return "game_over", f"Checkmate: {side(not board.turn)} wins.", [king, *board.checkers()]
    if board.is_stalemate():
        return "game_over", f"Stalemate: {side(board.turn)} has no legal moves.", [board.king(board.turn)]
    return None


def format_eval(eval_: float | None, mate: int | None) -> str:
    if mate is not None:
        return f"Mate in {abs(mate)} for {'White' if mate > 0 else 'Black'}"
    if eval_ is not None:
        return f"{eval_:+.2f}" if round(eval_, 2) else "0.00"
    return ""


def _board_svg(board: chess.Board, size: int, move: chess.Move | None = None, bad: list[int] = (),
               unsure: list[int] = ()) -> str:
    fill = {sq: UNSURE_FILL for sq in unsure}
    arrows = []
    if move is not None:
        fill.update({move.from_square: FROM_FILL, move.to_square: TO_FILL})
        arrows = [chess.svg.Arrow(move.from_square, move.to_square, color=ARROW)]
    for sq in bad:
        if sq is not None:
            fill[sq] = BAD_FILL
    check = board.king(board.turn) if board.is_valid() and board.is_check() else None
    return chess.svg.board(board, orientation=board.turn, fill=fill, arrows=arrows, check=check,
                           size=size, coordinates=True)


def _wrap(text: str, width: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    return lines + [line] if line else lines


def _page(size: int, body: str, lines: list[tuple[str, int, str, str]], board_h: int) -> str:
    """Wrap `body` (height board_h) with caption lines below: (text, font px, weight, colour)."""
    pad = round(size * 0.035)
    height = board_h + pad
    texts = []
    for text, px, weight, colour in lines:
        height += round(px * 1.35)
        texts.append(f'<text x="{size / 2}" y="{height - round(px * 0.35)}" text-anchor="middle" '
                     f'font-family="{FONT}" font-size="{px}" font-weight="{weight}" fill="{colour}">{escape(text)}</text>')
    height += pad
    return (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'width="{size}" height="{height}" viewBox="0 0 {size} {height}">'
            f'<rect width="100%" height="100%" fill="#ffffff"/>{body}{"".join(texts)}</svg>')


def board_image(board: chess.Board, move: chess.Move | None, eval_: float | None, mate: int | None,
                size: int = DEFAULT_SIZE) -> str:
    """SVG of the position with the best move drawn, and a caption underneath."""
    big, small = round(size * 0.05), round(size * 0.035)
    if move is not None:
        caption = f"Best: {board.san(move)}"
        score = format_eval(eval_, mate)
        lines = [(caption + (f"  ·  {score}" if score else ""), big, "bold", "#1d1d1f")]
    else:
        found = diagnose(board)
        lines = [(found[1] if found else "No move given", big, "bold", "#1d1d1f")]
    lines.append((f"{side(board.turn)} to move", small, "normal", "#6b6b70"))
    return _page(size, _board_svg(board, size, move), lines, size)


def error_image(code: str, board: chess.Board | None = None, size: int = DEFAULT_SIZE,
                move: chess.Move | None = None, uncertain: list[int] = ()) -> str:
    """SVG explaining an error; draws the position (offending squares red, squares the
    recognizer was unsure about amber) when one is given."""
    code = code if code in ERRORS else "error"
    title, detail = ERRORS[code]
    big, small = round(size * 0.055), round(size * 0.036)
    if board is not None and code in POSITION_ERRORS:
        bad: list[int] = []
        found = diagnose(board)
        if found and (code in ("illegal_position", "game_over") or found[0] == code):
            if code == "illegal_position":
                title = ERRORS[found[0]][0]
            detail, bad = found[1], found[2]
        if code == "illegal_move" and move is not None:
            bad = [move.from_square, move.to_square]
        lines = [(title, big, "bold", "#b3261e")] + [(t, small, "normal", "#1d1d1f") for t in _wrap(detail, 52)]
        if uncertain:
            lines.append(("Amber squares were hard to read: check them first.", small, "normal", "#8a5a00"))
        return _page(size, _board_svg(board, size, bad=bad, unsure=uncertain), lines, size)

    # No board: a simple warning mark above the message.
    r = size * 0.11
    cx, cy = size / 2, size * 0.2
    mark = (f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="#b3261e"/>'
            f'<rect x="{cx - r * 0.11}" y="{cy - r * 0.6}" width="{r * 0.22}" height="{r * 0.75}" rx="{r * 0.1}" fill="#fff"/>'
            f'<circle cx="{cx}" cy="{cy + r * 0.45}" r="{r * 0.13}" fill="#fff"/>')
    lines = [(title, big, "bold", "#b3261e")] + [(t, small, "normal", "#1d1d1f") for t in _wrap(detail, 48)]
    return _page(size, mark, lines, round(size * 0.31))


def to_png(svg: str) -> bytes | None:
    """PNG bytes, or None when cairo isn't installed (callers then serve the SVG)."""
    if cairosvg is None:
        return None
    return cairosvg.svg2png(bytestring=svg.encode("utf-8"))
