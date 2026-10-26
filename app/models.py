"""Move models: which engine answers "what's the best move?".

A model is any engine that speaks UCI, plus how to search with it:

- ``stockfish``: the default, full-strength alpha-beta search. Add ``elo`` to make it play
  at a human rating (Stockfish's own UCI_LimitStrength), which works with nothing extra
  installed.
- ``maia-1100`` ... ``maia-1900``: Maia (https://www.maiachess.com), a network trained on
  millions of human games to predict the move a player of that rating would make. It runs
  inside Leela Chess Zero (lc0) with one node of search, as its authors recommend, so its
  answer is the human-like move rather than the strongest one. Needs lc0 and the weights.
- your own: any UCI engine (Rodent IV, a Komodo or HIARCS you own, ...) listed in the
  UCI_ENGINES environment variable.

Nothing here is bundled: engines are separate programs you install (see the README).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field

from . import config

log = logging.getLogger("bestmovechess.models")

DEFAULT = "stockfish"
MAIA_ELOS = tuple(range(1100, 2000, 100))
# Stockfish's UCI_Elo range (Stockfish 16+); outside it the engine rejects the setting.
MIN_ELO, MAX_ELO = 1320, 3190
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")


class UnknownModel(ValueError):
    pass


class ModelUnavailable(RuntimeError):
    """A real model that isn't installed on this server."""


@dataclass(frozen=True)
class ModelSpec:
    name: str
    label: str
    description: str
    command: tuple[str, ...] = ()
    options: dict = field(default_factory=dict)
    search: str = "depth"          # "depth": honour the depth parameter; "nodes1": one node, no lookahead
    human_like: bool = False
    supports_elo: bool = False
    missing: str = ""              # why it can't run here, empty when it can

    @property
    def installed(self) -> bool:
        return not self.missing


def _stockfish() -> ModelSpec:
    path = config.STOCKFISH_PATH
    found = shutil.which(path) or (path if os.path.isfile(path) else None)
    return ModelSpec(
        DEFAULT, "Stockfish", "Strongest play. Add elo=1320-3190 for a human-rated Stockfish.",
        command=(found or path,), supports_elo=True,
        missing="" if found else f"Stockfish not found at {path!r}",
    )


def _maia(elo: int) -> ModelSpec:
    lc0 = config.LC0_PATH or shutil.which("lc0") or ""
    weights = os.path.join(config.MAIA_DIR, f"maia-{elo}.pb.gz")
    missing = ""
    if not lc0 or not (os.path.exists(lc0) or shutil.which(lc0)):
        missing = "lc0 (Leela Chess Zero) isn't installed; set LC0_PATH"
    elif not os.path.isfile(weights):
        missing = f"Maia weights not found at {weights}"
    options = {"Backend": config.LC0_BACKEND} if config.LC0_BACKEND else {}
    return ModelSpec(
        f"maia-{elo}", f"Maia {elo}",
        f"Predicts the move a ~{elo}-rated human would play (human-like, not strongest).",
        command=(lc0 or "lc0", f"--weights={weights}"), options=options,
        search="nodes1", human_like=True, missing=missing,
    )


def _custom() -> dict[str, ModelSpec]:
    raw = config.UCI_ENGINES.strip()
    if not raw:
        return {}
    try:
        entries = json.loads(raw)
        if not isinstance(entries, dict):
            raise ValueError("must be a JSON object")
    except ValueError as exc:
        log.error("UCI_ENGINES ignored: %s", exc)
        return {}
    out = {}
    for name, entry in entries.items():
        try:
            if not NAME_RE.match(name) or name == DEFAULT or name.startswith("maia-"):
                raise ValueError("bad or reserved name")
            path = entry["path"]
            found = shutil.which(path) or (path if os.path.isfile(path) else None)
            search = entry.get("search", "depth")
            if search not in ("depth", "nodes1"):
                raise ValueError("search must be depth or nodes1")
            out[name] = ModelSpec(
                name, entry.get("label", name), entry.get("description", "Custom UCI engine."),
                command=(found or path, *map(str, entry.get("args", []))),
                options=dict(entry.get("options", {})), search=search,
                human_like=bool(entry.get("human_like", False)),
                missing="" if found else f"engine not found at {path!r}",
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.error("UCI_ENGINES entry %r ignored: %s", name, exc)
    return out


def all_models() -> dict[str, ModelSpec]:
    """Every model this server knows about, installed or not (checked now, not cached)."""
    models = {DEFAULT: _stockfish()}
    models.update({f"maia-{e}": _maia(e) for e in MAIA_ELOS})
    models.update(_custom())
    return models


def resolve(name: str | None) -> ModelSpec:
    """The spec for `name` (default model when empty). UnknownModel / ModelUnavailable."""
    name = (name or DEFAULT).strip().lower()
    models = all_models()
    if name not in models:
        raise UnknownModel(f"unknown model {name!r}; see GET /models")
    spec = models[name]
    if not spec.installed and name != DEFAULT:   # a missing Stockfish stays an engine_unavailable error
        raise ModelUnavailable(f"model {name!r} isn't installed on this server: {spec.missing}")
    return spec
