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
IO=(--device-read-bps /dev/sda:10mb --device-write-bps /dev/sda:10mb --device-read-iops /dev/sda:100 --device-write-iops /dev/sda:100)
run() { docker run --rm "${IO[@]}" --user "$(id -u):$(id -g)" -e PHOTOS_API_KEY_FILE=/work/import.env -v "$W":/work -v "$PWD/migration":/app:ro -w /work python:3.14-slim python /app/google_takeout_import.py "$@"; }
```

**Disk I/O caps (`IO`).** avalon has one spinning disk, which every service shares. An uncapped `tar` of the export saturated it on 2026-10-11: IO pressure was around 95%, and `/_info` timed out on several services. `ionice` does nothing here, because the disk's scheduler is `mq-deadline`. Docker's `--device-*` limits do work: they set cgroup v2 `io.max`, which throttles buffered writes as well as reads on ext4. On avalon, a capped direct read measured 10.2MB/s against 52MB/s uncapped. Every container in this runbook that reads or writes the export uses the same caps. For `import`, capping its reads also caps the upload rate, and so the API's writes into the uploads volume, because the API only writes what it receives.

**Don't start a heavy step while a lucos_photos deploy is running.** Every merge to lucos_photos (runbook changes included) redeploys it, and pulling the multi-GB image saturates the disk by itself: IO pressure went from about 1% to 53% during one on 2026-10-11.

**IO pressure is the gauge** for every heavy step: `grep some /proc/pressure/io`. A quiet avalon reads `avg60` in single digits. If `some avg60` is above 25% on two readings a minute apart, with no deploy underway, stop the step (`docker stop <name>`) and halve the caps (`5mb` and `50`) before restarting it.

`--user` makes the container write the ledger and skipped logs as the operator, not root (otherwise the final clean-up needs `sudo` for them). The script reads `KEY_LUCOS_PHOTOS` from `import.env` itself (quotes stripped), so the key never appears in the environment, on a command line or in `docker inspect`.

**How to run each subcommand.** `dry-run` and `delete` are short: run them in the foreground with `run …`, bounded by `timeout`. Never background anything (`&`, `nohup`, `setsid`) inside an `ssh` one-liner: the child is orphaned when the session closes and keeps running with nobody watching it. If a short run is cut off, run it again: `dry-run` uploads nothing, and `delete` resumes from its ledger. `import` and `prename` take days, so they run as named, detached containers that don't depend on anyone's session (see "Long runs" below). Subcommands:

- `dry-run <dir>` parses the export and uploads nothing. It reports how many files are before the cutoff, have no sidecar, or have an unexpected `people` shape, and prints a few sample `people` values. Skipped files and reasons go to `migration-skipped.tsv`.
- `import <dir> [--concurrency 2] [--max-pending 200] [--limit N]` uploads via `POST /photos`. It pauses while `/_info` reports more than `--max-pending` pending items, and aborts (exit 2) if the queue depth can't be read for about five minutes, rather than carrying on unthrottled.
- `prename` runs Tier 1 then Tier 2 face pre-naming over everything in the ledger.
- `delete <ids-file> --expected-count N` deletes from a frozen list of ids.

The ledger (`migration-ledger.sqlite`) records a file only after a confirmed 2xx. Files the server permanently rejects (4xx, e.g. an invalid image) are recorded too and listed in `migration-skipped.tsv`, so a resume doesn't resend them.

## Long runs: `import` and `prename`

The full import takes about two weeks, which is longer than any agent session. Run it as a named, detached container, so it isn't a child of anyone's `ssh` session and carries on when the session that started it ends:

```bash
run_detached() {  # run_detached import <dir> [options]   |   run_detached prename
  docker run -d --name "lucos_photos_migration_$1" --restart no \
    --log-opt max-size=20m --log-opt max-file=5 \
    "${IO[@]}" --user "$(id -u):$(id -g)" -e PHOTOS_API_KEY_FILE=/work/import.env \
    -v "$W":/work -v "$PWD/migration":/app:ro -w /work \
    python:3.14-slim python /app/google_takeout_import.py "$@"
}
```

- **This is not the backgrounding the rule above forbids.** That rule is about anonymous children of an `ssh` session that nobody can see or stop. This container has a fixed name, so it shows in `docker ps -a`, stops with `docker stop`, and logs to `docker logs`. The fixed name also guards the ledger: `docker run` refuses a second container with the same name, so two imports can't run against one ledger by accident. For the same reason, never run `import` in the foreground while the container exists.
- **No `--rm`.** A stopped container keeps its exit code and logs until someone has read them (`docker inspect -f '{{.State.ExitCode}}' lucos_photos_migration_import`, `docker logs -t --tail 50 …`). Remove it with `docker rm` before the next run.
- **`--restart no`, on purpose.** The script already retries connection errors, 5xx (including 507) and 429 with backoff, so any exit means something a person should read first. For `import`: 0 is done; 1 is usually "some files failed, rerun" (the last log lines say so); 2 means it stopped on purpose for a human to look (the key was rejected, or the queue depth couldn't be read for five minutes). For `prename`: 0 is done, and **any** non-zero exit means it stopped on an unexpected HTTP status or error (the last log line says which), so it's a look-first, never a plain resume. Docker can't tell those cases apart, and an automatic restart would override the deliberate stops. A reboot of avalon also stops the run, and it won't come back by itself; the check below catches that. (`live-restore` is on, so a dockerd restart alone doesn't stop it.)
- **To pause** (e.g. after a failed backup): `docker stop lucos_photos_migration_import`. Stopping mid-upload is safe, because a file is only recorded in the ledger after a confirmed 2xx.
- **To resume**, once you know why it stopped: `docker rm lucos_photos_migration_import && run_detached import <dir>`. The ledger skips everything already confirmed. Run it from the same checkout, so `$PWD/migration` resolves.

### Checking on the run

Run this whenever a session is working on the migration, until `prename` finishes. **Nothing alerts on the import itself:** lucos_monitoring doesn't watch the container, so a stopped run is only found by this check. A stopped run is safe and just costs time; the ledger means nothing is lost. The backups check in lucos_monitoring goes red after 72 hours without a successful backup, so a backup failure is eventually alerted, but this check finds it sooner.

1. **The run is alive:**

   ```bash
   docker ps -a --filter name=lucos_photos_migration_ --format '{{.Names}}  {{.Status}}'
   docker logs -t --tail 3 lucos_photos_migration_import 2>&1
   ```

   It's fine if the status is `Up` and the newest line is recent. `-t` adds the date, because the script's own timestamps are time-only. At about 200 photos an hour there's a `progress:` line roughly every 30 minutes; `queue depth … pausing 30s` lines mean it's waiting for the worker, which is normal. **Look now** if it has `Exited`, if no line is newer than an hour, or if there's a `FAILED` or `ABORTED` line.
2. **Last night's backup completed:** loganne has a `backups` event from `lucos_backups` ("… archives successfully backed up") within the past day. Don't treat a green monitoring dashboard as proof, because of the 72-hour window above. If there's no event, pause the import (step 6) until the backup has caught up.

## Handing the key to avalon

Production credentials can't be read by agents and the value must never go through chat, GitHub or a log. lucas42 puts it straight onto avalon with one sudo account, in two commands run from his laptop (the `ssh` host is avalon):

```bash
# 1. Once: the working directory, owned by the operator, not readable by anyone else (this is also where the Takeout export goes)
ssh avalon.s.l42.eu 'sudo install -d -o lucos-agent -g docker -m 700 /srv/photos-migration'

