#!/usr/bin/env python3
"""Resumable Google Takeout -> lucos_photos import, plus the pre-cutoff deletion phase.

Standard library only, so it runs from a stock `python` docker image (see docs/migration-runbook.md).
Uploads go through POST /photos; resume state is a local sqlite ledger written only after a 2xx.
"""

import argparse
import http.client
import json
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

CUTOFF = datetime(2026, 2, 1, tzinfo=timezone.utc)
CUTOFF_SECONDS = int(CUTOFF.timestamp())

CONTENT_TYPES = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "heic": "image/heic",
    "heif": "image/heif", "gif": "image/gif", "webp": "image/webp",
    "mp4": "video/mp4", "mov": "video/quicktime",
}
SIDECAR_SUFFIX = "supplemental-metadata"
SIDECAR_STEM_LIMIT = 46  # Takeout truncates sidecar names to this many chars before ".json"
CHUNK = 64 * 1024


def load_key():
    """The API key, from the env file named by PHOTOS_API_KEY_FILE (preferred: it stays out of container
    metadata) or from PHOTOS_API_KEY. Never logged."""
    path = os.environ.get("PHOTOS_API_KEY_FILE")  # lucos_repos: noenv PHOTOS_API_KEY_FILE
    if path:
        for line in open(path, encoding="utf-8"):
            name, _, value = line.strip().partition("=")
            if name == "KEY_LUCOS_PHOTOS" and value.strip("\"'"):
                return value.strip("\"'")
        sys.exit(f"no KEY_LUCOS_PHOTOS entry in {path}")
    key = os.environ.get("PHOTOS_API_KEY")  # lucos_repos: noenv PHOTOS_API_KEY
    if not key:
        sys.exit("set PHOTOS_API_KEY_FILE (path to the creds .env) or PHOTOS_API_KEY")
    return key


