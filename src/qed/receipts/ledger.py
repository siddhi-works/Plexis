"""Signed certificates in a tamper-evident, hash-chained ledger.

Each certificate (plan + evidence + data fingerprints) is serialised as
canonical JSON, hashed with SHA-256 and signed with Ed25519. Ledger entries
chain each entry's hash into the next, so editing or deleting any past entry
breaks every later link. Backups use SQLite's online backup API and can be
mirrored to S3 when a bucket is configured.
"""

from __future__ import annotations

import base64
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from qed.util import canonical_json, sha256_hex

GENESIS = "0" * 64


class Signer:
    def __init__(self, key_dir: Path) -> None:
        key_dir.mkdir(parents=True, exist_ok=True)
        path = key_dir / "ed25519.pem"
        if path.exists():
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
            assert isinstance(key, Ed25519PrivateKey)
        else:
            key = Ed25519PrivateKey.generate()
            path.write_bytes(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
        self._key = key
        self.public_raw = key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        self.key_id = "qed-" + sha256_hex(self.public_raw)[:16]

    @property
    def public_b64(self) -> str:
        return base64.b64encode(self.public_raw).decode()

    def sign(self, message: bytes) -> str:
        return base64.b64encode(self._key.sign(message)).decode()


def verify_signature(public_b64: str, message: bytes, signature_b64: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_b64)).verify(
            base64.b64decode(signature_b64), message
        )
        return True
    except (InvalidSignature, ValueError):
        return False


@dataclass
class Entry:
    index: int
    kind: str
    cert_hash: str
    prev_hash: str
    entry_hash: str
    signature: str
    key_id: str
    created_at: str
    body: dict[str, Any]

    def public(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "kind": self.kind,
            "certHash": self.cert_hash,
            "prevHash": self.prev_hash,
            "entryHash": self.entry_hash,
            "signature": self.signature,
            "keyId": self.key_id,
            "createdAt": self.created_at,
        }


def entry_hash(index: int, kind: str, cert_hash: str, prev_hash: str, created_at: str) -> str:
    return sha256_hex(f"{index}|{kind}|{cert_hash}|{prev_hash}|{created_at}")


