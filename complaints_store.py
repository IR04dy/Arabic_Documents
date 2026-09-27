"""SQLite register for the complaint-management tab: rows, files, audit, analytics.

One connection behind one RLock. The worker thread and the API threadpool share
it; SQLite serialises writers anyway, so a guarded connection is simpler than a
pool and makes the read-then-write steps (dedup in `create`, `claim_next`)
atomic without extra machinery. WAL lets an outside reader (the sqlite3 shell, a
backup) look at the register without blocking the app.

Effective vs model fields: category/subcategory/ministry/priority/region/
governorate are what the register shows — the pipeline's output until a
reviewer corrects them. `model_*` always hold the pipeline's latest output (LLM
+ rule floors), so the analytics can measure how often reviewers agree with it.
`review_reasons` stays as the analysis recorded it: a reviewer's verdict clears
needs_review, not the reasons. Taxonomy ids are not validated here; the API
checks them against the taxonomy.

Field reviews: a structured field the model gave a value for but the text
does not carry verbatim (`verified: false`) is pending until a reviewer
accepts it or changes it (review_field). `field_reviews` keeps those
decisions by field key; the detail returns analysis.structured.fields with
them merged in, complainant_name / national_id follow the effective value, and
`pending_fields` counts what is left. A pending field keeps the complaint in
needs_review whatever the verdict on its classification, and the
fields_unverified reason is shown only while one is. A re-analysis keeps every
change, and an accept only while the model still gives the accepted value.

Outside jurisdiction follows the EFFECTIVE region: with `home_region` (the
receiving entity's region id, which the API passes to the constructor) a
complaint whose region is known and is not home is outside, one in home is not,
and only an unknown region falls back to the reason the analysis recorded. The
summaries' `outside_jurisdiction` and analytics' totals.outside_jurisdiction
are both computed that way; without home_region they are the recorded reason.

One process owns the queue (acquire_owner: an OS lock beside the database, held
until close). Only the owner sweeps interrupted uploads and orphaned files; a
second app started on the same data directory reads and writes rows but leaves
the queue and the files of the running one alone.

Deleting erases: secure_delete zeroes the freed pages and a WAL checkpoint
drops the old copies, so a deleted complaint's text does not linger in
complaints.db or its -wal file.

Timestamps are ISO UTC ("YYYY-MM-DDTHH:MM:SSZ") from an injectable clock, so they
also sort correctly as strings. Day buckets (trend, the year in a ref) use Riyadh
time: a complaint filed at 01:00 local belongs to that local day.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import threading
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from registry import Normalizer

SCHEMA_VERSION = 4
STAGES = ("queued", "ocr", "structuring", "classifying", "done", "error")
PROCESSING = ("ocr", "structuring", "classifying")
SOURCES = ("upload", "text")
FILE_KINDS = ("pdf", "image", "txt")
# Highest first, mirroring the taxonomy's ranks: the store sorts and zero-fills
# by priority without loading the taxonomy (the API owns that).
PRIORITY_ORDER = ("critical", "high", "medium", "low")
# The taxonomy's open statuses, for callers that do not pass their own.
OPEN_STATUSES = ("new", "in_review", "referred")
FEEDBACK_FIELDS = ("category", "subcategory", "ministry", "priority", "region", "governorate")
# The review reason the pipeline gives a complaint located outside the
# receiving entity's region (the fallback when the effective region is unknown).
OUTSIDE_JURISDICTION = "outside_jurisdiction"
UNKNOWN_REGIONS = (None, "", "unknown")
# Reasons that put a reviewed complaint back in front of a reviewer when a
# re-analysis finds one the reviewer has not seen: the text itself changed.
REVIEW_AGAIN = ("empty_text", "not_a_complaint", "input_truncated")
# The reason for fields the text does not carry verbatim: the field reviews
# answer it, not a verdict, so it counts only while a field is still pending.
FIELDS_UNVERIFIED = "fields_unverified"
FIELD_ACTIONS = ("accept", "change")
MAX_FIELD_VALUE = 500
LIST_MAX = 500
TEMP_MAX_AGE_S = 3600                   # an upload temp file this old was cut short by a crash
TOP_CORRECTIONS = 10
TOP_CORRECTION_REFS = 5
# Saudi Arabia has kept UTC+3 without DST for decades, so a fixed offset is exact
# and needs no tzdata (absent on a stock Windows Python).
RIYADH = timezone(timedelta(hours=3), "Asia/Riyadh")

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS complaints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ref TEXT UNIQUE,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        source TEXT NOT NULL,
        filename TEXT NOT NULL DEFAULT '',
        file_kind TEXT,
        file_sha256 TEXT NOT NULL,
        file_size INTEGER,
        file_ext TEXT NOT NULL DEFAULT '',
        page_count INTEGER NOT NULL DEFAULT 0,
        stage TEXT NOT NULL DEFAULT 'queued',
        error TEXT,
        text TEXT,
        analysis TEXT,
        provider TEXT,
        model TEXT,
        subject TEXT,
        summary TEXT,
        complainant_name TEXT,
        national_id TEXT,
        category TEXT,
        subcategory TEXT,
        ministry TEXT,
        priority TEXT,
        region TEXT,
        model_category TEXT,
        model_ministry TEXT,
        model_priority TEXT,
        model_region TEXT,
        status TEXT NOT NULL DEFAULT 'new',
        needs_review INTEGER NOT NULL DEFAULT 0,
        reviewed INTEGER NOT NULL DEFAULT 0,
        due_at TEXT,
        processed_at TEXT,
        timings TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        governorate TEXT,
        model_governorate TEXT,
        review_reasons TEXT,
        reocr INTEGER NOT NULL DEFAULT 0,
        field_reviews TEXT,
        pending_fields INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        complaint_id INTEGER NOT NULL REFERENCES complaints(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        verdict TEXT NOT NULL,
        changes TEXT NOT NULL DEFAULT '{}',
        note TEXT NOT NULL DEFAULT '',
        reviewer TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        complaint_id INTEGER NOT NULL REFERENCES complaints(id) ON DELETE CASCADE,
        at TEXT NOT NULL,
        kind TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '{}'
    )""",
)
# Columns schema 2 added: a new database gets them from CREATE TABLE above (at
# the end, so both kinds of database have the same layout), a schema-1 one by
# ALTER TABLE in _migrate.
_V2_COLUMNS = (("governorate", "TEXT"), ("model_governorate", "TEXT"), ("review_reasons", "TEXT"))
# Schema 3: reocr = a re-extraction was asked for. The text stays until the new
# OCR result replaces it, so a failed or empty pass loses nothing.
_V3_COLUMNS = (("reocr", "INTEGER NOT NULL DEFAULT 0"),)
# Schema 4: the reviewers' field decisions ({key: review}) and how many fields
# still wait for one (denormalised, so the register need not parse analyses).
_V4_COLUMNS = (("field_reviews", "TEXT"), ("pending_fields", "INTEGER NOT NULL DEFAULT 0"))
_MIGRATIONS = ((2, _V2_COLUMNS), (3, _V3_COLUMNS), (4, _V4_COLUMNS))
# After the migration: an index on a column the old table lacks would fail.
_INDEXES = (
    *(f"CREATE INDEX IF NOT EXISTS complaints_{c} ON complaints({c})"
      for c in ("stage", "status", "category", "ministry", "priority", "created_at",
                "file_sha256", "national_id", "governorate")),
    # The cascades and the detail view look rows up by complaint.
    "CREATE INDEX IF NOT EXISTS feedback_complaint ON feedback(complaint_id)",
    "CREATE INDEX IF NOT EXISTS events_complaint ON events(complaint_id)",
)

