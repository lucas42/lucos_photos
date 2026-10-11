# Google Photos migration runbook

Cutover plan from lucas42/lucos_photos#424, implemented by `migration/google_takeout_import.py` (lucas42/lucos_photos#427, #469).
Photos taken **before 1 Feb 2026 (UTC)** come from Google; everything on/after comes from the phone. The cutoff is on date *taken* in both systems.

**Order is not negotiable: confirm the backup → delete pre-cutoff → trial import → full import → pre-name.** The deletion comes before any import, because it would otherwise delete freshly imported pre-cutoff photos (`delete` refuses ids found in the ledger). The deletion is irreversible, so it only runs once a current snapshot is confirmed to exist.

No separate trial restore is required beforehand. lucas42 decided this in chat on 2026-10-11 ("no need for a trial of the specific snapshot"): the 2026-09-16 avalon rebuild already restored the photos volume from aurora's incremental snapshot and verified it (lucas42/lucos#294).

## Who does what

| Step | Who |
|---|---|
| Request the Takeout export, authorise its transfer to avalon (e.g. one-off `rclone authorize drive` token) | lucas42 |
| Create the import key: `ssh -p 2202 creds.l42.eu "lucos_photos_import/production => lucos_photos/production"` (no `|scope`), then the team redeploys lucos_photos. Fetch it with `scp -P 2202 creds.l42.eu:lucos_photos_import/production/.env .` (it's `KEY_LUCOS_PHOTOS`) | lucas42 |
| Go-ahead on the deletion count (step 5) | lucas42 |
| Everything else (backup check, trial, deletion, import, pre-naming, resume, checks) | SRE / sysadmin via prod SSH + docker |

## The script

Standard library only. Run it from a stock python image on avalon, with the export, the ledger and the id file on a disk-backed directory (not `/tmp`, which is RAM-backed):

```bash
W=/srv/photos-migration        # holds takeout/, migration-ledger.sqlite, ids.txt
run() { docker run --rm -e PHOTOS_API_KEY -v "$W":/work -v "$PWD/migration":/app:ro -w /work python:3.14-slim python /app/google_takeout_import.py "$@"; }
export PHOTOS_API_KEY=...      # KEY_LUCOS_PHOTOS from the lucos_photos_import creds; never put it on a command line
```

Run long commands under `Monitor`/`nohup` so they can be stopped part-way. Subcommands:

- `dry-run <dir>` parses the export and uploads nothing. It reports how many files are before the cutoff, have no sidecar, or have an unexpected `people` shape, and prints a few sample `people` values. Skipped files and reasons go to `migration-skipped.tsv`.
- `import <dir> [--concurrency 2] [--max-pending 200] [--limit N]` uploads via `POST /photos`. It pauses while `/_info` reports more than `--max-pending` pending items.
- `prename` runs Tier 1 then Tier 2 face pre-naming over everything in the ledger.
- `delete <ids-file> --expected-count N` deletes from a frozen list of ids.

The ledger (`migration-ledger.sqlite`) records a file only after a confirmed 2xx. Files the server permanently rejects (4xx, e.g. an invalid image) are recorded too and listed in `migration-skipped.tsv`, so a resume doesn't resend them.

## Steps

1. **First look at the export.** As soon as any Takeout export exists (even a small one), run `dry-run` on it and check the sample output: the `people` values must be plain names, `no_sidecar` should be near zero, and `bad_people_shape` must be zero. If not, stop and tell the developer — the sidecar parsing was written from documentation, not a real export (acceptance criterion on #469).
2. **Confirm the backup.** The photos volume already has an incremental chain, so no fresh seed is expected. Check that the latest snapshot under `/share/backups/host/avalon/volume-snapshots/lucos_photos_photos/` on aurora is complete and recent (and, if unsure, read lucos_backups `scripts/seed-volume.py` before seeding anything). Do not run any large backup operation during the nightly backup window.
3. *(No trial restore — see above.)*
4. **Deletion — on the 7th of a month.** Retention keeps only the 6th-of-month snapshots for a year, so deleting on the 7th keeps a restore point from the day before. Confirm the 6th's snapshot completed first (step 2), then freeze the ids:

   ```bash
   docker exec lucos_photos_postgres psql -U photos -d photos -Atc \
     "SELECT id FROM media_item WHERE taken_at < '2026-02-01T00:00:00Z' ORDER BY id" > "$W/ids.txt"
   wc -l "$W/ids.txt"
   docker exec lucos_photos_postgres psql -U photos -d photos -Atc \
     "SELECT count(*) FROM media_item WHERE taken_at IS NULL"
   ```

   Show lucas42 both counts. Rows with a null `taken_at` are *not* in the list; decide about them explicitly before deleting. After his go-ahead: `delete "$W/ids.txt" --expected-count <wc -l>`.
5. **Trial import.** With the key in place, `import <dir> --limit 200`, watching `docker stats` and `free -h` on avalon. Tune `--concurrency` (start at 2, never above 4) and check the backup run that night still finishes in its window.
6. **Full import.** `import <dir>`. If it is interrupted, or reports failures, run the exact same command again: the ledger skips everything already confirmed. Throttle to what the nightly backup copy can absorb (about 11MB/s over the home WAN leg); a full-speed import makes every night a large incremental.
7. **Pre-naming.** Once `import` finishes with no failures, run `prename`. It waits for each photo to finish processing (the API answers 409 until then), runs Tier 1 across the whole batch, and only then Tier 2. It is also resumable. Don't run it on a partial import: Tier 2 depends on Tier 1 having assigned confirmed faces across the whole batch.
8. **Post-import checks.**
   - photo count roughly equals the `after_cutoff`-excluded total from `dry-run`, plus the photos taken on/after the cutoff already in lucos_photos;
   - spot-check a few photos in `.photo-metadata`: taken date and description match the sidecars;
   - a few named people have photos and sensible face assignments; remaining duplicates (spelling variants) are merged with `POST /people/merge`;
   - `/_info` is healthy and the worker queue has drained.

## Resume and failure handling

- Upload interrupted → rerun the same `import` command.
- `507` from the API means disk is low: the script backs off and retries on its own; check free space on avalon.
- A `401`/`403`/`404` from the API aborts `import` and `prename` (nothing is marked as done), so a wrong key or `--base-url` costs nothing: fix it and rerun. `delete` stops on the first unexpected status and resumes from its ledger; ids already gone (404) are counted, not errors.
- Descriptions are sent as a form field (the `X-Description` header can't carry newlines or non-ASCII text).

## Judgement calls worth knowing

- Photos and their `-edited` copies are both imported (different bytes, same sidecar), so edited photos appear twice.
- Files with no sidecar, or no `photoTakenTime`, are skipped and listed rather than imported blind, because they can't be placed on the correct side of the cutoff.
- Tier 2 assignments are *unconfirmed* (an ML-grade guess, like auto-assignment); only Tier 1 sets `person_confirmed`. Tier 2 uses `FACE_SIMILARITY_THRESHOLD`.
- Deleting a photo now also deletes any person it leaves with no faces (unless linked to a contact); names from Google recreate or re-match people by exact case-insensitive `display_name`.
