import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import google_takeout_import as imp

JPEG = b"\xff\xd8\xff\xe0fakejpeg"


def sidecar(taken, description="", people=None):
    return json.dumps({"photoTakenTime": {"timestamp": str(taken)}, "description": description, "people": people or []})


class SidecarMatching(unittest.TestCase):
    def match(self, media, *jsons):
        return imp.find_sidecar(media, set(jsons))

    def test_plain_and_supplemental_names(self):
        self.assertEqual(self.match("a.jpg", "a.jpg.json"), "a.jpg.json")
        self.assertEqual(self.match("a.jpg", "a.jpg.supplemental-metadata.json"), "a.jpg.supplemental-metadata.json")

    def test_truncated_supplemental_name(self):
        media = "IMG_20200101_123456_somewhat_long_name.jpg"
        truncated = (media + ".supplemental-metadata")[:46] + ".json"
        self.assertEqual(self.match(media, truncated), truncated)

    def test_truncated_plain_name(self):
        media = "a_really_quite_long_photo_filename_from_a_camera_0001.jpg"
        truncated = media[:46] + ".json"
        self.assertEqual(self.match(media, truncated), truncated)

    def test_duplicate_counter_moves_to_the_end(self):
        self.assertEqual(self.match("IMG(1).jpg", "IMG.jpg(1).json"), "IMG.jpg(1).json")
        self.assertEqual(self.match("IMG(1).jpg", "IMG.jpg.supplemental-metadata(1).json"), "IMG.jpg.supplemental-metadata(1).json")

    def test_edited_copy_uses_original_sidecar(self):
        self.assertEqual(self.match("IMG-edited.jpg", "IMG.jpg.json"), "IMG.jpg.json")

    def test_edited_copy_with_counter(self):
        self.assertEqual(self.match("DSC_000008-edited(8).JPG", "DSC_000008.JPG.supplemental-metadata(8).json"),
                         "DSC_000008.JPG.supplemental-metadata(8).json")

    def test_duplicate_without_its_own_counter_sidecar_is_not_matched_to_the_plain_one(self):
        # a different photo can share the filename, so its date/people must not be borrowed
        self.assertIsNone(self.match("DSC_2513(1).JPG", "DSC_2513.JPG.supplemental-metadata.json"))
        self.assertIsNone(self.match("DSC_000008-edited(10).JPG", "DSC_000008.JPG.supplemental-metadata.json"))

    def test_counter_sidecar_preferred_over_the_plain_one_and_plain_edited(self):
        self.assertEqual(self.match("DSC(2).JPG", "DSC.JPG.supplemental-metadata.json", "DSC.JPG.supplemental-metadata(2).json"),
                         "DSC.JPG.supplemental-metadata(2).json")
        self.assertEqual(self.match("DSC-edited.JPG", "DSC.JPG.supplemental-metadata.json"), "DSC.JPG.supplemental-metadata.json")

    def test_no_sidecar(self):
        self.assertIsNone(self.match("a.jpg", "b.jpg.json"))


