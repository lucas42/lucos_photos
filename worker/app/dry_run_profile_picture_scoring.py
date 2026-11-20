"""Read-only dry run of the blur/typicality profile-picture scoring (lucas42/lucos_photos#681).

    python -m app.dry_run_profile_picture_scoring --sample 40 --seed 1
    python -m app.dry_run_profile_picture_scoring --person-id <uuid> [--person-id ...]

Prints, per person, the old pick (existing 0-4 score, recency) next to the new pick, with the
numbers behind each. Persons are numbered, never named. Nothing is written: the session is a
READ ONLY transaction and no job is enqueued.
"""

import argparse
import random

from sqlalchemy import func, text

from lucos_photos_common import jobs
from lucos_photos_common.database import SessionLocal
from lucos_photos_common.models import Face, MediaItem, Person


def load_faces(db, person_id):
    return (
        db.query(Face)
        .filter(Face.person_id == person_id)
        .join(Face.media_item)
        .filter(MediaItem.width.isnot(None), MediaItem.height.isnot(None))
        .all()
    )


def describe(entry) -> str:
    blur = "n/a" if entry["blur"] is None else f"{entry['blur']:.0f}"
    distance = "n/a" if entry["distance"] is None else f"{entry['distance']:.3f}"
    return f"score={entry['base']} blur={blur} dist={distance} bonus={entry['bonus']:.2f}"


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sample", type=int, default=40, help="random auto-generated persons to compare")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--min-faces", type=int, default=1, help="only sample persons with at least this many faces")
    parser.add_argument("--person-id", action="append", default=[], help="compare these persons as well")
    args = parser.parse_args(argv)

    db = SessionLocal()
    try:
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SET TRANSACTION READ ONLY"))
        face_count = func.count(Face.id)
        auto_ids = [
            row[0]
            for row in db.query(Person.id)
            .join(Face, Face.person_id == Person.id)
            .filter(Person.profile_auto_generated.is_(True))
            .group_by(Person.id)
            .having(face_count >= args.min_faces)
            .all()
        ]
        chosen = random.Random(args.seed).sample(auto_ids, min(args.sample, len(auto_ids)))
        person_ids = [pid for pid in args.person_id] + chosen
        changed = 0
        for number, person_id in enumerate(person_ids, start=1):
            faces = load_faces(db, person_id)
            if not faces:
                continue
            ranked = jobs._rank_profile_faces(db, faces)
            new = ranked[0]
            tier = [e for e in ranked if e["base"] == new["base"]]
            old = max(tier, key=lambda e: -1 if e["photo"].taken_at is None else e["photo"].taken_at.timestamp())
            differs = old["face"].id != new["face"].id
            changed += differs
            print(f"#{number} faces={len(faces)} tier={len(tier)} changed={differs}")
            print(f"   old: {describe(old)}")
            print(f"   new: {describe(new)}")
        print(f"changed: {changed} of {len(person_ids)}")
    finally:
        db.rollback()
        db.close()


if __name__ == "__main__":
    main()
