"""UCI engine processes shared by all requests: one per model, started on first use.

Each engine is used by one search at a time (its own lock); different models can search
at the same time. At most MAX_LIVE_ENGINES non-default engines stay running, so trying
several Maia nets doesn't pile up processes on a small board.
"""

import asyncio
import logging
from collections import OrderedDict

import chess
import chess.engine

from . import config, models

log = logging.getLogger("bestmovechess.engine")


class EngineUnavailable(RuntimeError):
    pass


class _Process:
    def __init__(self, spec: models.ModelSpec) -> None:
        self.spec = spec
        self.engine: chess.engine.Protocol | None = None
        self.transport: asyncio.SubprocessTransport | None = None
        self.lock = asyncio.Lock()

    async def start(self) -> None:
        try:
            self.transport, self.engine = await chess.engine.popen_uci(list(self.spec.command))
        except (FileNotFoundError, PermissionError, chess.engine.EngineError) as exc:
            raise EngineUnavailable(f"cannot start {self.spec.name} ({self.spec.command[0]!r}): {exc}") from exc
        options = dict(self.spec.options)
        if self.spec.name == models.DEFAULT:
            options.update({"Threads": config.ENGINE_THREADS, "Hash": config.ENGINE_HASH_MB})
        await self.engine.configure({k: v for k, v in options.items() if k in self.engine.options})
        log.info("%s started: %s", self.spec.name, self.engine.id.get("name", "unknown"))

    async def stop(self) -> None:
        if self.engine is not None:
            try:
                await asyncio.wait_for(self.engine.quit(), timeout=2)
            except Exception:
                if self.transport is not None:
                    self.transport.close()
        self.engine = None
        self.transport = None


class Engine:
    """The pool. `start()` launches the default model so a missing Stockfish is logged early."""

    def __init__(self) -> None:
        self._procs: OrderedDict[str, _Process] = OrderedDict()   # least recently used first
        self._pool_lock = asyncio.Lock()

    async def start(self) -> None:
        await self._get(models.resolve(models.DEFAULT))

    async def stop(self) -> None:
        async with self._pool_lock:
            procs, self._procs = list(self._procs.values()), OrderedDict()
        for proc in procs:
            await proc.stop()

    async def _get(self, spec: models.ModelSpec) -> _Process:
        async with self._pool_lock:
            proc = self._procs.get(spec.name)
            if proc is None or proc.spec != spec:   # new, or its settings changed
                proc = self._procs[spec.name] = _Process(spec)
            self._procs.move_to_end(spec.name)
            idle = [p for n, p in self._procs.items() if n != models.DEFAULT and n != spec.name and not p.lock.locked()]
            extra = len([n for n in self._procs if n != models.DEFAULT]) - config.MAX_LIVE_ENGINES
            evicted = idle[:max(0, extra)]
            for p in evicted:
                self._procs.pop(p.spec.name, None)
        for p in evicted:
            await p.stop()
        return proc

    async def analyse(self, spec: models.ModelSpec, board: chess.Board, depth: int,
                      elo: int | None = None) -> tuple[chess.Move, chess.engine.InfoDict]:
        """The model's chosen move and its info (score, depth). Raises EngineUnavailable."""
        if spec.search == "nodes1":
            limit = chess.engine.Limit(nodes=1, time=config.ENGINE_MAX_SECONDS)
        else:
            limit = chess.engine.Limit(depth=depth, time=config.ENGINE_MAX_SECONDS)
        options = {"UCI_LimitStrength": True, "UCI_Elo": elo} if elo else {}
        proc = await self._get(spec)
        async with proc.lock:
            for attempt in range(2):
                if proc.engine is None:
                    await proc.start()
                try:
                    # play() (not analyse()) so the move is the one the engine would really
                    # play: at a limited Elo that is not always the top line.
                    played = await proc.engine.play(board, limit, info=chess.engine.INFO_ALL, options=options)
                    if played.move is None:
                        raise EngineUnavailable(f"{spec.name} returned no move")
                    return played.move, played.info
                except (chess.engine.EngineTerminatedError, chess.engine.EngineError):
                    # Engine crashed or got wedged: restart once, then give up.
                    log.warning("%s failed (attempt %d), restarting", spec.name, attempt + 1)
                    await proc.stop()
            raise EngineUnavailable(f"{spec.name} failed twice in a row")