class ParseSidecar(unittest.TestCase):
    def parse(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            f.write(text)
        try:
            return imp.parse_sidecar(f.name)
        finally:
            os.unlink(f.name)

    def test_reads_date_description_and_names(self):
        taken, description, names = self.parse(sidecar(1500000000, "Sunset\nover hills", [{"name": "Alice"}, {"name": " "}]))
        self.assertEqual((taken, description, names), (1500000000, "Sunset\nover hills", ["Alice"]))

    def test_empty_description_is_none(self):
        self.assertIsNone(self.parse(sidecar(1))[1])

    def test_missing_taken_time(self):
        self.assertIsNone(self.parse(json.dumps({"description": "x"}))[0])

    def test_unexpected_people_shape_is_rejected(self):
        for people in ("Alice", ["Alice"], [{"person": "Alice"}], [{"name": 3}]):
            with self.assertRaises(imp.SidecarShapeError):
                self.parse(json.dumps({"photoTakenTime": {"timestamp": "1"}, "people": people}))


class Throttle(unittest.TestCase):
    class Args:
        max_pending = 200

    def setUp(self):
        self.sleeps = []
        self._sleep, imp.time.sleep = imp.time.sleep, self.sleeps.append

    def tearDown(self):
        imp.time.sleep = self._sleep

    def api(self, *readings):
        readings = iter(readings)
        return type("Api", (), {"pending_count": lambda self: next(readings)})()

    def test_returns_once_the_queue_is_short_enough(self):
        imp.throttle(self.api(500, 300, 100), self.Args)
        self.assertEqual(len(self.sleeps), 2)

    def test_fails_closed_when_queue_depth_stays_unreadable(self):
        with self.assertRaises(imp.Fatal):
            imp.throttle(self.api(*[None] * 50), self.Args)
        self.assertEqual(len(self.sleeps), imp.UNKNOWN_QUEUE_LIMIT - 1)

    def test_a_single_blip_is_tolerated(self):
        imp.throttle(self.api(None, 5), self.Args)


class PendingCountAttempts(unittest.TestCase):
    def test_makes_a_single_attempt_per_reading(self):
        attempts = []

        class Broken:
            def request(self, *args, **kwargs):
                attempts.append(1)
                raise OSError("refused")

            def getresponse(self):
                raise OSError("refused")

        api = imp.Api("http://127.0.0.1:1", "k")
        api._conn = lambda: Broken()
        sleeps = []
        original, imp.time.sleep = imp.time.sleep, sleeps.append
        try:
            self.assertIsNone(api.pending_count())
        finally:
            imp.time.sleep = original
        self.assertEqual((len(attempts), sleeps), (1, []))


class LoadKey(unittest.TestCase):
    def test_reads_key_from_creds_env_file_without_other_vars(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write('SYSTEM="lucos_photos_import"\nKEY_LUCOS_PHOTOS="abc123"\n')
        os.environ["PHOTOS_API_KEY_FILE"] = f.name
        try:
            self.assertEqual(imp.load_key(), "abc123")
        finally:
            del os.environ["PHOTOS_API_KEY_FILE"]
            os.unlink(f.name)

    def test_exits_when_file_has_no_key(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write("SYSTEM=x\n")
        os.environ["PHOTOS_API_KEY_FILE"] = f.name
        try:
            with self.assertRaises(SystemExit):
                imp.load_key()
        finally:
            del os.environ["PHOTOS_API_KEY_FILE"]
            os.unlink(f.name)


class FakePhotos(BaseHTTPRequestHandler):
    log = []
    lock = threading.Lock()

    def _reply(self, status, body=b""):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self, body=b""):
        with self.lock:
            self.log.append((self.command, self.path, dict(self.headers), body))

    def do_GET(self):
        self._record()
        self._reply(200, json.dumps({"metrics": {"processing-pending-count": {"value": 0}}}).encode())

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self._record(body)
        if self.path == "/photos":
            status = 422 if b"rejectme" in body else 401 if self.headers["Authorization"] == "Bearer wrong" else 201
            self._reply(status, json.dumps({"id": f"id-{len(self.log)}"}).encode())
        else:
            self._reply(200, b"{}")

    def do_DELETE(self):
        self._record()
        self._reply(204)

    def log_message(self, *args):
        pass


class EndToEnd(unittest.TestCase):
    def setUp(self):
        FakePhotos.log = []
        self.server = HTTPServer(("127.0.0.1", 0), FakePhotos)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "takeout"
        self.root.mkdir()
        os.environ["PHOTOS_API_KEY"] = "secret"
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.ledger = str(Path(self.tmp.name) / "ledger.sqlite")
        self.skipped = str(Path(self.tmp.name) / "skipped.tsv")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def add(self, name, taken, description="", people=None, body=JPEG):
        (self.root / name).write_bytes(body)
        (self.root / f"{name}.json").write_text(sidecar(taken, description, people))

    def run_import(self, *extra):
        return imp.main(["import", str(self.root), "--base-url", self.base, "--ledger", self.ledger,
                         "--unmatched-log", self.skipped, *extra])

    def uploads(self):
        return [e for e in FakePhotos.log if e[0] == "POST" and e[1] == "/photos"]

    def test_imports_only_before_cutoff_and_sends_metadata(self):
        self.add("old.jpg", imp.CUTOFF_SECONDS - 1, "Line one\nCafé", [{"name": "Alice"}])
        self.add("new.jpg", imp.CUTOFF_SECONDS)
        (self.root / "orphan.jpg").write_bytes(JPEG)

        self.assertEqual(self.run_import(), 0)

        sent = self.uploads()
        self.assertEqual(len(sent), 1)
        _, _, headers, body = sent[0]
        self.assertEqual(headers["X-Taken-At"], str((imp.CUTOFF_SECONDS - 1) * 1000))
        self.assertEqual(headers["Authorization"], "Bearer secret")
        self.assertIn("Line one\nCafé".encode(), body)
        self.assertIn(b'filename="old.jpg"', body)
        self.assertIn("orphan.jpg", Path(self.skipped).read_text())

    def test_resume_does_not_repush_confirmed_files(self):
        self.add("a.jpg", 1)
        self.add("b.jpg", 2)
        self.run_import()
        self.assertEqual(len(self.uploads()), 2)

        self.add("c.jpg", 3)
        self.run_import()

        self.assertEqual(len(self.uploads()), 3)

    def test_rejected_files_are_not_retried(self):
        self.add("bad.jpg", 1, body=JPEG + b"rejectme")
        self.assertEqual(self.run_import(), 0)
        self.run_import()
        self.assertEqual(len(self.uploads()), 1)

    def test_bad_key_aborts_and_records_nothing(self):
        for i in range(5):
            self.add(f"{i}.jpg", i + 1)
        os.environ["PHOTOS_API_KEY"] = "wrong"

        self.assertEqual(self.run_import(), 2)

        os.environ["PHOTOS_API_KEY"] = "secret"
        self.assertEqual(self.run_import(), 0)
        ids = imp.Ledger(self.ledger).uploaded_ids()
        self.assertEqual(len(ids), 5)
        self.assertFalse(any(i.startswith("rejected") for i in ids))

    def test_max_per_hour_spaces_uploads_out(self):
        for i in range(3):
            self.add(f"{i}.jpg", i + 1)
        sleeps = []
        original, imp.time.sleep = imp.time.sleep, sleeps.append
        try:
            self.run_import("--max-per-hour", "3600")
        finally:
            imp.time.sleep = original

        self.assertEqual(len(self.uploads()), 3)
        # 3600 an hour is one a second: the first goes straight away, the next two wait about a second each
        waits = [s for s in sleeps if s > 0.5]
        self.assertEqual(len(waits), 2)
        self.assertTrue(all(0.9 < s <= 1.0 for s in waits), waits)

    def test_no_pacing_by_default(self):
        for i in range(3):
            self.add(f"{i}.jpg", i + 1)
        sleeps = []
        original, imp.time.sleep = imp.time.sleep, sleeps.append
        try:
            self.run_import()
        finally:
            imp.time.sleep = original
        self.assertEqual([s for s in sleeps if s > 0.5], [])

    def test_media_filter_imports_photos_then_videos_later(self):
        self.add("a.jpg", 1)
        self.add("b.mp4", 2, body=b"fakevideo")

        self.run_import("--media", "photos")
        self.assertEqual(len(self.uploads()), 1)

        self.run_import("--media", "videos")
        self.assertEqual(len(self.uploads()), 2)

    def test_limit_stops_early(self):
        for i in range(5):
            self.add(f"{i}.jpg", i + 1)
        self.run_import("--limit", "2")
        self.assertEqual(len(self.uploads()), 2)

    def test_prename_runs_tier1_then_tier2_and_resumes(self):
        self.add("one.jpg", 1, people=[{"name": "Alice"}])
        self.add("two.jpg", 2, people=[{"name": "Alice"}, {"name": "Bob"}])
        self.add("none.jpg", 3)
        self.run_import()

        self.assertEqual(imp.main(["prename", "--base-url", self.base, "--ledger", self.ledger]), 0)
        tiers = [json.loads(e[3])["tier"] for e in FakePhotos.log if e[1].endswith("/prename")]
        self.assertEqual(tiers, [1, 2])

        imp.main(["prename", "--base-url", self.base, "--ledger", self.ledger])
        self.assertEqual(len([e for e in FakePhotos.log if e[1].endswith("/prename")]), 2)

    def test_delete_refuses_ids_this_import_uploaded(self):
        uploaded = "00000000-0000-0000-0000-000000000009"
        imp.Ledger(self.ledger).record_upload("a.jpg", uploaded, [])
        ids_file = Path(self.tmp.name) / "ids.txt"
        ids_file.write_text(f"00000000-0000-0000-0000-000000000001\n{uploaded}\n")

        with self.assertRaises(SystemExit):
            imp.main(["delete", str(ids_file), "--base-url", self.base, "--ledger", self.ledger, "--expected-count", "2"])
        self.assertEqual([e for e in FakePhotos.log if e[0] == "DELETE"], [])

    def test_delete_requires_matching_count_and_resumes(self):
        ids = [f"00000000-0000-0000-0000-00000000000{i}" for i in range(3)]
        ids_file = Path(self.tmp.name) / "ids.txt"
        ids_file.write_text("\n".join(ids) + "\n")
        args = ["delete", str(ids_file), "--base-url", self.base, "--ledger", self.ledger, "--delay", "0"]

        with self.assertRaises(SystemExit):
            imp.main(args + ["--expected-count", "2"])
        self.assertEqual([e for e in FakePhotos.log if e[0] == "DELETE"], [])

        imp.main(args + ["--expected-count", "3"])
        imp.main(args + ["--expected-count", "3"])
        self.assertEqual(len([e for e in FakePhotos.log if e[0] == "DELETE"]), 3)


if __name__ == "__main__":
    unittest.main()