SUMMARY_COLUMNS = (
    "id", "ref", "created_at", "updated_at", "source", "filename", "file_kind",
    "page_count", "stage", "error", "status", "subject", "summary", "complainant_name",
    "category", "subcategory", "ministry", "priority", "region", "governorate",
    "model_category", "model_ministry", "model_priority", "model_governorate",
    "needs_review", "review_reasons", "reviewed", "due_at", "provider", "model",
    "pending_fields",
)
_DETAIL_COLUMNS = SUMMARY_COLUMNS + (
    "text", "analysis", "timings", "national_id", "model_region", "processed_at",
    "attempts", "file_ext", "field_reviews",
)
_SUMMARY_SQL = ", ".join(SUMMARY_COLUMNS)
_DETAIL_SQL = ", ".join(_DETAIL_COLUMNS)
_FILTERS = ("status", "category", "ministry", "priority", "region", "governorate", "stage")
_SEARCH_COLUMNS = ("ref", "subject", "summary", "complainant_name", "filename", "national_id")
# A search that is only digits and separators may be a national id typed as
# printed on the letter («١٠٩٨ ٧٦٥ ٤٣٢», 1098-765-432); the column has none.
_ID_SEPARATORS = re.compile(r"[-‐-―.]")
_STORED_FILE = re.compile(r"([0-9]+)\.[a-z0-9]{1,10}")
_RANK_SQL = ("CASE priority " + " ".join(
    f"WHEN '{p}' THEN {len(PRIORITY_ORDER) - i}" for i, p in enumerate(PRIORITY_ORDER))
    + " ELSE 0 END")
_SORTS = {
    "created": "created_at {o}, id {o}",
    "updated": "updated_at {o}, id {o}",
    "due": "due_at IS NULL, due_at {o}, id {o}",      # undated rows last either way
    "priority": _RANK_SQL + " {o}, created_at DESC, id DESC",   # ties: newest first
}
_TIMING_KEYS = ("ocr_s", "structure_s", "classify_s")

_DIGITS = {ord(c): str(i) for digits in ("٠١٢٣٤٥٦٧٨٩", "۰۱۲۳۴۵۶۷۸۹")
           for i, c in enumerate(digits)}
# Whitespace plus the invisible bidi/zero-width marks OCR leaves around digit runs.
_ID_NOISE = re.compile("[\\s​-‏؜‪-‮⁦-⁩﻿]+")
_NORMALIZE = Normalizer()


def default_paths(env=os.environ) -> tuple[Path, Path]:
    """(db_path, files_dir) under CMS_DATA_DIR, else <repo>/data/complaints."""
    root = Path(env.get("CMS_DATA_DIR") or Path(__file__).resolve().parent / "data" / "complaints")
    return root / "complaints.db", root / "files"


def normalize_national_id(value) -> str:
    """One spelling per ID: OCR gives ١٠٩٨ ٧٦٥ ٤٣٢ where a form typed 1098765432."""
    if not isinstance(value, str):
        return ""
    return _ID_NOISE.sub("", value).translate(_DIGITS)


def _fold(value) -> str:
    """Search key: the app's Arabic folds (alef forms, ة/ه, tashkeel, digits) + casefold."""
    return _NORMALIZE(value).casefold() if isinstance(value, str) else ""


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(text, default):
    """Decode a JSON column; a damaged value reads as `default`, never an exception."""
    try:
        value = json.loads(text) if text else default
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) or default is None else default


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def _clean_ext(ext: str, kind: str | None) -> str:
    ext = (ext or "").strip().lstrip(".").lower()
    if re.fullmatch(r"[a-z0-9]{1,10}", ext):
        return ext
    return {"pdf": "pdf", "txt": "txt"}.get(kind or "", "bin")


def _log(message: str, exc: BaseException | None = None) -> None:
    """A log line that cannot fail: the app's stdout is often cp1252, and a
    path (CMS_DATA_DIR) or an exception repr may hold Arabic."""
    line = message if exc is None else f"{message} {exc!r}"
    try:
        print(line.encode("ascii", "backslashreplace").decode("ascii"))
    except Exception:
        pass


def _remove(path: Path) -> bool:
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError as exc:              # e.g. still open by a download on Windows
        _log("complaints store file error:", exc)
        return False


def _try_lock(fh) -> bool:
    """A non-blocking exclusive lock on the first byte of an open file, held
    until it is unlocked or the file closed (also when the process dies)."""
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fh) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass                            # closing the file releases it anyway


