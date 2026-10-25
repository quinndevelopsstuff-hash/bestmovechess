"""A single long-lived Stockfish process shared by all requests."""

import asyncio
import logging

import chess
import chess.engine

from . import config

log = logging.getLogger("bestmovechess.engine")


class EngineUnavailable(RuntimeError):
    pass


class Engine:
    def __init__(self) -> None:
        self._engine: chess.engine.Protocol | None = None
        self._transport: asyncio.SubprocessTransport | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        try:
            self._transport, self._engine = await chess.engine.popen_uci(config.STOCKFISH_PATH)
        except (FileNotFoundError, PermissionError, chess.engine.EngineError) as exc:
            raise EngineUnavailable(f"cannot start Stockfish at {config.STOCKFISH_PATH!r}: {exc}") from exc
        options = {"Threads": config.ENGINE_THREADS, "Hash": config.ENGINE_HASH_MB}
        await self._engine.configure({k: v for k, v in options.items() if k in self._engine.options})
        log.info("Stockfish started: %s", self._engine.id.get("name", "unknown"))

    async def stop(self) -> None:
        if self._engine is not None:
            try:
                await asyncio.wait_for(self._engine.quit(), timeout=2)
            except Exception:
                if self._transport is not None:
                    self._transport.close()
        self._engine = None
        self._transport = None

    async def analyse(self, board: chess.Board, depth: int) -> chess.engine.InfoDict:
        limit = chess.engine.Limit(depth=depth, time=config.ENGINE_MAX_SECONDS)
        async with self._lock:
            for attempt in range(2):
                if self._engine is None:
                    await self.start()
                try:
                    return await self._engine.analyse(board, limit)
                except (chess.engine.EngineTerminatedError, chess.engine.EngineError):
                    # Engine crashed or got wedged: restart once, then give up.
                    log.warning("Stockfish failed (attempt %d), restarting", attempt + 1)
                    await self.stop()
            raise EngineUnavailable("Stockfish failed twice in a row")
