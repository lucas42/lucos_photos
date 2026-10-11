# Google Photos migration runbook

Cutover plan from lucas42/lucos_photos#424, implemented by `migration/google_takeout_import.py` (lucas42/lucos_photos#427, #469).
Photos taken **before 1 Feb 2026 (UTC)** come from Google; everything on/after comes from the phone. The cutoff is on date *taken* in both systems.

**Order is not negotiable: confirm the backup → delete pre-cutoff → trial import → full import → pre-name.** The deletion comes before any import, because it would otherwise delete freshly imported pre-cutoff photos (`delete` refuses ids found in the ledger). The deletion is irreversible, so it only runs once a current snapshot is confirmed to exist.

No separate trial restore is required beforehand. lucas42 decided this in chat on 2026-10-11 ("no need for a trial of the specific snapshot"): the 2026-09-16 avalon rebuild already restored the photos volume from aurora's incremental snapshot and verified it (lucas42/lucos#294).

## Who does what

| Step | Who |
|---|---|
| Request the Takeout export, authorise its transfer to avalon (e.g. one-off `rclone authorize drive` token) | lucas42 |
| Create the import key (done), and put it on avalon with the two commands under "Handing the key to avalon" below | lucas42 |
| Run the backup coverage check (step 4's one command) and give the go-ahead on the deletion count | lucas42 |
| Everything else (backup check, trial, deletion, import, pre-naming, resume, checks) | SRE / sysadmin via prod SSH + docker |

## The script

Standard library only. Run it from a stock python image on avalon, with the export, the ledger and the id file on a disk-backed directory (not `/tmp`, which is RAM-backed):

```bash
W=/srv/photos-migration        # holds takeout/, import.env, migration-ledger.sqlite, ids.txt
run() { docker run --rm -e PHOTOS_API_KEY_FILE=/work/import.env -v "$W":/work -v "$PWD/migration":/app:ro -w /work python:3.14-slim python /app/google_takeout_import.py "$@"; }
```

The script reads `KEY_LUCOS_PHOTOS` from `import.env` itself (quotes stripped), so the key never appears in the environment, on a command line or in `docker inspect`.

Run long commands under `Monitor`/`nohup` so they can be stopped part-way. Subcommands:

- `dry-run <dir>` parses the export and uploads nothing. It reports how many files are before the cutoff, have no sidecar, or have an unexpected `people` shape, and prints a few sample `people` values. Skipped files and reasons go to `migration-skipped.tsv`.
- `import <dir> [--concurrency 2] [--max-pending 200] [--limit N]` uploads via `POST /photos`. It pauses while `/_info` reports more than `--max-pending` pending items.
- `prename` runs Tier 1 then Tier 2 face pre-naming over everything in the ledger.
- `delete <ids-file> --expected-count N` deletes from a frozen list of ids.

The ledger (`migration-ledger.sqlite`) records a file only after a confirmed 2xx. Files the server permanently rejects (4xx, e.g. an invalid image) are recorded too and listed in `migration-skipped.tsv`, so a resume doesn't resend them.

## Handing the key to avalon

Production credentials can't be read by agents and the value must never go through chat, GitHub or a log. lucas42 puts it straight onto avalon with one sudo account, in two commands run from his laptop (the `ssh` host is avalon):

```bash
# 1. Once: the working directory, owned by the operator, not readable by anyone else (this is also where the Takeout zips go)
ssh avalon.s.l42.eu 'sudo install -d -o lucos-agent -g docker -m 700 /srv/photos-migration'

# 2. Before the deletion (step 4; `delete` authenticates with this key too, so it is also needed before the trial import): the key, copied in one pipe so it never lands on his laptop's disk or in a shell variable
scp -P 2202 creds.l42.eu:lucos_photos_import/production/.env /dev/stdout \
  | ssh avalon.s.l42.eu 'sudo install -o lucos-agent -g docker -m 600 /dev/stdin /srv/photos-migration/import.env'
```

Rules for everyone afterwards: don't `cat` the file, run `env`, enable `set -x`, or paste `docker inspect` output or request logs into chat or GitHub. The script never logs the key or request headers. Before the first run, the system administrator confirms `/srv/photos-migration` isn't in any backup scope (it holds a credential and 78GB of export).

The key can upload **and** delete photos (it's an unscoped `CLIENT_KEYS` entry). `import` and `prename` contain no delete call; the only delete path is the explicit `delete` subcommand, which needs a frozen id file and a matching `--expected-count`.

**Lifetime** (lucos-security). The limit is tied to the migration, not the calendar:

- **Primary:** revoke within 48 hours of the import being verified complete (step 9).
- **Backstop:** a hard limit of 21 days from first use (the deletion, step 4, is its first use). If the import isn't done by then, extending is a deliberate decision recorded on #577, not a default.
- **Stall rule:** if the import is stopped or stalled for more than 3 days, revoke rather than leaving a deletion-capable key idle on a host, and re-mint when it restarts.
- Revocation is the real cleanup; deleting the file is secondary (`shred` means little on a journaling filesystem). Delete `import.env` right after the last run finishes, not at the end of the migration.

**Owner:** whoever runs the import (SRE/sysadmin) owns revocation and the dates above, and prompts lucas42 for the creds command, which only he can run.

## Steps

1. **First look at the export.** As soon as any Takeout export exists (even a small one), run `dry-run` on it and check the sample output: the `people` values must be plain names, `no_sidecar` should be near zero, and `bad_people_shape` must be zero. If not, stop and tell the developer — the sidecar parsing was written from documentation, not a real export (acceptance criterion on #469).
2. **Confirm the backups exist** (first half of the gate before the deletion; no trial restore is needed). Everything the deletion removes was taken before 2026-02-01 and originals are immutable (stored by SHA-256), so any pre-deletion backup that contains those files is as good as the day before's, and the date of the deletion doesn't matter. Use the 2026-10-06 backups: they sit on the long retention track (kept until about 2027-10-06, the 6th of each month for a year, for both volumes). Confirm both are present on aurora:
   - the photos snapshot directory `/share/backups/host/avalon/volume-snapshots/lucos_photos_photos/2026-10-06/` (a top-level listing of the backup directory can wrongly show nothing: the lucas42/lucos#294 lesson);
   - the postgres tarball `lucos_photos_postgres_data.2026-10-06.tar.gz` under `host/avalon/volume/` on aurora. Postgres uses the default daily tarball strategy, *not* `volume-snapshots/`. The copy under `/srv/backups/local/volume/` on avalon does **not** count for this gate: it sits on avalon's single disk, so it would be lost in exactly the host failure a long-lived restore point exists to survive (avalon's disk already died once, lucas42/lucos#294).

   **Nobody on the team can see aurora, so for now lucas42 performs these checks** (or arranges read access for the operator). They are in the one command in step 4.
3. *(No trial restore — see above.)*
4. **Deletion.** The date doesn't matter; run it once the checks below pass. First freeze the ids and the matching original filenames in one query, and show lucas42 the counts:

   ```bash
   docker exec lucos_photos_postgres psql -U photos -d photos -Atc \
     "SELECT id, sha256_hash || '.' || file_extension FROM media_item WHERE taken_at < '2026-02-01T00:00:00Z' ORDER BY id" > "$W/frozen.txt"
   cut -d'|' -f1 "$W/frozen.txt" > "$W/ids.txt"; cut -d'|' -f2 "$W/frozen.txt" | sort > "$W/original-names.txt"
   wc -l "$W/ids.txt"
   docker exec lucos_photos_postgres psql -U photos -d photos -Atc \
     "SELECT count(*) FROM media_item WHERE taken_at IS NULL"
   ```

   Rows with a null `taken_at` are *not* in the list; decide about them explicitly before deleting.

   **Coverage and presence check (lucas42, one command, no manual comparison).** A photo uploaded after 2026-10-06 (e.g. by a phone resync) wouldn't be in the 10-06 snapshot, so every frozen original must be in it. From a machine that can reach both avalon and aurora, this prints the number of frozen originals **missing** from the snapshot (expected `0`) and checks both backups exist:

   ```bash
   (
     set -eo pipefail   # any failed ssh/sudo aborts the check instead of printing a clean-looking 0
     S=/share/backups/host/avalon/volume-snapshots/lucos_photos_photos/2026-10-06
     ssh aurora "ls -d $S /share/backups/host/avalon/volume/lucos_photos_postgres_data.2026-10-06.tar.gz" > /dev/null
     snap=$(ssh aurora "ls $S/originals" | sort)
     want=$(ssh avalon.s.l42.eu 'sudo cat /srv/photos-migration/original-names.txt' | sort)
     test -n "$snap" && test -n "$want"
     echo "frozen originals:     $(printf '%s\n' "$want" | wc -l)   (must equal the id count the operator showed you)"
     echo "originals in snapshot: $(printf '%s\n' "$snap" | wc -l)"
     echo "frozen but missing:    $(comm -13 <(printf '%s\n' "$snap") <(printf '%s\n' "$want") | wc -l)   (must be 0)"
   )
   ```

   If any command errors, the check has not passed (it stops at the first failure and prints no counts). If a path is missing, step 2 fails. If "frozen originals" differs from the id count, or "frozen but missing" isn't `0`, send the team the output of the same `comm` with `| head -20` instead of `| wc -l` (the names are file hashes, safe to share); those photos are covered by the nightly backup for a week, so re-run the check after the next backup or exclude them from `ids.txt`. The shell logic was tested with stubbed `ssh` (clean pass, detects a missing original, aborts on a failed `sudo`/`ssh`), but it hasn't run against the real hosts (it assumes `aurora` is an ssh alias you can reach, a snapshot layout of `<snapshot>/originals/<sha>.<ext>`, and that listing ~78k names over ssh is fine); if it doesn't work, tell the team rather than comparing by hand.

   After his go-ahead on the count: `delete "$W/ids.txt" --expected-count <wc -l>`.
5. **Trial import.** With the key in place, `import <dir> --limit 200`, watching `docker stats` and `free -h` on avalon. Tune `--concurrency` (start at 2, never above 4) and check the backup run that night still finishes in its window.
6. **Full import.** `import <dir>`. If it is interrupted, or reports failures, run the exact same command again: the ledger skips everything already confirmed. Throttle to what the nightly backup copy can absorb (about 11MB/s over the home WAN leg); a full-speed import makes every night a large incremental.
7. **Pre-naming.** Once `import` finishes with no failures, run `prename`. It waits for each photo to finish processing (the API answers 409 until then), runs Tier 1 across the whole batch, and only then Tier 2. It is also resumable. Don't run it on a partial import: Tier 2 depends on Tier 1 having assigned confirmed faces across the whole batch.
8. **Post-import checks.**
   - photo count roughly equals the `after_cutoff`-excluded total from `dry-run`, plus the photos taken on/after the cutoff already in lucos_photos;
   - spot-check a few photos in `.photo-metadata`: taken date and description match the sidecars;
   - a few named people have photos and sensible face assignments; remaining duplicates (spelling variants) are merged with `POST /people/merge`;
   - `/_info` is healthy and the worker queue has drained.
9. **Revoke the import key** (owner: the operator, within 48 hours of step 8 passing). Delete `import.env` from avalon, ask lucas42 to run `ssh -p 2202 creds.l42.eu "rm lucos_photos_import/production => lucos_photos/production"`, then redeploy lucos_photos and confirm the key name is gone from the api's `CLIENT_KEYS` (names only, never values). Record the date on #577. Finally, once everything is verified complete, remove the whole `/srv/photos-migration` directory (export, ledger, id file): agents empty it, then lucas42 removes the root-owned directory itself (`ssh avalon.s.l42.eu 'sudo rmdir /srv/photos-migration'`).

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