def _ranked(counts: Counter) -> list[dict]:
    return [{"id": k, "count": n} for k, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def _reasons(text) -> list[str]:
    """The review_reasons column as a list of ids ([] for NULL or damage)."""
    return [r for r in _loads(text, []) if isinstance(r, str)]


def _outside(region, reasons: list[str], home: str | None) -> bool:
    """Outside the receiving entity's jurisdiction: the effective region
    decides when it is known, else the reason the analysis recorded."""
    if home and region not in UNKNOWN_REGIONS:
        return region != home
    return OUTSIDE_JURISDICTION in reasons


def _fields(analysis) -> list[dict]:
    """analysis.structured.fields: the entries that are objects with a key."""
    structured = analysis.get("structured") if isinstance(analysis, dict) else None
    fields = structured.get("fields") if isinstance(structured, dict) else None
    if not isinstance(fields, list):
        return []
    return [f for f in fields if isinstance(f, dict) and isinstance(f.get("key"), str)]


def _field_reviews(text) -> dict[str, dict]:
    """The field_reviews column as {field key: review}, damaged entries left out."""
    return {k: r for k, r in _loads(text, {}).items()
            if isinstance(r, dict) and r.get("action") in FIELD_ACTIONS
            and isinstance(r.get("value"), str)}


def _is_pending(field: dict, review: dict | None) -> bool:
    """A value the model gave that the text does not carry verbatim, and no
    reviewer has decided on yet. `verified` must be False, not just absent."""
    value = field.get("value")
    return (review is None and field.get("verified") is False
            and isinstance(value, str) and bool(value.strip()))


def _pending(fields: list[dict], reviews: dict) -> int:
    return sum(_is_pending(f, reviews.get(f["key"])) for f in fields)


def _effective(fields: list[dict], reviews: dict) -> dict:
    """{key: value} as the register shows it: a reviewed field has the value
    the reviewer accepted or typed, the others the model's."""
    return {f["key"]: reviews[f["key"]]["value"] if f["key"] in reviews else f.get("value")
            for f in fields}


def _merged(fields: list, reviews: dict) -> list:
    """The detail's fields: a reviewed one carries the reviewer's value and
    `review`, a pending one `pending: true`; anything else as analysed."""
    out = []
    for f in fields:
        if isinstance(f, dict) and isinstance(f.get("key"), str):
            review = reviews.get(f["key"])
            if review is not None:
                f = {**f, "value": review["value"],
                     "review": {k: review.get(k) for k in ("action", "reviewer", "at", "from", "note")}}
            elif _is_pending(f, None):
                f = {**f, "pending": True}
        out.append(f)
    return out


def _needs_review(pending: int, reviewed, reasons: list[str], again: bool = False) -> bool:
    """A pending field always needs a reviewer; the classification needs one
    until a verdict is given (or, `again`, a re-analysis found a REVIEW_AGAIN
    reason the reviewer has not seen). fields_unverified alone asks for no
    verdict: the field reviews answer it."""
    return (pending > 0 or again
            or (not reviewed and any(r != FIELDS_UNVERIFIED for r in reasons)))


def _summary_of(row, home: str | None = None) -> dict:
    out = {k: row[k] for k in SUMMARY_COLUMNS}
    out["needs_review"] = bool(out["needs_review"])
    out["reviewed"] = bool(out["reviewed"])
    out["pending_fields"] = int(out["pending_fields"] or 0)
    reasons = _reasons(out["review_reasons"])
    if not out["pending_fields"]:
        # Every such field has been reviewed: the reason is answered.
        reasons = [r for r in reasons if r != FIELDS_UNVERIFIED]
    out["review_reasons"] = reasons
    # A queued complaint has no region and no reasons yet: not outside.
    out["outside_jurisdiction"] = _outside(out["region"], out["review_reasons"], home)
    return out


class Store:
    """The complaint register. Every public method is safe to call from any thread."""

    def __init__(self, db_path, files_dir, *, clock=None, home_region: str | None = None):
        self.db_path = Path(db_path)
        self.files_dir = Path(files_dir)
        # The receiving entity's region id: decides outside_jurisdiction (see
        # the module docstring). The API sets it from the taxonomy.
        self.home_region = home_region
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._claimed: dict[int, datetime] = {}      # id -> claim time, for ocr_s
        self._owner = None                           # the lock file while we own the queue
        self._orphans = False                        # a deleted complaint's file is still on disk
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: transactions are explicit (BEGIN IMMEDIATE in _tx),
        # not opened implicitly by the sqlite3 module behind our backs.
        db = sqlite3.connect(str(self.db_path), check_same_thread=False,
                             isolation_level=None, timeout=10)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")     # durable enough under WAL
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA secure_delete=ON")       # freed pages are zeroed, not left readable
            db.create_function("cms_fold", 1, _fold, deterministic=True)
            self._db = db
            self._migrate()
        except BaseException:
            db.close()
            raise

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None
            if self._owner is not None:
                _unlock(self._owner)
                self._owner.close()
                self._owner = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------ plumbing

    def _migrate(self) -> None:
        with self._tx() as db:
            # Read under the write lock: a second process opening the same
            # file waits here instead of migrating it at the same time.
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(f"complaints database schema {version} is newer than this code")
            for statement in _SCHEMA:          # IF NOT EXISTS: harmless on every start
                db.execute(statement)
            # Only the columns still missing: a database CREATE TABLE just made
            # (version 0) already has them, in the same order.
            have = {r["name"] for r in db.execute("PRAGMA table_info(complaints)")}
            for target, columns in _MIGRATIONS:
                if version < target:
                    for name, kind in columns:
                        if name not in have:
                            db.execute(f"ALTER TABLE complaints ADD COLUMN {name} {kind}")
            if version < 2:
                # Old rows' reasons come from the analysis they were recorded
                # with. CASE, not AND: SQLite may evaluate AND terms in any
                # order, and json_type fails the statement on a damaged value.
                db.execute("UPDATE complaints SET review_reasons = json_extract(analysis, '$.review_reasons') "
                           "WHERE review_reasons IS NULL AND CASE WHEN json_valid(analysis) "
                           "THEN json_type(analysis, '$.review_reasons') = 'array' ELSE 0 END")
            if version < 4:
                # Older analyses already mark a paraphrased value verified:
                # false. From now on it is pending, and a pending field needs
                # a reviewer. Reviews are read too: a schema-4 database
                # labelled 3 again must not count the reviewed ones.
                for row in db.execute("SELECT id, analysis, field_reviews FROM complaints "
                                      "WHERE analysis IS NOT NULL").fetchall():
                    pending = _pending(_fields(_loads(row["analysis"], {})),
                                       _field_reviews(row["field_reviews"]))
                    db.execute("UPDATE complaints SET pending_fields = ?, "
                               "needs_review = MAX(needs_review, ?) WHERE id = ?",
                               (pending, int(pending > 0), row["id"]))
            for statement in _INDEXES:
                db.execute(statement)
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION:d}")

    @contextmanager
    def _tx(self):
        """BEGIN IMMEDIATE … COMMIT under the lock: the write lock is taken up
        front, so a read-then-write cannot interleave with another writer."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.execute("COMMIT")
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def _now_dt(self) -> datetime:
        now = self._clock()
        return (now if now.tzinfo else now.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)

    def _now(self) -> str:
        return _iso(self._now_dt())

    def _file(self, id: int, ext: str) -> Path:
        return self.files_dir / f"{int(id)}.{ext}"

    @staticmethod
    def _event(db, id: int, kind: str, detail: dict, at: str) -> None:
        # Details carry ids, stages and short reviewer notes — never document
        # text beyond a reviewed field's short value (field_review's from / to).
        db.execute("INSERT INTO events (complaint_id, at, kind, detail) VALUES (?, ?, ?, ?)",
                   (id, at, kind, _dumps(detail)))

    def _summary(self, id: int) -> dict | None:
        with self._lock:
            row = self._db.execute(f"SELECT {_SUMMARY_SQL} FROM complaints WHERE id = ?",
                                   (id,)).fetchone()
        return _summary_of(row, self.home_region) if row else None

    def _write_temp(self, data: bytes) -> Path:
        """Write the upload beside its final name; os.replace later makes it appear
        whole or not at all (a crash never leaves a truncated complaint file)."""
        fd, name = tempfile.mkstemp(prefix=".upload-", suffix=".tmp", dir=self.files_dir)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            _remove(Path(name))
            raise
        return Path(name)

    # ------------------------------------------------------------ ownership

    def acquire_owner(self) -> bool:
        """Become the one process that works this register's queue, or False
        when another process (an app already running on the same data
        directory) is it. The owner, and only the owner, then sweeps what a
        crash left behind: half-written uploads and orphaned files. Held until
        close(); the OS lets go of it if the process dies."""
        with self._lock:
            if self._owner is not None:
                return True
            fh = open(self.db_path.with_name(self.db_path.name + ".lock"), "a+b")
            if not _try_lock(fh):
                fh.close()
                return False
            self._owner = fh
        # A write cut short by a crash. Only old ones: a second app on this
        # directory may be writing an upload right now.
        cutoff = datetime.now(timezone.utc).timestamp() - TEMP_MAX_AGE_S   # file times are real time
        for stale in self.files_dir.glob(".upload-*.tmp"):
            try:
                old = stale.stat().st_mtime < cutoff
            except OSError:
                continue
            if old:
                _remove(stale)
        self._orphans = True
        self.sweep_orphans()
        return True

    @property
    def is_owner(self) -> bool:
        return self._owner is not None

    def sweep_orphans(self) -> int:
        """Remove stored files whose complaint was deleted while the file could
        not be (on Windows, while a download still had it open). Runs only
        when a delete left one behind, or once when the queue is taken over."""
        if not self._orphans:
            return 0
        removed, left = 0, False
        with self._lock:
            ids = {r[0] for r in self._db.execute("SELECT id FROM complaints")}
            # create() moves a new file in just before its COMMIT. Here that
            # happens under the same lock; another process's new row has an id
            # above the highest one committed (AUTOINCREMENT), so it is skipped.
            row = self._db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'complaints'").fetchone()
            highest = row[0] if row else 0
            for path in self.files_dir.iterdir():
                match = _STORED_FILE.fullmatch(path.name)
                if match and int(match.group(1)) <= highest and int(match.group(1)) not in ids:
                    if _remove(path):
                        removed += 1
                    else:
                        left = True
            self._orphans = left
        return removed

    # --------------------------------------------------------------- intake

    def create(self, *, source: str, filename: str, file_kind: str | None,
               data: bytes | None = None, text: str | None = None,
               file_ext: str = "") -> tuple[dict, bool]:
        """(summary, duplicate). The same bytes (or the same pasted text) twice
        return the first row instead of queueing a second copy."""
        if source not in SOURCES:
            raise ValueError("unknown source")
        if file_kind is not None and file_kind not in FILE_KINDS:
            raise ValueError("unknown file kind")
        if data is None and text is None:
            raise ValueError("a complaint needs a file or text")
        payload = bytes(data) if data is not None else text.encode("utf-8")
        sha = hashlib.sha256(payload).hexdigest()
        ext = _clean_ext(file_ext, file_kind) if data is not None else ""
        # The slow part (writing up to 100 MB) happens before the lock is taken.
        temp = self._write_temp(payload) if data is not None else None
        placed = None
        try:
            with self._lock:
                with self._tx() as db:
                    row = db.execute("SELECT id FROM complaints WHERE file_sha256 = ? "
                                     "ORDER BY id LIMIT 1", (sha,)).fetchone()
                    if row is not None:
                        return self._summary(row["id"]), True
                    now_dt = self._now_dt()
                    now = _iso(now_dt)
                    cur = db.execute(
                        "INSERT INTO complaints (created_at, updated_at, source, filename, "
                        "file_kind, file_sha256, file_size, file_ext, stage, text) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)",
                        (now, now, source, filename or "", file_kind, sha,
                         len(payload) if data is not None else None, ext, text))
                    id = cur.lastrowid
                    # The ref needs the id, so it is set in the same transaction:
                    # no reader ever sees a row without one.
                    db.execute("UPDATE complaints SET ref = ? WHERE id = ?",
                               (f"CMP-{now_dt.astimezone(RIYADH).year}-{id:06d}", id))
                    self._event(db, id, "created", {"source": source, "filename": filename or ""}, now)
                    if temp is not None:
                        # Last step before COMMIT, so no SQL failure can strand the file.
                        placed = self._file(id, ext)
                        os.replace(temp, placed)
                        temp = None
                return self._summary(id), False
        except BaseException:
            if placed is not None and temp is None:
                _remove(placed)              # the row it belonged to was rolled back
            raise
        finally:
            if temp is not None:
                _remove(temp)

    # ---------------------------------------------------------------- reads

    def get(self, id: int) -> dict | None:
        with self._lock:
            row = self._db.execute(f"SELECT {_DETAIL_SQL} FROM complaints WHERE id = ?",
                                   (id,)).fetchone()
            if row is None:
                return None
            feedback = self._db.execute(
                "SELECT id, created_at, verdict, changes, note, reviewer FROM feedback "
                "WHERE complaint_id = ? ORDER BY id DESC", (id,)).fetchall()
            events = self._db.execute(
                "SELECT at, kind, detail FROM events WHERE complaint_id = ? ORDER BY id DESC",
                (id,)).fetchall()
        detail = _summary_of(row, self.home_region)
        detail.update(
            text=row["text"],
            analysis=_loads(row["analysis"], None) if row["analysis"] else None,
            timings=_loads(row["timings"], {}),
            national_id=row["national_id"],
            model_region=row["model_region"],
            processed_at=row["processed_at"],
            attempts=row["attempts"],
            file_available=bool(row["file_ext"]) and self._file(id, row["file_ext"]).is_file(),
            feedback=[{"id": f["id"], "created_at": f["created_at"], "verdict": f["verdict"],
                       "changes": _loads(f["changes"], {}), "note": f["note"],
                       "reviewer": f["reviewer"]} for f in feedback],
            events=[{"at": e["at"], "kind": e["kind"], "detail": _loads(e["detail"], {})}
                    for e in events],
        )
        analysis = detail["analysis"]
        if not isinstance(analysis, dict):
            detail["analysis"] = None
            return detail
        structured = analysis.get("structured")
        if isinstance(structured, dict) and isinstance(structured.get("fields"), list):
            structured["fields"] = _merged(structured["fields"], _field_reviews(row["field_reviews"]))
        if not detail["pending_fields"] and isinstance(analysis.get("review_reasons"), list):
            # As in the summary's list: pages that read the analysis's own
            # reasons do not show an answered fields_unverified either.
            analysis["review_reasons"] = [r for r in analysis["review_reasons"]
                                          if r != FIELDS_UNVERIFIED]
        return detail

    def list(self, *, status=None, category=None, ministry=None, priority=None, region=None,
             governorate=None, stage=None, needs_review=None, q=None, sort="created",
             order="desc", limit=200, offset=0,
             open_statuses=OPEN_STATUSES) -> tuple[list[dict], int]:
        """(page of summaries, total matching). `q` matches a substring of the ref,
        subject, summary, name, filename or national id after the Arabic folds, so
        احمد finds أحمد and ١٠٩٨ finds 1098; a national id also matches typed with
        spaces or dashes. sort="due" lists open processed complaints first (the
        ones whose deadline still matters), then closed and unprocessed ones."""
        if sort not in _SORTS:
            raise ValueError("unknown sort")
        if order not in ("asc", "desc"):
            raise ValueError("unknown order")
        where, params = [], []
        values = dict(status=status, category=category, ministry=ministry,
                      priority=priority, region=region, governorate=governorate, stage=stage)
        for column in _FILTERS:
            if values[column] not in (None, ""):
                where.append(f"{column} = ?")
                params.append(values[column])
        if needs_review not in (None, ""):
            where.append("needs_review = ?")
            params.append(1 if str(needs_review).strip().lower() in ("1", "true", "yes") else 0)
        key = _fold(q)
        if key:
            pattern = "%" + _escape_like(key) + "%"
            matches = [f"cms_fold({c}) LIKE ? ESCAPE '\\'" for c in _SEARCH_COLUMNS]
            params.extend([pattern] * len(_SEARCH_COLUMNS))
            digits = _ID_SEPARATORS.sub("", normalize_national_id(q))
            if digits != key and re.fullmatch(r"[0-9]+", digits):
                matches.append("national_id LIKE ?")          # digits only: no wildcard to escape
                params.append(f"%{digits}%")
            where.append("(" + " OR ".join(matches) + ")")
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        limit = max(0, min(int(limit), LIST_MAX))
        offset = max(0, int(offset))
        ordering, order_params = _SORTS[sort].format(o=order.upper()), []
        if sort == "due" and open_statuses:
            # In both directions: a closed complaint's deadline no longer matters.
            marks = ", ".join("?" * len(open_statuses))
            ordering = f"CASE WHEN stage = 'done' AND status IN ({marks}) THEN 0 ELSE 1 END, " + ordering
            order_params = list(open_statuses)
        with self._lock:
            total = self._db.execute("SELECT COUNT(*) FROM complaints" + clause, params).fetchone()[0]
            rows = self._db.execute(
                f"SELECT {_SUMMARY_SQL} FROM complaints{clause} "
                f"ORDER BY {ordering} LIMIT ? OFFSET ?",
                [*params, *order_params, limit, offset]).fetchall()
        return [_summary_of(r, self.home_region) for r in rows], total

    def file_path(self, id: int) -> Path | None:
        with self._lock:
            row = self._db.execute("SELECT file_ext FROM complaints WHERE id = ?", (id,)).fetchone()
        if row is None or not row["file_ext"]:
            return None
        path = self._file(id, row["file_ext"])
        return path if path.is_file() else None

    # ----------------------------------------------------------- the worker

    def claim_next(self) -> dict | None:
        """Take the oldest queued complaint: it skips OCR when it already has text
        and no re-extraction was asked for. `needs_ocr` in the result says which;
        on a re-extraction `text` is still the previous one."""
        with self._lock:
            with self._tx() as db:
                row = db.execute("SELECT id, (text IS NULL OR reocr != 0) AS needs_ocr FROM complaints "
                                 "WHERE stage = 'queued' ORDER BY id LIMIT 1").fetchone()
                if row is None:
                    return None
                id, stage = row["id"], ("ocr" if row["needs_ocr"] else "structuring")
                now_dt = self._now_dt()
                now = _iso(now_dt)
                # Timings restart with each run: a reprocess that skips OCR must
                # not report the previous run's OCR time as its own.
                db.execute("UPDATE complaints SET stage = ?, attempts = attempts + 1, "
                           "error = NULL, timings = NULL, updated_at = ? WHERE id = ?",
                           (stage, now, id))
                self._event(db, id, "stage", {"stage": stage}, now)
            self._claimed[id] = now_dt
            try:
                detail = self.get(id)
                path = self.file_path(id)
            except BaseException:
                # The worker never gets this item: it must not look held by it.
                self._claimed.pop(id, None)
                raise
        detail["file_path"] = str(path) if path else None
        detail["needs_ocr"] = stage == "ocr"
        return detail

    def is_claimed(self, id: int) -> bool:
        """Whether this process's worker holds `id` (claimed and not yet settled)."""
        with self._lock:
            return id in self._claimed

    def release_claim(self, id: int) -> None:
        """Forget a claim the worker could not settle (the database failed under
        it): the row is no longer anyone's, so delete and reprocess may touch
        it. In memory only, so it cannot fail."""
        with self._lock:
            self._claimed.pop(id, None)

    def set_stage(self, id: int, stage: str) -> None:
        if stage not in STAGES:
            raise ValueError("unknown stage")
        with self._tx() as db:
            row = db.execute("SELECT stage FROM complaints WHERE id = ?", (id,)).fetchone()
            # claim_next already set 'structuring' for text items; the pipeline's
            # own on_stage("structuring") must not log it twice.
            if row is None or row["stage"] == stage:
                return
            now = self._now()
            db.execute("UPDATE complaints SET stage = ?, updated_at = ? WHERE id = ?", (stage, now, id))
            self._event(db, id, "stage", {"stage": stage}, now)

    def save_text(self, id: int, text: str, page_count: int) -> None:
        now_dt = self._now_dt()
        with self._tx() as db:
            started = self._claimed.get(id)
            timings = ({"ocr_s": round(max(0.0, (now_dt - started).total_seconds()), 2)}
                       if started else None)
            db.execute("UPDATE complaints SET text = ?, page_count = ?, timings = ?, reocr = 0, "
                       "updated_at = ? WHERE id = ?",
                       (text, int(page_count or 0), _dumps(timings) if timings else None,
                        _iso(now_dt), id))

    def save_analysis(self, id: int, analysis: dict, *, due_at: str | None) -> None:
        """Store the pipeline's result. A reviewed complaint keeps the reviewer's
        values (and the deadline derived from them); only `model_*` move — but it
        needs review again when the new analysis finds a REVIEW_AGAIN reason the
        reviewer has not seen. Field reviews carry over: a change always, an
        accept only while the new model value is the accepted one (else the
        field is pending again). An analysis of empty text never blanks the
        subject, summary, name or national id the complaint already has."""
        structured = analysis.get("structured") or {}
        cls = analysis.get("classification") or {}
        region, governorate = structured.get("region"), structured.get("governorate")
        model = {"category": cls.get("category"), "subcategory": cls.get("subcategory"),
                 "ministry": cls.get("ministry"), "priority": cls.get("priority"),
                 "region": region.get("id") if isinstance(region, dict) else region,
                 # Absent from analyses made before governorates existed.
                 "governorate": (governorate.get("id") if isinstance(governorate, dict)
                                 else governorate)}
        reasons = analysis.get("review_reasons")
        reasons = [r for r in reasons if isinstance(r, str)] if isinstance(reasons, list) else []
        fields = _fields(analysis)
        given = {f["key"]: f.get("value") for f in fields}
        run_timings = analysis.get("timings") if isinstance(analysis.get("timings"), dict) else {}
        now = self._now()
        with self._tx() as db:
            row = db.execute("SELECT reviewed, timings, due_at, governorate, review_reasons, subject, "
                             "summary, complainant_name, national_id, field_reviews FROM complaints "
                             "WHERE id = ?", (id,)).fetchone()
            if row is None:
                return
            # A stale accept is dropped: the reviewer accepted another value.
            reviews = {k: r for k, r in _field_reviews(row["field_reviews"]).items()
                       if r["action"] == "change" or given.get(k) == r["value"]}
            values = _effective(fields, reviews)
            pending = _pending(fields, reviews)
            # save_text's ocr_s plus the pipeline's structure_s / classify_s.
            timings = {**_loads(row["timings"], {}), **run_timings}
            identity = {"subject": structured.get("subject") or "",
                        "summary": structured.get("summary") or "",
                        "complainant_name": values.get("complainant_name") or "",
                        "national_id": normalize_national_id(values.get("national_id")) or None}
            if "empty_text" in reasons:
                identity = {k: v or row[k] for k, v in identity.items()}
            sets = {
                "stage": "done", "error": None, "processed_at": now, "updated_at": now,
                "analysis": _dumps(analysis), "provider": analysis.get("provider"),
                "model": analysis.get("model"), "timings": _dumps(timings), **identity,
                "model_category": model["category"], "model_ministry": model["ministry"],
                "model_priority": model["priority"], "model_region": model["region"],
                "model_governorate": model["governorate"], "review_reasons": _dumps(reasons),
                "field_reviews": _dumps(reviews) if reviews else None, "pending_fields": pending,
            }
            if row["reviewed"]:
                # A reviewer cannot clear a governorate, so NULL means one the
                # reviewer never saw (reviewed before schema 2): the model's fills it.
                unseen = (set(reasons) & set(REVIEW_AGAIN)) - set(_reasons(row["review_reasons"]))
                sets.update(needs_review=int(_needs_review(pending, True, reasons, bool(unseen))),
                            due_at=row["due_at"] or due_at or None,
                            governorate=row["governorate"] or model["governorate"])
            else:
                sets.update(category=model["category"], subcategory=model["subcategory"],
                            ministry=model["ministry"], priority=model["priority"],
                            region=model["region"], governorate=model["governorate"],
                            due_at=due_at or None,
                            needs_review=int(_needs_review(pending, False, reasons)))
            db.execute(f"UPDATE complaints SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                       [*sets.values(), id])
            self._event(db, id, "processed", {
                "provider": analysis.get("provider"), "model": analysis.get("model"),
                "priority": model["priority"], "needs_review": bool(sets["needs_review"])}, now)
            self._claimed.pop(id, None)

    def fail(self, id: int, message: str, *, if_stuck: bool = False) -> bool:
        """Mark the complaint failed. if_stuck: only when it is still in a
        processing stage that no worker here holds (a settle the database
        refused earlier) — not once the user has requeued it."""
        marks = ", ".join("?" * len(PROCESSING))
        with self._tx() as db:
            row = db.execute(f"SELECT stage IN ({marks}) AS busy FROM complaints WHERE id = ?",
                             (*PROCESSING, id)).fetchone()
            if row is None or (if_stuck and (not row["busy"] or id in self._claimed)):
                return False
            now = self._now()
            db.execute("UPDATE complaints SET stage = 'error', error = ?, updated_at = ? "
                       "WHERE id = ?", (message, now, id))
            self._event(db, id, "error", {"message": message}, now)
            self._claimed.pop(id, None)
            return True

    def requeue(self, id: int, *, ocr: bool = False, interrupted: bool = False) -> dict | None:
        """Queue the complaint again. ocr=True asks for a re-extraction (its
        text stays until the new OCR result replaces it); ocr=False analyses
        the text it has, dropping a re-extraction asked for earlier — unless
        `interrupted` (the worker putting back what it could not finish),
        which keeps a pending re-extraction pending."""
        with self._lock:
            with self._tx() as db:
                row = db.execute("SELECT file_ext, file_kind, reocr FROM complaints WHERE id = ?",
                                 (id,)).fetchone()
                if row is None:
                    return None
                # Re-extraction needs the original file; a .txt upload's text IS
                # its file, so there is nothing to extract again.
                reocr = (bool(ocr) and bool(row["file_ext"]) and row["file_kind"] != "txt"
                         and self._file(id, row["file_ext"]).is_file())
                if interrupted:
                    reocr = reocr or bool(row["reocr"])
                now = self._now()
                db.execute("UPDATE complaints SET stage = 'queued', error = NULL, reocr = ?, "
                           "updated_at = ? WHERE id = ?", (int(reocr), now, id))
                detail = {"ocr": reocr, "interrupted": True} if interrupted else {"ocr": reocr}
                self._event(db, id, "reprocess", detail, now)
                self._claimed.pop(id, None)
            return self._summary(id)

    def reset_interrupted(self, *, max_interruptions: int | None = None, message: str = "") -> int:
        """Put back what a previous process was working on when it stopped
        (a pending re-extraction stays pending). With max_interruptions, a
        complaint interrupted that many times in a row — one that brings the
        app down or never finishes, like a huge scan — is failed with
        `message` instead of being queued forever. Returns how many were requeued."""
        marks = ", ".join("?" * len(PROCESSING))
        requeued = 0
        with self._tx() as db:
            rows = db.execute(f"SELECT id, reocr FROM complaints WHERE stage IN ({marks})",
                              PROCESSING).fetchall()
            now = self._now()
            for id, reocr in rows:
                self._claimed.pop(id, None)
                if max_interruptions and self._interruptions(db, id) + 1 >= max_interruptions:
                    db.execute("UPDATE complaints SET stage = 'error', error = ?, updated_at = ? "
                               "WHERE id = ?", (message, now, id))
                    self._event(db, id, "error", {"message": message, "interrupted": True}, now)
                    continue
                db.execute("UPDATE complaints SET stage = 'queued', updated_at = ? WHERE id = ?", (now, id))
                self._event(db, id, "reprocess", {"ocr": bool(reocr), "interrupted": True}, now)
                requeued += 1
        return requeued

    @staticmethod
    def _interruptions(db, id: int) -> int:
        """Interruptions since the complaint last finished, failed or was requeued
        by a person: the run of 'interrupted' requeues at the end of its history."""
        n = 0
        for row in db.execute("SELECT kind, detail FROM events WHERE complaint_id = ? "
                              "ORDER BY id DESC", (id,)):
            if row["kind"] == "stage":
                continue
            if row["kind"] != "reprocess" or not _loads(row["detail"], {}).get("interrupted"):
                break
            n += 1
        return n

    def queue_state(self) -> dict:
        marks = ", ".join("?" * len(PROCESSING))
        with self._lock:
            counts = {r[0]: r[1] for r in self._db.execute(
                "SELECT stage, COUNT(*) FROM complaints GROUP BY stage")}
            row = self._db.execute(
                f"SELECT id, ref, stage, filename FROM complaints WHERE stage IN ({marks}) "
                "ORDER BY updated_at DESC, id DESC LIMIT 1", PROCESSING).fetchone()
        return {"queued": counts.get("queued", 0),
                "processing": dict(row) if row else None,
                "errors": counts.get("error", 0)}

    # --------------------------------------------------------- the reviewer

    def add_feedback(self, id: int, *, verdict: str, changes: dict, note: str = "",
                     reviewer: str = "", due_at: str | None = None) -> dict | None:
        """Apply a reviewer's verdict. Only real changes are recorded as
        {"from", "to"}, so the history and the few-shot precedents see
        corrections, not clicks. (The correction analytics are net: they
        compare the model's values with the effective ones, not these steps.)
        A verdict does not answer pending fields: they keep needs_review."""
        if verdict not in ("confirm", "correct"):
            raise ValueError("unknown verdict")
        changes = changes or {}
        if set(changes) - set(FEEDBACK_FIELDS):
            raise ValueError("unknown feedback field")
        with self._lock:
            with self._tx() as db:
                row = db.execute(f"SELECT {', '.join(FEEDBACK_FIELDS)}, pending_fields "
                                 "FROM complaints WHERE id = ?", (id,)).fetchone()
                if row is None:
                    return None
                recorded = {}
                for field in FEEDBACK_FIELDS:          # fixed order: stable JSON
                    if field in changes:
                        new = changes[field] or None   # "" clears (a subcategory)
                        if new != row[field]:
                            recorded[field] = {"from": row[field], "to": new}
                now = self._now()
                sets = {field: change["to"] for field, change in recorded.items()}
                sets.update(reviewed=1, updated_at=now,
                            needs_review=int(_needs_review(row["pending_fields"], True, [])))
                if due_at:
                    sets["due_at"] = due_at
                db.execute(f"UPDATE complaints SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                           [*sets.values(), id])
                db.execute("INSERT INTO feedback (complaint_id, created_at, verdict, changes, "
                           "note, reviewer) VALUES (?, ?, ?, ?, ?, ?)",
                           (id, now, verdict, _dumps(recorded), note or "", reviewer or ""))
                self._event(db, id, "feedback", {"verdict": verdict, "fields": list(recorded)}, now)
            return self.get(id)

    def review_field(self, id: int, key: str, *, action: str, value: str | None = None,
                     reviewer: str = "", note: str = "") -> dict | None:
        """A reviewer's decision on one structured field: "accept" records its
        current value as right (`value` is ignored), "change" replaces it with
        `value` ("" clears it). `key` must be one of the analysis's fields
        (ValueError otherwise). Returns the updated detail, None for an
        unknown id."""
        if action not in FIELD_ACTIONS:
            raise ValueError("unknown field action")
        if action == "change" and (not isinstance(value, str) or len(value) > MAX_FIELD_VALUE):
            raise ValueError(f"a change needs a value of at most {MAX_FIELD_VALUE} characters")
        with self._lock:
            with self._tx() as db:
                row = db.execute("SELECT analysis, field_reviews, pending_fields, reviewed, "
                                 "needs_review, review_reasons FROM complaints WHERE id = ?",
                                 (id,)).fetchone()
                if row is None:
                    return None
                fields = _fields(_loads(row["analysis"], {}))
                if key not in {f["key"] for f in fields}:
                    raise ValueError("unknown field")
                reviews = _field_reviews(row["field_reviews"])
                before = _effective(fields, reviews)[key]
                before = before if isinstance(before, str) else ""
                after = before if action == "accept" else value
                now = self._now()
                reviews[key] = {"action": action, "value": after, "from": before,
                                "reviewer": reviewer or "", "note": note or "", "at": now}
                pending = _pending(fields, reviews)
                reasons = _reasons(row["review_reasons"])
                # A reviewed complaint flagged by pending fields alone is free
                # once they are done; one a re-analysis sent back (an unseen
                # REVIEW_AGAIN reason) still waits for a verdict. Which of the
                # two it is cannot be told when both could be: a REVIEW_AGAIN
                # reason then keeps it — one review too many beats a lost one.
                again = bool(row["reviewed"] and row["needs_review"]) and (
                    not row["pending_fields"] or bool(set(reasons) & set(REVIEW_AGAIN)))
                sets = {"field_reviews": _dumps(reviews), "pending_fields": pending,
                        "needs_review": int(_needs_review(pending, row["reviewed"], reasons, again)),
                        "updated_at": now}
                if key == "complainant_name":
                    sets["complainant_name"] = after
                elif key == "national_id":
                    sets["national_id"] = normalize_national_id(after) or None
                db.execute(f"UPDATE complaints SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                           [*sets.values(), id])
                self._event(db, id, "field_review", {"key": key, "action": action,
                                                     "from": before, "to": after}, now)
            return self.get(id)

    def set_status(self, id: int, status: str, note: str = "") -> dict | None:
        if not isinstance(status, str) or not status:
            raise ValueError("unknown status")
        with self._lock:
            with self._tx() as db:
                row = db.execute("SELECT status FROM complaints WHERE id = ?", (id,)).fetchone()
                if row is None:
                    return None
                now = self._now()
                db.execute("UPDATE complaints SET status = ?, updated_at = ? WHERE id = ?",
                           (status, now, id))
                self._event(db, id, "status", {"from": row["status"], "to": status,
                                               "note": note or ""}, now)
            return self.get(id)

    def delete(self, id: int) -> bool:
        with self._lock:
            with self._tx() as db:
                row = db.execute("SELECT file_ext FROM complaints WHERE id = ?", (id,)).fetchone()
                if row is None:
                    return False
                db.execute("DELETE FROM complaints WHERE id = ?", (id,))   # cascades
            self._claimed.pop(id, None)
            # secure_delete zeroed the freed pages in the WAL; the checkpoint
            # writes them over the main file and truncates the WAL, so the old
            # copies of the row are gone from both. Best effort: a reader may
            # keep the WAL busy for now, and the next delete tries again.
            try:
                self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            except sqlite3.Error as exc:
                _log("complaints store checkpoint error:", exc)
            # After COMMIT: a failed delete must not leave a row without its file.
            # A file still open elsewhere (Windows) is removed by a later sweep.
            if row["file_ext"] and not _remove(self._file(id, row["file_ext"])):
                self._orphans = True
        return True

    # ----------------------------------------------------- pipeline helpers

    def repeat_count(self, national_id: str, exclude_id: int | None = None) -> int:
        nid = normalize_national_id(national_id)
        if not nid:
            return 0
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM complaints WHERE national_id = ? "
                                    "AND id IS NOT ?", (nid, exclude_id)).fetchone()[0]

    def recent_corrections(self, limit: int = 3) -> list[dict]:
        """Few-shot precedents: the latest complaints a reviewer actually corrected
        (a 'correct' verdict that changed nothing teaches the model nothing)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT c.subject, c.summary, c.category, c.ministry, c.priority, MAX(f.id) AS last "
                "FROM feedback f JOIN complaints c ON c.id = f.complaint_id "
                "WHERE c.reviewed = 1 AND f.verdict = 'correct' AND f.changes != '{}' "
                "AND (COALESCE(c.subject, '') != '' OR COALESCE(c.summary, '') != '') "
                "GROUP BY c.id ORDER BY last DESC LIMIT ?",
                (max(0, min(int(limit), LIST_MAX)),)).fetchall()
        return [{"subject": r["subject"] or "", "summary": (r["summary"] or "")[:200],
                 "category": r["category"], "ministry": r["ministry"], "priority": r["priority"]}
                for r in rows]

    def priority_sources(self, ids) -> dict[int, str]:
        """{id: classification.priority_source} of the stored analyses (the
        export's «مصدر الأولوية»), without loading each complaint's detail."""
        ids = [int(i) for i in ids]
        out: dict[int, str] = {}
        for start in range(0, len(ids), LIST_MAX):
            chunk = ids[start:start + LIST_MAX]
            with self._lock:
                rows = self._db.execute(
                    "SELECT id, CASE WHEN json_valid(analysis) THEN "
                    "json_extract(analysis, '$.classification.priority_source') END "
                    f"FROM complaints WHERE id IN ({', '.join('?' * len(chunk))})", chunk).fetchall()
            out.update({r[0]: r[1] for r in rows if isinstance(r[1], str)})
        return out

    def samples(self, limit: int = 30) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT ref, subject, category, ministry, priority, region, governorate, status "
                "FROM complaints WHERE stage = 'done' ORDER BY created_at DESC, id DESC LIMIT ?",
                (max(0, min(int(limit), LIST_MAX)),)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ analytics

    def analytics(self, *, sla_hours: dict[str, int] | None = None,
                  open_statuses=OPEN_STATUSES, due_soon_hours: int = 24,
                  days: int = 30) -> dict:
        """Dashboard aggregates. Distributions, totals beyond the stage counts, SLA
        and model quality cover processed (stage 'done') rows only — a queued
        complaint has no category yet. The trend counts every row by created_at."""
        now = self._now_dt()
        days = max(1, min(int(days), 366))
        first = now.astimezone(RIYADH).date() - timedelta(days=days - 1)
        window_start = datetime.combine(first, time(), RIYADH)
        with self._lock:
            stages = {r[0]: r[1] for r in self._db.execute(
                "SELECT stage, COUNT(*) FROM complaints GROUP BY stage")}
            done = self._db.execute(
                "SELECT id, ref, created_at, category, subcategory, ministry, priority, region, "
                "governorate, status, needs_review, review_reasons, reviewed, model_category, "
                "model_ministry, model_priority, model_region, model_governorate, due_at, provider, "
                "timings, pending_fields, "
                # Only the classification is needed; json_valid guards json_extract,
                # which would otherwise fail the whole query on one damaged row.
                "CASE WHEN json_valid(analysis) THEN json_extract(analysis, '$.classification') "
                "END AS classification FROM complaints WHERE stage = 'done'").fetchall()
            per_day = {r[0]: r[1] for r in self._db.execute(
                "SELECT date(created_at, '+3 hours'), COUNT(*) FROM complaints "
                "WHERE created_at >= ? GROUP BY 1", (_iso(window_start),))}
            # Each complaint's latest verdict, so each correction cites the
            # newest reviewed complaints.
            last_review = {r[0]: r[1] for r in self._db.execute(
                "SELECT complaint_id, MAX(id) FROM feedback GROUP BY complaint_id")}

        open_set = set(open_statuses)
        open_rows = [r for r in done if r["status"] in open_set]
        classifications = [_loads(r["classification"], {}) for r in done]

        def count(column):
            return Counter(r[column] for r in done if r[column])

        by_priority = count("priority")
        cat_pri: dict[str, Counter] = {}
        for r in done:
            if r["category"]:
                cat_pri.setdefault(r["category"], Counter())[r["priority"]] += 1
        by_category = _ranked(count("category"))

        sla = {"on_track": 0, "due_soon": 0, "overdue": 0}
        soon = now + timedelta(hours=due_soon_hours)
        for r in open_rows:
            due = _parse(r["due_at"])
            if due is None and sla_hours and r["priority"] in sla_hours:
                created = _parse(r["created_at"])
                due = created + timedelta(hours=sla_hours[r["priority"]]) if created else None
            if due is not None:
                sla["overdue" if due < now else "due_soon" if due <= soon else "on_track"] += 1

        parts = {k: [] for k in _TIMING_KEYS}
        totals_s = []
        for r in done:
            timings = _loads(r["timings"], {})
            found = {k: _number(timings.get(k)) for k in _TIMING_KEYS}
            found = {k: v for k, v in found.items() if v is not None}
            for k, v in found.items():       # each average only over rows that have it
                parts[k].append(v)
            if found:
                totals_s.append(sum(found.values()))

        def mean(values):
            return round(sum(values) / len(values), 2) if values else 0.0

        reviewed = [r for r in done if r["reviewed"]]

        def agreement(field):
            # A row analysed before the model gave this field (a governorate
            # before schema 2) has no answer to agree or disagree with.
            rows = [r for r in reviewed if r["model_" + field] is not None]
            if not rows:
                return None
            return round(sum(r["model_" + field] == r[field] for r in rows) / len(rows), 4)

        # Net corrections: per reviewed complaint, the model's latest answer →
        # the value the register shows, however many feedback steps led there.
        # A value changed and changed back counts nothing; a chain counts once.
        corrections = Counter()
        cited: dict[tuple, list[str]] = {}
        for r in sorted(reviewed, key=lambda r: last_review.get(r["id"], 0), reverse=True):
            # A field the model gave no answer for is left out, as in
            # agreement(). The subcategory has no model_ column: the analysis
            # holds it, and there None is an answer (the model named none).
            model = {f: r["model_" + f] for f in FEEDBACK_FIELDS
                     if f != "subcategory" and r["model_" + f]}
            cls = _loads(r["classification"], {})
            sub = cls.get("subcategory")
            if "subcategory" in cls and (sub is None or isinstance(sub, str)):
                model["subcategory"] = sub or None
            for field, before in model.items():
                after = r[field] or None          # "" and NULL are both "none"
                if before != after:
                    key = (field, before, after)
                    corrections[key] += 1
                    refs = cited.setdefault(key, [])
                    if r["ref"] and len(refs) < TOP_CORRECTION_REFS:
                        refs.append(r["ref"])
        top = sorted(corrections.items(),
                     key=lambda kv: (-kv[1], kv[0][0], str(kv[0][1] or ""), str(kv[0][2] or "")))

        signals = Counter()
        for c in classifications:
            signals.update({s.get("id") for s in c.get("signals") or ()
                            if isinstance(s, dict) and s.get("id")})

        return {
            "generated_at": _iso(now),
            "totals": {
                "all": sum(stages.values()), "done": len(done),
                "queued": stages.get("queued", 0),
                "processing": sum(stages.get(s, 0) for s in PROCESSING),
                "error": stages.get("error", 0),
                "open": len(open_rows), "closed": len(done) - len(open_rows),
                "needs_review": sum(1 for r in done if r["needs_review"]),
                # Complaints with fields still waiting for a reviewer's accept or change.
                "fields_pending": sum(1 for r in done if r["pending_fields"]),
                "reviewed": len(reviewed), "overdue": sla["overdue"],
                "critical_open": sum(1 for r in open_rows if r["priority"] == "critical"),
                # By the effective region, so a reviewer's region correction
                # counts; the recorded reason only where the region is unknown.
                "outside_jurisdiction": sum(1 for r in done if _outside(
                    r["region"], _reasons(r["review_reasons"]), self.home_region)),
            },
            "by_category": by_category,
            "by_ministry": _ranked(count("ministry")),
            "by_priority": [{"id": p, "count": by_priority.get(p, 0)} for p in PRIORITY_ORDER],
            "by_region": _ranked(count("region")),
            "by_governorate": _ranked(count("governorate")),
            "by_status": _ranked(count("status")),
            "by_tone": _ranked(Counter(c.get("tone") for c in classifications
                                       if isinstance(c.get("tone"), str) and c.get("tone"))),
            "by_scope": _ranked(Counter(c.get("affected_scope") for c in classifications
                                        if isinstance(c.get("affected_scope"), str)
                                        and c.get("affected_scope"))),
            "trend": [{"date": d, "count": per_day.get(d, 0)} for d in
                      ((first + timedelta(days=i)).isoformat() for i in range(days))],
            "category_priority": [{"category": item["id"],
                                   **{p: cat_pri[item["id"]].get(p, 0) for p in PRIORITY_ORDER}}
                                  for item in by_category],
            "sla": sla,
            "processing": {"avg_total_s": mean(totals_s),
                           "avg_ocr_s": mean(parts["ocr_s"]),
                           "avg_structure_s": mean(parts["structure_s"]),
                           "avg_classify_s": mean(parts["classify_s"])},
            "model_quality": {
                "reviewed": len(reviewed),
                **{f"{f}_agreement": agreement(f)
                   for f in ("category", "ministry", "priority", "region", "governorate")},
                # count: complaints with that net correction; refs: at most 5 of
                # them, newest reviewed first, so insights can name them, not guess.
                "top_corrections": [{"field": f, "from": a, "to": b, "count": n,
                                     "refs": cited[(f, a, b)]}
                                    for (f, a, b), n in top[:TOP_CORRECTIONS]],
            },
            "providers": _ranked(Counter(r["provider"] for r in done if r["provider"])),
            "signals": _ranked(signals),
        }