def log(msg):
    print(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {msg}", flush=True)


# ---------------------------------------------------------------------------
# Takeout parsing
# ---------------------------------------------------------------------------

def _counter_split(name):
    """'IMG_1(1).jpg' -> ('IMG_1.jpg', '(1)'); no counter -> (name, '')."""
    stem, dot, ext = name.rpartition(".")
    if dot and stem.endswith(")") and "(" in stem:
        base, _, counter = stem.rpartition("(")
        if counter[:-1].isdigit():
            return f"{base}.{ext}", f"({counter}"
    return name, ""


def _sidecar_sources(media_name):
    """(name the sidecar is built from, counter) pairs. A duplicate with a `(n)` counter only ever
    matches its own `(n)` sidecar: a different photo can share the filename, so borrowing the
    plain sidecar could attach the wrong date and people."""
    pairs = [(media_name, "")]
    base, counter = _counter_split(media_name)
    if counter:
        pairs.append((base, counter))
    edited = re.fullmatch(r"(.*)-edited(\(\d+\))?(\.[^.]+)", media_name)
    if edited:
        original = edited.group(1) + edited.group(3)
        pairs.append((original, edited.group(2) or ""))
    return pairs


def sidecar_candidates(media_name):
    """Possible sidecar filenames for a media file, most specific first.

    Takeout names a sidecar `<media>.json` or `<media>.supplemental-metadata.json`, truncates the
    part before `.json` to 46 characters, and moves a `(1)` duplicate counter from the media
    name to the end (`IMG(1).jpg` -> `IMG.jpg(1).json`). `-edited` copies (`IMG-edited(8).jpg`)
    share the original's sidecar.
    """
    out = []
    for source, counter in _sidecar_sources(media_name):
        for suffix in (f".{SIDECAR_SUFFIX}", ""):
            full = source + suffix
            for stem in (full, full[:SIDECAR_STEM_LIMIT]):
                out.append(f"{stem}{counter}.json")
    return list(dict.fromkeys(out))


def find_sidecar(media_name, json_names):
    for cand in sidecar_candidates(media_name):
        if cand in json_names:
            return cand
    return None


class SidecarShapeError(Exception):
    pass


class Fatal(Exception):
    """Something is wrong with the whole run (bad key, wrong URL), not one file."""


REJECTED_STATUSES = (400, 413, 415, 422)  # per-file refusals: the server will never accept this file
FATAL_STATUSES = (401, 403, 404)


def parse_sidecar(path):
    """Return (taken_seconds | None, description | None, [people names]). Raises SidecarShapeError
    if `people` isn't the documented list of {"name": str} objects."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    taken = None
    try:
        taken = int(data["photoTakenTime"]["timestamp"])
    except (KeyError, TypeError, ValueError):
        pass
    description = data.get("description") or None
    people = data.get("people", [])
    if not isinstance(people, list) or not all(isinstance(p, dict) and isinstance(p.get("name"), str) for p in people):
        raise SidecarShapeError(f"unexpected 'people' shape: {json.dumps(people)[:200]}")
    return taken, description, [p["name"] for p in people if p["name"].strip()]


def iter_takeout(root):
    """Yield (relative path, absolute path, sidecar absolute path | None) for every media file."""
    for dirpath, _, filenames in os.walk(root):
        json_names = {n for n in filenames if n.endswith(".json")}
        for name in sorted(filenames):
            if name.rsplit(".", 1)[-1].lower() not in CONTENT_TYPES or name.endswith(".json"):
                continue
            sidecar = find_sidecar(name, json_names)
            full = os.path.join(dirpath, name)
            yield os.path.relpath(full, root), full, (os.path.join(dirpath, sidecar) if sidecar else None)


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

class Ledger:
    """Append-only record of confirmed uploads and their pre-naming progress."""

    def __init__(self, path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS uploaded (
            path TEXT PRIMARY KEY, photo_id TEXT NOT NULL, names TEXT NOT NULL,
            tier1_done INTEGER NOT NULL DEFAULT 0, tier2_done INTEGER NOT NULL DEFAULT 0)""")
        self.db.execute("CREATE TABLE IF NOT EXISTS deleted (photo_id TEXT PRIMARY KEY)")
        self.db.commit()

    def uploaded(self, path):
        with self.lock:
            return self.db.execute("SELECT 1 FROM uploaded WHERE path=?", (path,)).fetchone() is not None

    def uploaded_ids(self):
        with self.lock:
            return {r[0] for r in self.db.execute("SELECT photo_id FROM uploaded")}

    def record_upload(self, path, photo_id, names):
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO uploaded(path, photo_id, names) VALUES (?,?,?)",
                            (path, photo_id, json.dumps(names)))
            self.db.commit()

    def pending_prenames(self, tier):
        col = "tier1_done" if tier == 1 else "tier2_done"
        with self.lock:
            rows = self.db.execute(f"SELECT path, photo_id, names FROM uploaded WHERE {col}=0").fetchall()
        out = []
        for path, photo_id, names in rows:
            names = json.loads(names)
            if (len(names) == 1) if tier == 1 else (len(names) > 1):
                out.append((path, photo_id, names))
        return out

    def mark_prenamed(self, path, tier):
        col = "tier1_done" if tier == 1 else "tier2_done"
        with self.lock:
            self.db.execute(f"UPDATE uploaded SET {col}=1 WHERE path=?", (path,))
            self.db.commit()

    def deleted(self, photo_id):
        with self.lock:
            return self.db.execute("SELECT 1 FROM deleted WHERE photo_id=?", (photo_id,)).fetchone() is not None

    def record_delete(self, photo_id):
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO deleted(photo_id) VALUES (?)", (photo_id,))
            self.db.commit()


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

class Api:
    def __init__(self, base_url, key, retries=6):
        parts = urlsplit(base_url)
        self.https = parts.scheme == "https"
        self.host, self.port = parts.hostname, parts.port
        self.prefix = parts.path.rstrip("/")
        self.key = key
        self.retries = retries

    def _conn(self):
        cls = http.client.HTTPSConnection if self.https else http.client.HTTPConnection
        return cls(self.host, self.port, timeout=300)

    def request(self, method, path, body=None, headers=None, retries=None):
        """Send one request, retrying connection errors, 5xx (incl. 507 disk-full) and 429 with backoff.
        `body` may be bytes or a callable returning an iterable of bytes (re-invoked per attempt)."""
        hdrs = {"Authorization": f"Bearer {self.key}", **(headers or {})}
        retries = self.retries if retries is None else retries
        for attempt in range(retries + 1):
            try:
                conn = self._conn()
                conn.request(method, self.prefix + path, body=body() if callable(body) else body, headers=hdrs)
                resp = conn.getresponse()
                data = resp.read()
                conn.close()
                if resp.status < 500 and resp.status != 429:
                    return resp.status, data
                err = f"HTTP {resp.status}"
            except (OSError, http.client.HTTPException) as exc:
                err = repr(exc)
            if attempt == retries:
                raise RuntimeError(f"{method} {path} failed after {retries + 1} attempts: {err}")
            delay = min(2 ** attempt * 5, 300)
            log(f"{method} {path}: {err}; retrying in {delay}s")
            time.sleep(delay)

    def pending_count(self):
        try:
            status, data = self.request("GET", "/_info", retries=0)  # one attempt: throttle() does the waiting
        except RuntimeError:
            return None
        if status != 200:
            return None
        try:
            return json.loads(data)["metrics"]["processing-pending-count"]["value"]
        except (KeyError, ValueError, TypeError):
            return None


