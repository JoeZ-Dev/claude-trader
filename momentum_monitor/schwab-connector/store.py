"""
Durable bar storage for schwab-connector.

Append-only JSON Lines, one file per symbol, under a directory that
docker-compose mounts as a volume. This is what makes the definition-of-done
claim "previously-captured bars are still present after a restart" true: the
process is stateless, the volume is not.

Bars are stored and returned exactly on the specs.md section 4 contract:

    {"ts": int, "open": float, "high": float, "low": float,
     "close": float, "volume": float, "is_extended": bool}

`since(symbol, since_ts)` uses an INCLUSIVE lower bound (ts >= since_ts).
"Since" reads most naturally as "at or after"; the polling client is
responsible for advancing its cursor (e.g. asking for last_seen_ts + 1, or
de-duplicating the boundary bar by ts).
"""
from __future__ import annotations

import json
from pathlib import Path


class BarStore:
    def __init__(self, dir_path) -> None:
        self._dir = Path(dir_path)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, list[dict]] = {}

    def append(self, symbol: str, bar: dict) -> None:
        key = symbol.upper()
        bars = self._load(key)
        with self._path(key).open("a") as f:
            f.write(json.dumps(bar, separators=(",", ":")) + "\n")
        bars.append(bar)

    def append_many(self, symbol: str, bars) -> None:
        for b in bars:
            self.append(symbol, b)

    def since(self, symbol: str, since_ts: float) -> list[dict]:
        return [b for b in self._load(symbol.upper()) if b["ts"] >= since_ts]

    def symbols(self) -> list[str]:
        return sorted(p.stem for p in self._dir.glob("*.jsonl"))

    # -- internals ---------------------------------------------------------

    def _path(self, key: str) -> Path:
        return self._dir / f"{key}.jsonl"

    def _load(self, key: str) -> list[dict]:
        if key not in self._cache:
            bars: list[dict] = []
            p = self._path(key)
            if p.exists():
                for line in p.read_text().splitlines():
                    line = line.strip()
                    if line:
                        bars.append(json.loads(line))
            self._cache[key] = bars
        return self._cache[key]