# 2. Before the deletion (step 4; `delete` authenticates with this key too, so it is also needed before the trial import): the key, copied in one pipe so it never lands on his laptop's disk or in a shell variable
scp -P 2202 creds.l42.eu:lucos_photos_import/production/.env /dev/stdout \
  | ssh avalon.s.l42.eu 'sudo install -o lucos-agent -g docker -m 600 /dev/stdin /srv/photos-migration/import.env'
```

Rules for everyone afterwards: don't `cat` the file, run `env`, enable `set -x`, or paste `docker inspect` output or request logs into chat or GitHub. The script never logs the key or request headers. Before the first run, the system administrator confirms `/srv/photos-migration` isn't in any backup scope (it holds a credential and about 185GB of export).

The key can upload **and** delete photos (it's an unscoped `CLIENT_KEYS` entry). `import` and `prename` contain no delete call; the only delete path is the explicit `delete` subcommand, which needs a frozen id file and a matching `--expected-count`.

**Lifetime.** The key lives until the migration is complete, then lucas42 revokes it (step 9); only he can change production credentials. Revocation is the real cleanup. Deleting the file is secondary (`shred` means little on a journaling filesystem), but still delete `import.env` right after the last run finishes.

## Steps

1. **First look at the export.** As soon as any Takeout export exists (even a small one), run `dry-run` on it and check the sample output: the `people` values must be plain names, `no_sidecar` should be near zero, and `bad_people_shape` must be zero. If not, stop and tell the developer — the sidecar parsing was written from documentation, not a real export (acceptance criterion on #469).

   The export is four `.tgz` files, about 185GB, not the ~78GB first estimated. Extract each archive into `/srv/photos-migration/takeout/`, never `/tmp` (RAM-backed), one at a time, in a capped container (not a bare `tar`; see "Disk I/O caps" above):

   ```bash
   docker run -d --name lucos_photos_migration_extract --restart no "${IO[@]}" --memory 512m \
     --user "$(id -u):$(id -g)" -v "$W":/work -w /work python:3.14-slim \
     tar -xzf raw-takeout/partN.tgz -C takeout
   cat /sys/fs/cgroup/system.slice/docker-$(docker inspect -f '{{.Id}}' lucos_photos_migration_extract).scope/io.max   # expect 8:0 rbps=10485760 wbps=10485760 riops=100 wiops=100
   ```

   `--memory 512m` bounds the dirty page cache, so the writer is throttled before it can queue gigabytes for a single flush. A 36GB archive takes about an hour. Watch it with IO pressure. When it exits, check `docker inspect -f '{{.State.ExitCode}}' lucos_photos_migration_extract` is 0, then `docker rm` it before the next archive. To redo an archive, rerun it: GNU `tar` overwrites partial files. Run `dry-run` only once extraction has finished, never at the same time. Photos and video don't compress, so extraction roughly doubles it; with the archives kept the peak is about 370GB, and avalon has about 1.6TB free (sysadmin, 2026-10-11), so deleting each archive as you go is optional. Point the script at the extracted `Takeout/Google Photos` directory. `dry-run` also prints `videos` and `bytes_to_import`, which settle the photo/video split and how big the pre-cutoff import actually is.
2. **Confirm the backups exist** (first half of the gate before the deletion; no trial restore is needed). Everything the deletion removes was taken before 2026-02-01 and originals are immutable (stored by SHA-256), so any pre-deletion backup that contains those files is as good as the day before's, and the date of the deletion doesn't matter. Use the 2026-10-06 backups: they sit on the long retention track (kept until about 2027-10-06, the 6th of each month for a year, for both volumes). Confirm both are present on aurora:
   - the photos snapshot directory `/share/backups/host/avalon/volume-snapshots/lucos_photos_photos/2026-10-06/` (a top-level listing of the backup directory can wrongly show nothing: the lucas42/lucos#294 lesson);
   - the postgres tarball `lucos_photos_postgres_data.2026-10-06.tar.gz` under `host/avalon/volume/` on aurora. Postgres uses the default daily tarball strategy, *not* `volume-snapshots/`. The copy under `/srv/backups/local/volume/` on avalon does **not** count for this gate: it sits on avalon's single disk, so it would be lost in exactly the host failure a long-lived restore point exists to survive (avalon's disk already died once, lucas42/lucos#294).

   **Nobody on the team can see aurora, so lucas42 checks both paths exist** (one command, in step 4), or arranges read access for the operator.
3. *(No trial restore — see above.)*
4. **Deletion.** The date doesn't matter; run it once the checks below pass. Freeze the ids, then run the coverage query, and show lucas42 both counts:

   ```bash
   docker exec lucos_photos_postgres psql -U photos -d photos -Atc \
     "SELECT id FROM media_item WHERE taken_at < '2026-02-01T00:00:00Z' ORDER BY id" > "$W/ids.txt"
   wc -l "$W/ids.txt"
   docker exec lucos_photos_postgres psql -U photos -d photos -Atc \
     "SELECT count(*) FROM media_item WHERE taken_at < '2026-02-01T00:00:00Z' AND uploaded_at >= '2026-10-06T00:00:00Z'"   # coverage: must be 0
   docker exec lucos_photos_postgres psql -U photos -d photos -Atc \
     "SELECT count(*) FROM media_item WHERE taken_at IS NULL"
   ```

   Rows with a null `taken_at` are *not* in the list; decide about them explicitly before deleting.

   **Why the coverage query is enough.** The only way a photo on the list could be missing from the 10-06 snapshot is if it was added to lucos_photos after the snapshot was taken, e.g. by a phone resync of old photos. `uploaded_at` is set by the database (`now()`) when the row is inserted, and no code writes it afterwards. Midnight is earlier than either daily backup run, so a 0 means the 10-06 snapshot holds every photo being deleted. If it isn't 0, those photos are covered by the nightly backups for a week: wait for the next one, or leave them out of `ids.txt`.

   This relies on the snapshot being complete. lucos_backups writes each snapshot into a `<date>.partial` directory and renames it to `<date>` only after `rsync` exits 0 (a non-zero exit raises before the rename). The 2026-09-16 rebuild restored the photos volume from this mechanism. A per-file comparison against aurora would also catch a snapshot damaged after it was written, which is a small residual risk, and isn't required.

   **Backup existence check (lucas42, one command).** From a machine that can reach aurora, both paths must be listed without an error:

   ```bash
   ssh aurora 'ls -d /share/backups/host/avalon/volume-snapshots/lucos_photos_photos/2026-10-06 /share/backups/host/avalon/volume/lucos_photos_postgres_data.2026-10-06.tar.gz'
   ```

   After his go-ahead on the count: `delete "$W/ids.txt" --expected-count <wc -l>`.
5. **Trial import.** Before it, **lucas42 checks aurora's free space** (agents can't see it; the last figure, 954GB, is from June). The pre-cutoff originals stay linked from older snapshots for up to about 52 weeks, so deleting them frees nothing there, and the photos volume will grow to roughly 200GB+ (more if the export is video-heavy). Then `run_detached import <dir> --limit 200` (and `docker rm lucos_photos_migration_import` once it exits 0), watching IO pressure (see "Disk I/O caps"), `docker stats`, `free -h` and `df -h /` on avalon. **This is the contention measurement:** the first ~200 uploads fill the queue as fast as the caps allow, which is the import's worst burst, and then the worker sets the pace (about 200 photos an hour). If pressure crosses the stop rule while the worker is merely processing, with uploads paused for queue depth, the worker itself is the problem. Uploading less won't help in that case: stop and tell the team. Note the worker's steady-state pressure, the `lucos_photos_api` and `lucos_photos_worker` CPU in `docker stats`, and whether other services' `/_info` stayed green, on #577 before the full import (the API's `MIN_FREE_DISK_SPACE` guard is effectively off at this scale, so watch disk by hand). **Confirm that night's backup completes and publishes** before going on.
6. **Full import.** `run_detached import <dir>`, then use the check under "Long runs" whenever a session is working on it, until it exits. If it is interrupted, or exits 1 (some files failed), resume it as described under "Long runs": the ledger skips everything already confirmed. Exit 2 means it stopped for a human: read its log before resuming.
   - **Don't raise `--max-pending` (200) or `--concurrency` (2, never above 4)** without redoing this arithmetic: the nightly backup delta is set by the worker's throughput (about 200 photos an hour, roughly 14GB a day), not by upload speed, and one 2-hour rsync attempt moves roughly 79GB at the ~11MB/s ceiling. Big videos are the way an item's size could break that, which is what `dry-run`'s video count is for.
   - **Confirm the nightly backup completed after the first full day at steady rate**, then keep checking it with the check under "Long runs". If a night's backup fails, pause the import (`docker stop lucos_photos_migration_import`; it resumes from the ledger) and run `seed-volume` by hand before continuing.
   - If `import` aborts because it can't read the queue depth, `/_info` is unhealthy: stop and look before resuming.
7. **Pre-naming.** Once `import` exits 0, `docker rm lucos_photos_migration_import && run_detached prename`, and keep using the check (with `lucos_photos_migration_prename` in the `docker logs` line). It waits for each photo to finish processing (the API answers 409 until then), runs Tier 1 across the whole batch, and only then Tier 2. It is also resumable, but any non-zero exit means it stopped on an unexpected response: read its last log line before resuming (`docker rm lucos_photos_migration_prename && run_detached prename`). Don't run it on a partial import: Tier 2 depends on Tier 1 having assigned confirmed faces across the whole batch.
8. **Post-import checks.**
   - photo count roughly equals the `after_cutoff`-excluded total from `dry-run`, plus the photos taken on/after the cutoff already in lucos_photos;
   - spot-check a few photos in `.photo-metadata`: taken date and description match the sidecars;
   - a few named people have photos and sensible face assignments; remaining duplicates (spelling variants) are merged with `POST /people/merge`;
   - `/_info` is healthy and the worker queue has drained.
9. **Revoke the import key** once step 8 passes. The operator deletes `import.env` from avalon and asks lucas42 to run `ssh -p 2202 creds.l42.eu "rm lucos_photos_import/production => lucos_photos/production"`, then redeploy lucos_photos and confirm the key name is gone from the api's `CLIENT_KEYS` (names only, never values). Record the date on #577. Finally, once everything is verified complete, remove the whole `/srv/photos-migration` directory (export, ledger, id file): agents empty it, then lucas42 removes the root-owned directory itself (`ssh avalon.s.l42.eu 'sudo rmdir /srv/photos-migration'`).

## Resume and failure handling

- Upload interrupted (container exited, or avalon rebooted) → read the exit code and log, then resume as under "Long runs".
- `507` from the API means disk is low: the script backs off and retries on its own; check free space on avalon.
- A `401`/`403`/`404` from the API aborts `import` and `prename` (nothing is marked as done), so a wrong key or `--base-url` costs nothing: fix it and rerun. `delete` stops on the first unexpected status and resumes from its ledger; ids already gone (404) are counted, not errors.
- Descriptions are sent as a form field (the `X-Description` header can't carry newlines or non-ASCII text).

## Judgement calls worth knowing

- Photos and their `-edited` copies are both imported (different bytes, same sidecar), so edited photos appear twice.
- Files with no sidecar, or no `photoTakenTime`, are skipped and listed rather than imported blind, because they can't be placed on the correct side of the cutoff.
- Tier 2 assignments are *unconfirmed* (an ML-grade guess, like auto-assignment); only Tier 1 sets `person_confirmed`. Tier 2 uses `FACE_SIMILARITY_THRESHOLD`.
- Deleting a photo now also deletes any person it leaves with no faces (unless linked to a contact); names from Google recreate or re-match people by exact case-insensitive `display_name`.