def multipart_upload(api, full_path, taken_ms, description):
    boundary = uuid.uuid4().hex
    name = os.path.basename(full_path)
    ctype = CONTENT_TYPES[name.rsplit(".", 1)[-1].lower()]
    pre = b""
    if description:
        pre += (f'--{boundary}\r\nContent-Disposition: form-data; name="description"\r\n\r\n').encode() + description.encode("utf-8") + b"\r\n"
    safe_name = name.encode("utf-8", "replace").decode("utf-8").replace('"', "'")
    pre += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n").encode("utf-8")
    post = f"\r\n--{boundary}--\r\n".encode()
    length = len(pre) + os.path.getsize(full_path) + len(post)

    def body():
        yield pre
        with open(full_path, "rb") as f:
            while chunk := f.read(CHUNK):
                yield chunk
        yield post

    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": str(length)}
    if taken_ms:
        headers["X-Taken-At"] = str(taken_ms)
    return api.request("POST", "/photos", body=body, headers=headers)


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------

def scan(root, stats, unmatched_log=None):
    """Yield (rel, full, taken_seconds, description, names) for files before the cutoff."""
    for rel, full, sidecar in iter_takeout(root):
        stats["media"] += 1
        if sidecar is None:
            stats["no_sidecar"] += 1
            if unmatched_log:
                unmatched_log.write(f"no-sidecar\t{rel}\n")
            continue
        try:
            taken, description, names = parse_sidecar(sidecar)
        except SidecarShapeError as exc:
            stats["bad_people_shape"] += 1
            log(f"WARNING {rel}: {exc}")
            if unmatched_log:
                unmatched_log.write(f"bad-shape\t{rel}\n")
            continue
        except (OSError, ValueError) as exc:
            stats["bad_sidecar"] += 1
            log(f"WARNING {rel}: unreadable sidecar: {exc}")
            continue
        if taken is None:
            stats["no_taken_time"] += 1
            if unmatched_log:
                unmatched_log.write(f"no-taken-time\t{rel}\n")
            continue
        if taken >= CUTOFF_SECONDS:
            stats["after_cutoff"] += 1
            continue
        yield rel, full, taken, description, names


def media_kind(path):
    return "videos" if CONTENT_TYPES[path.rsplit(".", 1)[-1].lower()].startswith("video/") else "photos"


def new_stats():
    return {k: 0 for k in ("media", "no_sidecar", "bad_people_shape", "bad_sidecar", "no_taken_time",
                           "after_cutoff", "to_import", "already_uploaded", "uploaded", "failed", "named_photos")}


def cmd_dry_run(args):
    stats = new_stats()
    stats.update(videos=0, bytes_to_import=0)
    shown = 0
    with open(args.unmatched_log, "a") as unmatched:
        for rel, full, taken, description, names in scan(args.takeout_dir, stats, unmatched):
            stats["to_import"] += 1
            stats["bytes_to_import"] += os.path.getsize(full)
            stats["videos"] += media_kind(full) == "videos"
            if names:
                stats["named_photos"] += 1
                if shown < 5:
                    shown += 1
                    log(f"sample: {rel} taken={datetime.fromtimestamp(taken, timezone.utc).date()} people={names}")
    log("dry run (nothing uploaded): " + json.dumps(stats))


UNKNOWN_QUEUE_LIMIT = 10  # consecutive unreadable queue depths (about 5 minutes) before giving up


