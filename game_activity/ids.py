"""Stable activity identifiers, content hashes and rename detection.

Upstream feeds carry no stable identifiers, so identity is derived from the
normalized activity name once and then persisted forever (see design §7).
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import unicodedata

from .models import NormalizedActivity

_WHITESPACE = re.compile(r"\s+")
MAX_DUPLICATES = 99


def normalize_name(text: str) -> str:
    """Normalize an activity name for identity and fuzzy matching."""
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    normalized = normalized.replace("\u3000", " ")
    return _WHITESPACE.sub(" ", normalized).strip().casefold()


def base_raw_id(source_id: str, name: str) -> str:
    """Return the deterministic base id: ``a`` plus 16 hex characters."""
    digest = hashlib.sha256(
        f"{source_id}\x1f{normalize_name(name)}".encode(),
    ).hexdigest()
    return "a" + digest[:16]


def content_hash(item: NormalizedActivity) -> str:
    """Hash the normalized content tuple used to detect changes (design §7.3)."""
    payload = [
        normalize_name(item.name),
        (item.description or "").strip().replace("\r\n", "\n"),
        item.kind or "",
        int(item.start_epoch),
        int(item.end_epoch),
        (item.cover_url or "").strip(),
    ]
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def name_similarity(left: str, right: str) -> float:
    """Return a 0..1 similarity ratio between two normalized names."""
    a = normalize_name(left)
    b = normalize_name(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def is_rename(old: dict, new: NormalizedActivity) -> bool:
    """Return True when a new item is the same upstream activity renamed."""
    return (
        int(old.get("start_epoch") or 0) == int(new.start_epoch)
        and int(old.get("end_epoch") or 0) == int(new.end_epoch)
        and name_similarity(str(old.get("name") or ""), new.name) >= 0.9
    )


def _candidate(base: str, ordinal: int) -> str:
    return base if ordinal == 1 else f"{base}-{ordinal}"


def _fingerprint(name: str, start_epoch: int, end_epoch: int) -> tuple[str, int, int]:
    return (normalize_name(name), int(start_epoch), int(end_epoch))


def assign_raw_ids(
    source_id: str,
    items: list[NormalizedActivity],
    existing: list[dict],
) -> tuple[list[tuple[str, NormalizedActivity]], list[str]]:
    """Assign or reuse a raw id for every parsed item.

    Args:
        source_id: Source the items belong to.
        items: Parsed items of the current sync round, in upstream order.
        existing: Persisted activities of this source (including tombstones).

    Returns:
        A list of ``(raw_id, item)`` pairs in input order, plus warnings for
        items dropped because the duplicate suffix space was exhausted.
    """
    warnings: list[str] = []
    by_fingerprint: dict[tuple[str, int, int], str] = {}
    reserved: set[str] = set()
    for activity in existing:
        raw_id = str(activity.get("raw_id") or "")
        if not raw_id:
            continue
        reserved.add(raw_id)
        fingerprint = _fingerprint(
            str(activity.get("name") or ""),
            int(activity.get("start_epoch") or 0),
            int(activity.get("end_epoch") or 0),
        )
        by_fingerprint.setdefault(fingerprint, raw_id)

    assigned: dict[int, str] = {}
    used: set[str] = set()
    pending: list[int] = []
    for index, item in enumerate(items):
        fingerprint = _fingerprint(item.name, item.start_epoch, item.end_epoch)
        raw_id = by_fingerprint.get(fingerprint)
        if raw_id and raw_id not in used:
            assigned[index] = raw_id
            used.add(raw_id)
        else:
            pending.append(index)

    groups: dict[str, list[int]] = {}
    for index in pending:
        base = base_raw_id(source_id, items[index].name)
        groups.setdefault(base, []).append(index)

    for base, indexes in groups.items():
        indexes.sort(
            key=lambda i: (
                items[i].start_epoch,
                items[i].end_epoch,
                normalize_name(items[i].name),
            ),
        )
        ordinal = 1
        for index in indexes:
            while ordinal <= MAX_DUPLICATES:
                candidate = _candidate(base, ordinal)
                ordinal += 1
                if candidate in used or candidate in reserved:
                    continue
                assigned[index] = candidate
                used.add(candidate)
                break
            else:
                warnings.append(f"too_many_duplicates:{base}")

    pairs = [
        (assigned[index], items[index])
        for index in range(len(items))
        if index in assigned
    ]
    return pairs, warnings
