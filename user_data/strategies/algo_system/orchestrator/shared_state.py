"""
shared_state.py - Thread-safe shared state store for the LATS algo system.

Provides a dict-backed in-memory store keyed by (module_id, pair) with
optional JSON persistence.  All public methods are protected by a single
threading.Lock so the store is safe to use from Freqtrade's mixed
async/threaded callbacks.

T006 of the LATS system for Freqtrade.
"""

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

try:
    from typing import TypedDict
except ImportError:  # Python < 3.8 fallback (not expected in this project)
    from typing_extensions import TypedDict  # type: ignore[assignment]

logger = logging.getLogger("algo_system.shared_state")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class ModuleStateEntry(TypedDict):
    """State blob for one (module_id, pair) slot.

    Fields
    ------
    module_id : str
        Identifier of the algo module that owns this entry (e.g. "trend_detector").
    pair : str
        Market pair the entry belongs to (e.g. "BTC/USDT").
    updated_timestamp : int
        Unix timestamp (seconds) of the most recent :py:meth:`SharedState.set` call.
    data : dict
        Arbitrary module-specific payload.  Must be JSON-serialisable.
    """

    module_id: str
    pair: str
    updated_timestamp: int
    data: dict


# Alias used in type annotations below
_StoreKey = Tuple[str, str]  # (module_id, pair)

# Separator used when serialising the composite key to a JSON string.
_KEY_SEP = "|"


def _make_key(module_id: str, pair: str) -> str:
    """Encode a (module_id, pair) tuple as a single string for JSON storage."""
    if _KEY_SEP in module_id:
        raise ValueError(
            f"module_id must not contain the separator '{_KEY_SEP}'; got {module_id!r}"
        )
    return f"{module_id}{_KEY_SEP}{pair}"


def _parse_key(raw: str) -> _StoreKey:
    """Decode a serialised key back into a (module_id, pair) tuple."""
    module_id, _, pair = raw.partition(_KEY_SEP)
    if not pair:
        raise ValueError(f"Cannot parse key {raw!r}: missing separator '{_KEY_SEP}'")
    return module_id, pair


# ---------------------------------------------------------------------------
# SharedState
# ---------------------------------------------------------------------------


