#!/usr/bin/env python3
"""Privacy-strip a udbb-scraper field export for commit to the PUBLIC
bestball-bro-data repo.

The repo serves gh-pages, so anything committed is world-readable. This
script reduces a raw export to the inert, privacy-safe field corpus used by
the harness field-sigma refit / field-construction analysis, keeping only
what those consumers need to join picks -> positions and measure opponent
construction.

What it does (see sources/field/README.md for the rationale):
  1. UNKEYED envelopes: keep ONLY slate-level reference payloads needed for
     joins -- `/v1/slates/<id>/players` and `/v1/slates/<id>/.../appearances`.
     Every account-scoped envelope (any `/v*/user/...`: entries, balances,
     active drafts, rankings) is dropped, along with other non-reference
     captures (`/v1/tournaments`, bare `/v1/slates/<id>`, `/v2/slates/.../matches`).
  2. Hash every `user_id` -> sha256(user_id)[:12] (raw ids are opaque,
     high-entropy UUIDs; no salt needed). Tag the one hashed id present in
     (nearly) all drafts with `is_owner: true` so the opponent-only stats
     convention survives the strip.
  3. Retain player names (`first_name`/`last_name`): they are PUBLIC NFL data
     living only in the `/players` reference payloads, and downstream
     consumers (empirical ghost, leverage work, human inspection) want them
     without a re-join. (Flip `STRIP_PLAYER_NAMES = True` to drop them.)
  4. Drop `draft_round_index` (owner-scoped round history: `user_payout` /
     `user_place`, 180 rounds beyond the 51 drafts). Keep
     `round_tournament_index` (public tournament metadata; the draft->tournament
     join also survives on each draft envelope's own `tournament_id`).
  5. Defensive sweep (scoped): hard PII (`username`/`email`/`balance`/`phone`/
     ...) forbidden ANYWHERE; player names permitted ONLY inside the reference
     payloads, forbidden in draft_entries / any user-context object. Raises
     with locations on any violation.
  6. Deterministic output (sorted keys) so future re-strips diff cleanly.

Usage:
    python scripts/strip_field_export.py <raw_export.json> [out.json]
Default out: sources/field/boards_2026-06-10.json
"""
from __future__ import annotations
import hashlib
import json
import sys
from pathlib import Path

# --- configuration ----------------------------------------------------------
# Player first/last names are PUBLIC NFL data and live only in the
# `/players` reference payloads; retaining them spares every downstream
# consumer a re-join for zero privacy gain. Flip to True to drop them.
STRIP_PLAYER_NAMES = False
NAME_KEYS = ("first_name", "last_name")
# Hard PII: forbidden ANYWHERE in the public corpus.
HARD_FORBIDDEN = {
    "username", "user_name", "email", "full_name", "display_name",
    "balance", "phone", "address",
}
# Player-name keys: permitted ONLY inside the `/players` // `/appearances`
# reference payloads (public NFL names); forbidden in draft_entries or any
# other user-context object.
REFERENCE_ONLY = set(NAME_KEYS)


def hash_uid(uid: str) -> str:
    return hashlib.sha256(uid.encode("utf-8")).hexdigest()[:12]


def keep_unkeyed(api_endpoint: str) -> bool:
    """Reference payloads needed for the pick->position join only."""
    ep = api_endpoint or ""
    return ep.startswith("/v1/slates/") and ep.endswith(("/players", "/appearances"))


