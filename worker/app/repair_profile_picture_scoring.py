"""One-off re-pick of auto-generated profile pictures after the blur/typicality scoring ships (lucas42/lucos_photos#681).

Run inside the worker container, after the new scoring is deployed:

    python -m app.repair_profile_picture_scoring            # dry run: count only
    python -m app.repair_profile_picture_scoring --apply    # enqueue re-selection
    python -m app.repair_profile_picture_scoring --apply --limit 20

Every person with an auto-generated picture is enqueued; pinned pictures (profile_auto_generated
is False) are left alone. generate_profile_picture only rewrites a picture when the new pick
differs from the current one, and emit_event=False avoids one Loganne event per person.
"""

import argparse
import logging
import os

from redis import Redis
from rq import Queue
from rq.job import Retry

from lucos_photos_common import jobs
from lucos_photos_common.database import SessionLocal
from lucos_photos_common.models import Person


def find_auto_generated(db) -> list[str]:
    rows = db.query(Person.id).filter(Person.profile_auto_generated.is_(True)).all()
    return [str(person_id) for (person_id,) in rows]


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="enqueue re-selection (default is a dry run)")
    parser.add_argument("--limit", type=int, help="enqueue at most this many persons")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)

    db = SessionLocal()
    try:
        person_ids = find_auto_generated(db)
    finally:
        db.close()

    print(f"auto-generated profile pictures: {len(person_ids)}")
    if not args.apply:
        print("dry run: nothing enqueued (use --apply)")
        return

    queue = Queue("photos", connection=Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379")))
    retry = Retry(max=3, interval=[10, 30, 60])
    todo = person_ids[: args.limit] if args.limit else person_ids
    for person_id in todo:
        queue.enqueue(jobs.generate_profile_picture, person_id, emit_event=False, retry=retry)
    print(f"enqueued: {len(todo)}")


if __name__ == "__main__":
    main()
