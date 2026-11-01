"""One-off repair for profile pictures cropped from EXIF-rotated photos (lucas42/lucos_photos#619).

Run inside the worker container, after the exif_transpose fix is deployed:

    python -m app.repair_exif_profile_pictures            # dry run: count only
    python -m app.repair_exif_profile_pictures --apply    # enqueue regeneration
    python -m app.repair_exif_profile_pictures --apply --limit 20

Affected = a person whose current profile photo has an EXIF Orientation other than 1.
Auto-generated pictures are regenerated with force=True and no Loganne event; manually
pinned ones go through the manual path.
"""

import argparse
import logging
import os
from collections import Counter

from redis import Redis
from rq import Queue
from rq.job import Retry

from lucos_photos_common import jobs
from lucos_photos_common.database import SessionLocal
from lucos_photos_common.models import MediaItem, Person

logger = logging.getLogger(__name__)

EXIF_TAG_ORIENTATION = 0x0112


def read_orientation(media_item) -> int:
    """EXIF Orientation of the original file, or 1 if it is missing/unreadable."""
    from PIL import Image

    path = jobs.ORIGINALS_DIR / f"{media_item.sha256_hash}.{media_item.file_extension}"
    try:
        with Image.open(path) as img:
            return int(img.getexif().get(EXIF_TAG_ORIENTATION, 1))
    except Exception:
        logger.warning("could not read orientation of %s", path)
        return 1


def find_affected(db) -> list[tuple[str, str, bool, int]]:
    """(person_id, photo_id, is_manual, orientation) for each person whose profile photo is oriented."""
    rows = (
        db.query(Person.id, Person.profile_auto_generated, MediaItem)
        .join(MediaItem, MediaItem.id == Person.profile_photo_id)
        .all()
    )
    affected = []
    for person_id, auto_generated, media_item in rows:
        orientation = read_orientation(media_item)
        if orientation != 1:
            affected.append((str(person_id), str(media_item.id), auto_generated is False, orientation))
    return affected


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="enqueue regeneration (default is a dry run)")
    parser.add_argument("--limit", type=int, help="enqueue at most this many persons")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)

    db = SessionLocal()
    try:
        affected = find_affected(db)
    finally:
        db.close()

    print(f"affected persons: {len(affected)}")
    print(f"by orientation: {dict(sorted(Counter(a[3] for a in affected).items()))}")
    print(f"manually pinned: {sum(1 for a in affected if a[2])}")
    if not args.apply:
        print("dry run: nothing enqueued (use --apply)")
        return

    queue = Queue("photos", connection=Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379")))
    retry = Retry(max=3, interval=[10, 30, 60])
    todo = affected[: args.limit] if args.limit else affected
    for person_id, photo_id, is_manual, _ in todo:
        if is_manual:
            queue.enqueue(jobs.generate_manual_profile_picture, person_id, photo_id, retry=retry)
        else:
            queue.enqueue(jobs.generate_profile_picture, person_id, force=True, emit_event=False, retry=retry)
    print(f"enqueued: {len(todo)}")


if __name__ == "__main__":
    main()