def transform(node):
    """Recursively: hash user_id, drop player-name keys. Returns a new tree."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "user_id" and isinstance(v, str):
                out[k] = hash_uid(v)
            elif STRIP_PLAYER_NAMES and k in NAME_KEYS:
                continue  # drop player-name key entirely
            else:
                out[k] = transform(v)
        return out
    if isinstance(node, list):
        return [transform(x) for x in node]
    return node


def find_owner_hash(drafts: list) -> str:
    """The hashed user_id present in (nearly) all drafts is the owner."""
    from collections import Counter
    per_draft = Counter()
    for d in drafts:
        ids = set()
        _collect_user_ids(d, ids)
        for u in ids:
            per_draft[u] += 1
    if not per_draft:
        raise SystemExit("No user_id found in any draft -- cannot identify owner.")
    owner, n = per_draft.most_common(1)[0]
    return owner, n, len(drafts)


def _collect_user_ids(node, acc: set):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "user_id" and isinstance(v, str):
                acc.add(v)
            else:
                _collect_user_ids(v, acc)
    elif isinstance(node, list):
        for x in node:
            _collect_user_ids(x, acc)


def tag_owner(node, owner_hash: str):
    """Add is_owner:true to any object whose user_id == owner_hash."""
    if isinstance(node, dict):
        if node.get("user_id") == owner_hash:
            node["is_owner"] = True
        for v in node.values():
            tag_owner(v, owner_hash)
    elif isinstance(node, list):
        for x in node:
            tag_owner(x, owner_hash)


def sweep_scoped(node, in_reference, path, hits):
    """Hard PII forbidden anywhere; player names allowed only where
    `in_reference` (inside the kept /players // /appearances payloads)."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in HARD_FORBIDDEN:
                hits.append(f"{path}.{k}  (PII)")
            elif k in REFERENCE_ONLY and not in_reference:
                hits.append(f"{path}.{k}  (player-name outside reference payload)")
            sweep_scoped(v, in_reference, f"{path}.{k}", hits)
    elif isinstance(node, list):
        for i, x in enumerate(node):
            sweep_scoped(x, in_reference, f"{path}[{i}]", hits)


def defensive_sweep(out: dict) -> list:
    """Run the scoped sweep: the kept `unkeyed` reference payloads may carry
    public player names; everything else (drafts, indexes, top-level) may not."""
    hits = []
    sweep_scoped(out.get("unkeyed", []), True, "$.unkeyed", hits)
    rest = {k: v for k, v in out.items() if k != "unkeyed"}
    sweep_scoped(rest, False, "$", hits)
    return hits


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    raw_path = Path(sys.argv[1])
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 \
        else Path("sources/field/boards_2026-06-10.json")

    raw = json.loads(raw_path.read_text(encoding="utf-8"))

    drafts_in = raw.get("drafts", [])
    unkeyed_in = raw.get("unkeyed", [])

    # 1. hash owner identity (computed on RAW ids, then hashed for the tag)
    owner_raw, owner_n, n_drafts = find_owner_hash(drafts_in)
    owner_hash = hash_uid(owner_raw)

    # 2. transform drafts (hash uids, drop name keys) + tag owner
    drafts = [transform(d) for d in drafts_in]
    tag_owner(drafts, owner_hash)

    # 3. unkeyed: keep only reference payloads, then transform
    kept_unkeyed_in = [e for e in unkeyed_in if keep_unkeyed(e.get("api_endpoint", ""))]
    dropped = len(unkeyed_in) - len(kept_unkeyed_in)
    unkeyed = [transform(e) for e in kept_unkeyed_in]

    out = {
        "exported_at": raw.get("exported_at"),
        "schema_version": raw.get("schema_version"),
        "scraper_version": raw.get("scraper_version"),
        "privacy_strip": (
            "field-export-strip v1: account-scoped envelopes dropped; "
            "user_id -> sha256[:12]; is_owner tagged; draft_round_index dropped; "
            "player names retained (public, reference payloads only)"
        ),
        "draft_count": len(drafts),
        "unkeyed_kept_count": len(unkeyed),
        "unkeyed_dropped_count": dropped,
        "owner_id_hash": owner_hash,
        "drafts": drafts,
        "unkeyed": unkeyed,
        # public tournament metadata; draft->tournament also lives on each
        # draft envelope, so this is convenience, not a privacy dependency.
        "round_tournament_index": raw.get("round_tournament_index", {}),
        # NOTE: draft_round_index intentionally dropped (owner-scoped history).
    }

    # 4. defensive sweep -- must be clean
    hits = defensive_sweep(out)
    if hits:
        raise SystemExit(
            "DEFENSIVE SWEEP FAILED -- forbidden keys survive:\n  "
            + "\n  ".join(hits[:50])
            + (f"\n  ... (+{len(hits) - 50} more)" if len(hits) > 50 else "")
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # deterministic: sorted keys, stable indentation, utf-8
    text = json.dumps(out, sort_keys=True, indent=2, ensure_ascii=False)
    out_path.write_text(text + "\n", encoding="utf-8")

    size = out_path.stat().st_size
    print(f"owner raw->hash: {owner_raw[:8]}... -> {owner_hash} (in {owner_n}/{n_drafts} drafts)")
    print(f"drafts kept: {len(drafts)} | unkeyed kept: {len(unkeyed)} "
          f"(dropped {dropped})")
    print(f"defensive sweep: CLEAN (hard-PII absent everywhere; "
          f"player names confined to reference payloads)")
    print(f"wrote {out_path} ({size:,} bytes)")


if __name__ == "__main__":
    main()