class Ledger:
    def __init__(
        self, state_dir: Path, signer: Signer, *, backup_every: int = 25, s3_bucket: str = ""
    ) -> None:
        self.dir = state_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "ledger.sqlite"
        self.signer = signer
        self.backup_every = backup_every
        self.s3_bucket = s3_bucket
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("""CREATE TABLE IF NOT EXISTS entries (
            idx INTEGER PRIMARY KEY, kind TEXT NOT NULL, cert_hash TEXT NOT NULL, prev_hash TEXT NOT NULL,
            entry_hash TEXT NOT NULL UNIQUE, signature TEXT NOT NULL, key_id TEXT NOT NULL, created_at TEXT NOT NULL,
            idem_key TEXT UNIQUE, body BLOB NOT NULL)""")
        self._db.execute("CREATE INDEX IF NOT EXISTS ix_cert ON entries(cert_hash)")

    # ------------------------------------------------------------------ writes

    def append(self, kind: str, certificate: dict[str, Any], *, idem_key: str | None = None) -> Entry:
        """Hash, sign and chain a certificate. Idempotent per `idem_key`."""
        body = canonical_json(certificate)
        cert_hash = sha256_hex(body)
        with self._lock:
            if idem_key:
                existing = self.by_idem(idem_key)
                if existing is not None:
                    return existing
            head = self._db.execute(
                "SELECT idx, entry_hash FROM entries ORDER BY idx DESC LIMIT 1"
            ).fetchone()
            index = (head[0] + 1) if head else 0
            prev = head[1] if head else GENESIS
            created = datetime.now(UTC).isoformat(timespec="milliseconds")
            eh = entry_hash(index, kind, cert_hash, prev, created)
            sig = self.signer.sign(eh.encode())
            self._db.execute(
                "INSERT INTO entries(idx, kind, cert_hash, prev_hash, entry_hash, signature, key_id, created_at, idem_key, body)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (index, kind, cert_hash, prev, eh, sig, self.signer.key_id, created, idem_key, body),
            )
        if self.backup_every and index and index % self.backup_every == 0:
            self.backup()
        return Entry(index, kind, cert_hash, prev, eh, sig, self.signer.key_id, created, certificate)

    # ------------------------------------------------------------------ reads

    def _row(self, row: tuple[Any, ...] | None) -> Entry | None:
        if row is None:
            return None
        idx, kind, ch, prev, eh, sig, kid, created, _idem, body = row
        return Entry(idx, kind, ch, prev, eh, sig, kid, created, orjson.loads(body))

    def get(self, index: int) -> Entry | None:
        return self._row(self._db.execute("SELECT * FROM entries WHERE idx=?", (index,)).fetchone())

    def by_cert(self, cert_hash: str) -> Entry | None:
        return self._row(
            self._db.execute(
                "SELECT * FROM entries WHERE cert_hash=? ORDER BY idx DESC LIMIT 1", (cert_hash,)
            ).fetchone()
        )

    def by_idem(self, key: str) -> Entry | None:
        return self._row(self._db.execute("SELECT * FROM entries WHERE idem_key=?", (key,)).fetchone())

    def head(self) -> Entry | None:
        return self._row(self._db.execute("SELECT * FROM entries ORDER BY idx DESC LIMIT 1").fetchone())

    def count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM entries").fetchone()[0])

    def verify_chain(self) -> dict[str, Any]:
        """Re-derive every hash and check every signature from genesis to head."""
        prev, n, problems = GENESIS, 0, []
        public = self.signer.public_b64
        for row in self._db.execute("SELECT * FROM entries ORDER BY idx ASC"):
            e = self._row(row)
            assert e is not None
            if sha256_hex(canonical_json(e.body)) != e.cert_hash:
                problems.append(f"entry {e.index}: certificate body does not match its hash")
            if e.prev_hash != prev:
                problems.append(f"entry {e.index}: chain broken (prev hash mismatch)")
            if entry_hash(e.index, e.kind, e.cert_hash, e.prev_hash, e.created_at) != e.entry_hash:
                problems.append(f"entry {e.index}: entry hash mismatch")
            if not verify_signature(public, e.entry_hash.encode(), e.signature):
                problems.append(f"entry {e.index}: bad signature")
            prev, n = e.entry_hash, n + 1
        return {"ok": not problems, "entries": n, "head": prev, "problems": problems[:20]}

    # ------------------------------------------------------------------ backups

    def backup(self, dest: Path | None = None) -> Path:
        backups = self.dir / "backups"
        backups.mkdir(exist_ok=True)
        dest = dest or backups / f"ledger-{time.strftime('%Y%m%d-%H%M%S')}-{self.count()}.sqlite"
        with sqlite3.connect(dest) as target:
            self._db.backup(target)
        if self.s3_bucket:
            try:  # Off-site copy; failure must never block the request path.
                import boto3

                boto3.client("s3").upload_file(str(dest), self.s3_bucket, f"qed/ledger/{dest.name}")
            except Exception:  # noqa: S110
                pass
        return dest

    def restore(self, source: Path) -> dict[str, Any]:
        """Swap in a backup, keeping a copy of the current file, then re-verify the chain."""
        with self._lock:
            self._db.close()
            if self.path.exists():
                shutil.copy2(self.path, self.path.with_suffix(".pre-restore.sqlite"))
            for suffix in ("-wal", "-shm"):
                p = Path(str(self.path) + suffix)
                if p.exists():
                    p.unlink()
            shutil.copy2(source, self.path)
            self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            self._db.execute("PRAGMA journal_mode=WAL")
        return self.verify_chain()

    def close(self) -> None:
        self._db.close()
