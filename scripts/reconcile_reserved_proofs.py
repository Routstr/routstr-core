#!/usr/bin/env python3
"""Reconcile Routstr's reserved Cashu proofs against their mints.

Safe default is dry-run. --apply mutates wallet.sqlite3 and keys.db.
Run only while no process is using these databases and after verified backups.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

import httpx
from cashu.core.base import Proof
from cashu.wallet.helpers import deserialize_token_from_string


def proof_from_row(row: sqlite3.Row) -> Proof:
    return Proof(amount=row["amount"], C=row["C"], secret=row["secret"], id=row["id"])


async def fetch_states(
    client: httpx.AsyncClient,
    mint_url: str,
    proofs: list[Proof],
    batch_size: int,
) -> dict[str, str]:
    states: dict[str, str] = {}
    endpoint = mint_url.rstrip("/") + "/v1/checkstate"
    for offset in range(0, len(proofs), batch_size):
        batch = proofs[offset : offset + batch_size]
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                response = await client.post(endpoint, json={"Ys": [proof.Y for proof in batch]})
                response.raise_for_status()
                for item in response.json().get("states", []):
                    states[item["Y"]] = item["state"]
                last_error = None
                break
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                await asyncio.sleep(2**attempt)
        if last_error is not None:
            raise last_error
    return states


def load_pending_refund_secrets(keys: sqlite3.Connection) -> tuple[set[str], dict[str, set[str]]]:
    pending: set[str] = set()
    transaction_secrets: dict[str, set[str]] = {}
    rows = keys.execute(
        """
        SELECT id, token
        FROM cashu_transactions
        WHERE type = 'out' AND collected = 0 AND swept = 0
        """
    ).fetchall()
    for row in rows:
        try:
            token = deserialize_token_from_string(row["token"])
        except Exception:
            continue
        secrets = {proof.secret for proof in token.proofs}
        transaction_secrets[row["id"]] = secrets
        pending.update(secrets)
    return pending, transaction_secrets


async def run(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    keys_path = root / "keys.db"
    wallet_path = root / ".wallet" / "wallet.sqlite3"
    if not keys_path.exists() or not wallet_path.exists():
        raise SystemExit("keys.db or .wallet/wallet.sqlite3 not found")

    keys = sqlite3.connect(keys_path)
    wallet = sqlite3.connect(wallet_path)
    keys.row_factory = sqlite3.Row
    wallet.row_factory = sqlite3.Row
    keys.execute("PRAGMA foreign_keys=ON")
    wallet.execute("PRAGMA foreign_keys=ON")

    if keys.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise SystemExit("keys.db integrity check failed")
    if wallet.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise SystemExit("wallet.sqlite3 integrity check failed")

    pending_secrets, transaction_secrets = load_pending_refund_secrets(keys)
    rows = wallet.execute(
        """
        SELECT p.rowid AS proof_rowid, p.*, k.mint_url, k.unit
        FROM proofs p
        JOIN keysets k ON k.id = p.id
        WHERE COALESCE(p.reserved, 0) != 0
        ORDER BY k.mint_url, k.unit, p.time_reserved
        """
    ).fetchall()
    grouped: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[(row["mint_url"], row["unit"])].append(row)

    mint_reports: dict[str, object] = {}
    errors: dict[str, str] = {}
    report: dict[str, object] = {
        "mode": "apply" if args.apply else "dry-run",
        "root": str(root),
        "started_at": int(time.time()),
        "pending_refund_secrets": len(pending_secrets),
        "mints": mint_reports,
        "errors": errors,
    }
    state_by_secret: dict[str, str] = {}

    async with httpx.AsyncClient(timeout=args.timeout) as client:
        for (mint_url, unit), mint_rows in grouped.items():
            proofs = [proof_from_row(row) for row in mint_rows]
            try:
                states = await fetch_states(client, mint_url, proofs, args.batch_size)
            except Exception as exc:
                errors[f"{mint_url}|{unit}"] = f"{type(exc).__name__}: {exc}"
                continue

            summary: dict[str, dict[str, int]] = defaultdict(lambda: {"proofs": 0, "amount": 0})
            actions = {"delete_spent": 0, "release_untracked_unspent": 0, "preserve_pending": 0, "preserve_unknown": 0}
            for row, proof in zip(mint_rows, proofs):
                state = states.get(proof.Y, "MISSING")
                state_by_secret[row["secret"]] = state
                summary[state]["proofs"] += 1
                summary[state]["amount"] += row["amount"]

                if state == "SPENT":
                    actions["delete_spent"] += 1
                    if args.apply:
                        wallet.execute(
                            """
                            INSERT OR IGNORE INTO proofs_used
                                (amount, C, secret, time_used, id, derivation_path, mint_id, melt_id)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                row["amount"], row["C"], row["secret"],
                                row["time_reserved"] or int(time.time()), row["id"],
                                row["derivation_path"], row["mint_id"], row["melt_id"],
                            ),
                        )
                        wallet.execute("DELETE FROM proofs WHERE rowid = ?", (row["proof_rowid"],))
                elif state == "UNSPENT" and row["secret"] not in pending_secrets:
                    actions["release_untracked_unspent"] += 1
                    if args.apply:
                        wallet.execute(
                            "UPDATE proofs SET reserved = 0, send_id = NULL, time_reserved = NULL WHERE rowid = ?",
                            (row["proof_rowid"],),
                        )
                elif state == "UNSPENT":
                    actions["preserve_pending"] += 1
                else:
                    actions["preserve_unknown"] += 1

            mint_reports[f"{mint_url}|{unit}"] = {
                "states": dict(summary),
                "actions": actions,
            }

    # Mark pending outgoing tokens collected only when every proof the mint reported is SPENT.
    collected_transactions: list[str] = []
    for transaction_id, secrets in transaction_secrets.items():
        known = [state_by_secret.get(secret) for secret in secrets]
        if known and all(state == "SPENT" for state in known):
            collected_transactions.append(transaction_id)
            if args.apply:
                keys.execute(
                    "UPDATE cashu_transactions SET collected = 1 WHERE id = ? AND collected = 0 AND swept = 0",
                    (transaction_id,),
                )
    report["mark_collected_transactions"] = len(collected_transactions)

    malformed = keys.execute(
        "SELECT COUNT(*), COALESCE(SUM(balance), 0) FROM api_keys WHERE refund_mint_url = ?",
        ("https://mint.minibits.cash/Bi",),
    ).fetchone()
    report["canonicalize_minibits_url"] = {"keys": malformed[0], "balance_msat": malformed[1]}
    if args.apply:
        keys.execute(
            "UPDATE api_keys SET refund_mint_url = ? WHERE refund_mint_url = ?",
            ("https://mint.minibits.cash/Bitcoin", "https://mint.minibits.cash/Bi"),
        )

    negative_rows = keys.execute(
        "SELECT hashed_key, balance FROM api_keys WHERE balance < 0 ORDER BY balance"
    ).fetchall()
    report["negative_balances"] = {
        "keys": len(negative_rows),
        "amount_msat": sum(row["balance"] for row in negative_rows),
        "key_prefixes": [row["hashed_key"][:12] for row in negative_rows],
    }
    if args.apply:
        keys.execute("UPDATE api_keys SET balance = 0 WHERE balance < 0")

    if args.apply:
        wallet.commit()
        keys.commit()
    else:
        wallet.rollback()
        keys.rollback()

    report["finished_at"] = int(time.time())
    output = root / f"reconciliation-{'applied' if args.apply else 'dry-run'}-{report['finished_at']}.json"
    output.write_text(json.dumps(report, indent=2, sort_keys=True))
    os.chmod(output, 0o600)
    print(json.dumps(report, indent=2, sort_keys=True))
    print(f"report={output}")

    wallet.close()
    keys.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch-size", type=int, default=300)
    parser.add_argument("--timeout", type=float, default=45.0)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