def throttle(api, args):
    """Block while the worker's pending queue is above the limit. Fails closed: if the queue depth
    can't be read for UNKNOWN_QUEUE_LIMIT checks in a row, abort rather than upload unthrottled."""
    unknown = 0
    while True:
        pending = api.pending_count()
        if pending is None:
            unknown += 1
            if unknown >= UNKNOWN_QUEUE_LIMIT:
                raise Fatal("can't read the worker queue depth from /_info; stop and look before resuming")
            log(f"queue depth unreadable ({unknown}/{UNKNOWN_QUEUE_LIMIT}); pausing 30s")
        elif pending <= args.max_pending:
            return
        else:
            unknown = 0
            log(f"queue depth {pending} > {args.max_pending}; pausing 30s")
        time.sleep(30)


def prename_pass(api, ledger, tier, args):
    """Tier 1 / Tier 2 pass over everything uploaded. 409 means the photo isn't processed yet."""
    done = failed = 0
    while True:
        todo = ledger.pending_prenames(tier)
        if not todo:
            break
        waiting = 0
        for path, photo_id, names in todo:
            status, data = api.request("POST", f"/photos/{photo_id}/prename",
                                       body=json.dumps({"tier": tier, "names": names}).encode(),
                                       headers={"Content-Type": "application/json"})
            if status == 200:
                ledger.mark_prenamed(path, tier)
                done += 1
            elif status == 409 and json.loads(data).get("processingStatus") != "failed":
                waiting += 1
            elif status in (404, 409, 422):
                # photo gone, or failed processing, or unusable names: nothing more to do for this one
                ledger.mark_prenamed(path, tier)
                failed += 1
                log(f"WARNING tier {tier} {path}: HTTP {status} {data[:200]!r}")
            else:
                sys.exit(f"prename: HTTP {status} {data[:200]!r}; stopping (check the key and --base-url, then re-run to resume)")
        if waiting:
            log(f"tier {tier}: {waiting} photo(s) still processing; waiting 60s")
            time.sleep(60)
    log(f"tier {tier} complete: {done} done, {failed} gave up")


def cmd_import(args):
    key = load_key()
    api, ledger, stats = Api(args.base_url, key), Ledger(args.ledger), new_stats()
    unmatched = open(args.unmatched_log, "a")

    def upload(item):
        rel, full, taken, description, names = item
        status, data = multipart_upload(api, full, taken * 1000, description)
        if status in FATAL_STATUSES:
            raise Fatal(f"HTTP {status} from POST /photos; check the key and --base-url")
        if status in REJECTED_STATUSES:
            # The server won't ever accept this file (e.g. invalid image); record it so a resume doesn't resend it.
            ledger.record_upload(rel, f"rejected-{status}", [])
            unmatched.write(f"rejected-{status}\t{rel}\t{data[:200]!r}\n")
            unmatched.flush()
            return rel
        if status not in (200, 201):
            raise RuntimeError(f"HTTP {status} {data[:200]!r}")
        ledger.record_upload(rel, json.loads(data)["id"], names)
        return rel

    with unmatched, ThreadPoolExecutor(args.concurrency) as pool:
        inflight, fatal = {}, []

        def drain(block):
            if not inflight:
                return
            done, _ = wait(inflight, return_when=FIRST_COMPLETED, timeout=None if block else 0)
            for fut in done:
                rel = inflight.pop(fut)
                try:
                    fut.result()
                    stats["uploaded"] += 1
                    if stats["uploaded"] % 100 == 0:
                        log("progress: " + json.dumps(stats))
                except Fatal as exc:
                    fatal.append(exc)
                except Exception as exc:
                    stats["failed"] += 1
                    log(f"FAILED {rel}: {exc}")

        interval = 3600 / args.max_per_hour if args.max_per_hour else 0
        next_upload = 0.0
        for item in scan(args.takeout_dir, stats, unmatched):
            if fatal:
                break
            if args.media != "all" and media_kind(item[1]) != args.media:
                continue
            stats["to_import"] += 1
            if ledger.uploaded(item[0]):
                stats["already_uploaded"] += 1
                continue
            if args.limit and stats["uploaded"] + len(inflight) >= args.limit:
                break
            if stats["to_import"] % 20 == 0:
                try:
                    throttle(api, args)
                except Fatal as exc:
                    fatal.append(exc)
                    break
            while len(inflight) >= args.concurrency:
                drain(True)
            if interval:
                time.sleep(max(0.0, next_upload - time.monotonic()))
                next_upload = time.monotonic() + interval
            inflight[pool.submit(upload, item)] = item[0]
            drain(False)
        while inflight:
            drain(True)
    log("upload phase finished: " + json.dumps(stats))
    if fatal:
        log(f"ABORTED: {fatal[0]}")
        return 2
    if stats["failed"]:
        log("Some uploads failed; re-run the same command to retry them (the ledger skips the rest).")
    return 1 if stats["failed"] else 0