class SharedState:
    """Thread-safe in-process state store for algo modules.

    The store is an in-memory :class:`dict` keyed by ``(module_id, pair)``.
    Each value is a :class:`ModuleStateEntry` TypedDict.

    Persistence is optional.  Call :py:meth:`load_from_disk` at startup and
    :py:meth:`save_to_disk` whenever durable snapshots are required.  All
    disk I/O is atomic (write-to-temp-then-rename) to prevent partial writes.

    Parameters
    ----------
    persistence_path : str
        Path to the JSON file used for persistence.  Defaults to
        ``user_data/algo_system_state.json`` relative to the current working
        directory.

    Thread safety
    -------------
    A single :class:`threading.Lock` guards every mutation and all reads that
    iterate over the store.  Point reads (:py:meth:`get`) hold the lock only
    for the dict lookup so contention is minimal.
    """

    def __init__(
        self,
        persistence_path: str = "user_data/algo_system_state.json",
    ) -> None:
        self._store: Dict[_StoreKey, ModuleStateEntry] = {}
        self._lock = threading.Lock()
        self._persistence_path = Path(persistence_path)
        self._dirty: bool = False
        self._last_save_time: float = 0.0
        self._save_debounce_seconds: float = 60.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, module_id: str, pair: str) -> Optional[ModuleStateEntry]:
        """Return the state entry for *module_id* / *pair*, or ``None``.

        Parameters
        ----------
        module_id:
            Module identifier.
        pair:
            Market pair string (e.g. ``"ETH/USDT"``).

        Returns
        -------
        ModuleStateEntry or None
            A *copy* of the stored entry so callers cannot mutate shared
            state accidentally, or ``None`` if the key is absent.
        """
        with self._lock:
            entry = self._store.get((module_id, pair))
            if entry is None:
                return None
            # Return a shallow copy; deep-copy of 'data' keeps the dict
            # reference clean without the overhead of copy.deepcopy on
            # every read in a hot loop.
            return ModuleStateEntry(
                module_id=entry["module_id"],
                pair=entry["pair"],
                updated_timestamp=entry["updated_timestamp"],
                data=dict(entry["data"]),
            )

    def set(self, module_id: str, pair: str, data: dict) -> None:
        """Upsert state for *module_id* / *pair*.

        The ``updated_timestamp`` is set to the current Unix time
        (integer seconds) automatically.  Marks the store as dirty so a
        subsequent :py:meth:`save_to_disk` will persist the change.

        Parameters
        ----------
        module_id:
            Module identifier.
        pair:
            Market pair string.
        data:
            Arbitrary JSON-serialisable payload.
        """
        now: int = int(time.time())
        entry: ModuleStateEntry = {
            "module_id": module_id,
            "pair": pair,
            "updated_timestamp": now,
            "data": data,
        }
        with self._lock:
            self._store[(module_id, pair)] = entry
            self._dirty = True

        logger.debug(
            "SharedState.set: module_id=%r pair=%r ts=%d", module_id, pair, now
        )
        self._maybe_save()

    def _maybe_save(self) -> None:
        """Write to disk at most once per debounce window."""
        now = time.time()
        if now - self._last_save_time >= self._save_debounce_seconds:
            self.save_to_disk()
            self._last_save_time = now

    def delete(self, module_id: str, pair: str) -> None:
        """Remove the entry for *module_id* / *pair*.

        No-op if the key is not present.

        Parameters
        ----------
        module_id:
            Module identifier.
        pair:
            Market pair string.
        """
        with self._lock:
            removed = self._store.pop((module_id, pair), None)
            if removed is not None:
                self._dirty = True
                logger.debug(
                    "SharedState.delete: removed module_id=%r pair=%r",
                    module_id,
                    pair,
                )

    def get_all_for_module(self, module_id: str) -> Dict[str, ModuleStateEntry]:
        """Return all entries that belong to *module_id*.

        Parameters
        ----------
        module_id:
            Module identifier to filter by.

        Returns
        -------
        dict
            Mapping of ``pair`` -> :class:`ModuleStateEntry` (shallow copies).
            Returns an empty dict if the module has no stored entries.
        """
        result: Dict[str, ModuleStateEntry] = {}
        with self._lock:
            for (mid, pair), entry in self._store.items():
                if mid == module_id:
                    result[pair] = ModuleStateEntry(
                        module_id=entry["module_id"],
                        pair=entry["pair"],
                        updated_timestamp=entry["updated_timestamp"],
                        data=dict(entry["data"]),
                    )
        return result

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load_from_disk(self) -> None:
        """Populate the in-memory store from the JSON persistence file.

        The file format is::

            {
                "entries": [
                    {
                        "__key__": "module_id|pair",
                        "module_id": "...",
                        "pair": "...",
                        "updated_timestamp": 1700000000,
                        "data": { ... }
                    },
                    ...
                ]
            }

        Silently skips if the file does not exist.  Logs and skips corrupted
        entries individually so a single bad record does not abort the load.

        Raises
        ------
        json.JSONDecodeError
            Re-raised if the top-level JSON document is unparseable (the whole
            file is corrupt, not just individual entries).
        """
        path = self._persistence_path
        if not path.exists():
            logger.debug(
                "SharedState.load_from_disk: file not found at %s, skipping", path
            )
            return

        logger.info("SharedState.load_from_disk: loading from %s", path)
        try:
            raw_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error(
                "SharedState.load_from_disk: cannot read %s: %s", path, exc
            )
            return

        try:
            document = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            logger.error(
                "SharedState.load_from_disk: JSON parse error in %s: %s", path, exc
            )
            raise

        entries_raw = document.get("entries", [])
        if not isinstance(entries_raw, list):
            logger.error(
                "SharedState.load_from_disk: 'entries' is not a list in %s", path
            )
            return

        loaded = 0
        skipped = 0
        new_store: Dict[_StoreKey, ModuleStateEntry] = {}

        for item in entries_raw:
            if not isinstance(item, dict):
                logger.warning(
                    "SharedState.load_from_disk: non-dict entry skipped: %r", item
                )
                skipped += 1
                continue

            raw_key = item.get("__key__", "")
            try:
                store_key = _parse_key(raw_key)
            except ValueError as exc:
                logger.warning(
                    "SharedState.load_from_disk: bad __key__ %r, skipping: %s",
                    raw_key,
                    exc,
                )
                skipped += 1
                continue

            # Validate required fields
            missing = {f for f in ("module_id", "pair", "updated_timestamp", "data")
                       if f not in item}
            if missing:
                logger.warning(
                    "SharedState.load_from_disk: entry %r missing fields %s, skipping",
                    raw_key,
                    missing,
                )
                skipped += 1
                continue

            try:
                entry: ModuleStateEntry = {
                    "module_id": str(item["module_id"]),
                    "pair": str(item["pair"]),
                    "updated_timestamp": int(item["updated_timestamp"]),
                    "data": dict(item["data"]),
                }
            except (TypeError, ValueError) as exc:
                logger.warning(
                    "SharedState.load_from_disk: cannot coerce entry %r: %s, skipping",
                    raw_key,
                    exc,
                )
                skipped += 1
                continue

            new_store[store_key] = entry
            loaded += 1

        with self._lock:
            self._store = new_store
            self._dirty = False

        logger.info(
            "SharedState.load_from_disk: loaded %d entries, skipped %d", loaded, skipped
        )

    def save_to_disk(self) -> None:
        """Atomically persist the in-memory store to the JSON file.

        Uses a write-to-temp-then-rename strategy so a crash mid-write
        cannot leave the persistence file in a partial state.

        The parent directory is created if it does not already exist.

        Only serialises if the store is dirty or if the file does not yet
        exist, so repeated no-op calls have minimal overhead.
        """
        path = self._persistence_path
        tmp_path = path.with_suffix(path.suffix + ".tmp")

        # Snapshot under lock; serialise outside the lock to avoid
        # holding it during I/O.
        with self._lock:
            if not self._dirty and path.exists():
                logger.debug(
                    "SharedState.save_to_disk: store not dirty, skipping write"
                )
                return
            snapshot = list(self._store.values())

        entries_serialised = []
        for entry in snapshot:
            key_str = _make_key(entry["module_id"], entry["pair"])
            entries_serialised.append(
                {
                    "__key__": key_str,
                    "module_id": entry["module_id"],
                    "pair": entry["pair"],
                    "updated_timestamp": entry["updated_timestamp"],
                    "data": entry["data"],
                }
            )

        document = {"entries": entries_serialised}

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.write_text(
                json.dumps(document, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(str(tmp_path), str(path))
        except OSError as exc:
            logger.error(
                "SharedState.save_to_disk: failed to write %s: %s", path, exc
            )
            # Best-effort cleanup of the temp file.
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            raise

        with self._lock:
            self._dirty = False

        self._last_save_time = time.time()
        logger.debug(
            "SharedState.save_to_disk: wrote %d entries to %s",
            len(entries_serialised),
            path,
        )

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def clear_all(self) -> None:
        """Wipe the entire in-memory store.

        Intended for use in tests only.  Does **not** delete the
        persistence file on disk.
        """
        with self._lock:
            self._store.clear()
            self._dirty = True
        logger.debug("SharedState.clear_all: store cleared")

    def __len__(self) -> int:
        """Return the number of entries currently in the store."""
        with self._lock:
            return len(self._store)

    def __repr__(self) -> str:
        with self._lock:
            count = len(self._store)
        return (
            f"SharedState(entries={count}, "
            f"persistence_path={str(self._persistence_path)!r}, "
            f"dirty={self._dirty})"
        )