def cmd_prename(args):
    key = load_key()
    api, ledger = Api(args.base_url, key), Ledger(args.ledger)
    prename_pass(api, ledger, 1, args)  # Tier 2 needs Tier 1's confirmed faces across the whole batch
    prename_pass(api, ledger, 2, args)
    return 0


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------

def cmd_delete(args):
    key = load_key()
    ids = [line.strip() for line in open(args.ids_file) if line.strip()]
    if len(ids) != args.expected_count:
        sys.exit(f"{args.ids_file} has {len(ids)} ids but --expected-count is {args.expected_count}; refusing to delete")
    for photo_id in ids:
        uuid.UUID(photo_id)
    api, ledger = Api(args.base_url, key), Ledger(args.ledger)
    imported = ledger.uploaded_ids() & set(ids)
    if imported:
        sys.exit(f"{len(imported)} id(s) in {args.ids_file} were uploaded by this import (see the ledger); "
                 "deleting them would leave the ledger skipping photos that no longer exist. "
                 "Run the deletion before any import, or rebuild the id list.")
    done = skipped = missing = 0
    for photo_id in ids:
        if ledger.deleted(photo_id):
            skipped += 1
            continue
        status, data = api.request("DELETE", f"/photos/{photo_id}")
        if status == 204:
            done += 1
        elif status == 404:
            missing += 1
        else:
            sys.exit(f"DELETE {photo_id}: HTTP {status} {data[:200]!r}; stopping (re-run to resume)")
        ledger.record_delete(photo_id)
        if (done + missing) % 500 == 0:
            log(f"deleted {done}, already gone {missing}, skipped {skipped} of {len(ids)}")
        time.sleep(args.delay)
    log(f"delete finished: {done} deleted, {missing} already gone, {skipped} skipped from ledger")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--base-url", default=os.environ.get("PHOTOS_BASE_URL", "https://photos.l42.eu"))  # lucos_repos: noenv PHOTOS_BASE_URL
    common.add_argument("--ledger", default="migration-ledger.sqlite")

    imp = sub.add_parser("import", parents=[common], help="upload pre-cutoff photos (run `prename` once all uploads are done)")
    imp.add_argument("takeout_dir")
    imp.add_argument("--concurrency", type=int, default=2)
    imp.add_argument("--max-pending", type=int, default=200, help="pause uploads while the worker queue is deeper than this")
    imp.add_argument("--limit", type=int, default=0, help="stop after this many uploads (trial runs)")
    imp.add_argument("--max-per-hour", type=int, default=0, help="space uploads out to at most this many an hour, so the worker idles between items (0 = no limit)")
    imp.add_argument("--media", choices=("all", "photos", "videos"), default="all", help="import only photos or only videos (skipped files aren't recorded, so a later run picks them up)")
    imp.add_argument("--unmatched-log", default="migration-skipped.tsv")
    imp.set_defaults(func=cmd_import)

    pre = sub.add_parser("prename", parents=[common], help="Tier 1 then Tier 2 face pre-naming for everything in the ledger")
    pre.set_defaults(func=cmd_prename)

    dry = sub.add_parser("dry-run", help="parse the export and report what would be imported; uploads nothing")
    dry.add_argument("takeout_dir")
    dry.add_argument("--unmatched-log", default="migration-skipped.tsv")
    dry.set_defaults(func=lambda a: cmd_dry_run(a) or 0)

    dele = sub.add_parser("delete", parents=[common], help="delete the photos listed in a frozen id file")
    dele.add_argument("ids_file")
    dele.add_argument("--expected-count", type=int, required=True)
    dele.add_argument("--delay", type=float, default=0.2, help="seconds between deletes")
    dele.set_defaults(func=cmd_delete)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
