"""One-off repair: unconfirm suspicious migrated Google tags so they can be reviewed (lucas42/lucos_photos#664).

Run inside the worker container:

    python -m app.repair_prename_confirmations --cohort-date YYYY-MM-DD           # dry run
    python -m app.repair_prename_confirmations --cohort-date YYYY-MM-DD --apply
    python -m app.repair_prename_confirmations --revert

Prenaming tier 1 confirmed the only detected face on single-name photos without checking it looked
like the named person. Selected faces get person_confirmed = false; person_id is never changed. The
selection is computed from one snapshot, so processing order cannot change the result. Each demotion
is recorded in prename_demotion, which is what --revert reads: it re-confirms faces still on the
recorded person, deletes those rows, and leaves moved or detached faces (and their rows) alone.

The cohort date is supplied at run time and is deliberately never printed or logged.
"""

import argparse
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import numpy as np
from loganne import updateLoganne
from sqlalchemy import select, update
from sqlalchemy.orm import aliased

from lucos_photos_common.database import SessionLocal
from lucos_photos_common.models import Face, FaceEmbedding, MediaItem, PrenameDemotion

logger = logging.getLogger(__name__)

IMPORT_START = datetime(2026, 10, 12, tzinfo=timezone.utc)
MIN_DET_SCORE = 0.7
MIN_OTHER_FACES = 3
MAX_DISTANCE = 0.6
CHUNK = 500


@dataclass
class Selection:
    tier1_shaped: int = 0
    by_rule: Counter = field(default_factory=Counter)  # faces meeting each rule, not exclusive
    cohort_total: int = 0
    selected: dict = field(default_factory=dict)  # face_id -> (person_id, reason of first matching rule)

    @property
    def persons(self) -> Counter:
        return Counter(person_id for person_id, _ in self.selected.values())


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be YYYY-MM-DD")


def _unit(vector) -> np.ndarray:
    v = np.asarray(vector, dtype=np.float64)
    norm = np.linalg.norm(v)
    return v / norm if norm > 0 else v


def _chunks(items, size=CHUNK):
    items = list(items)
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _utc_date(moment):
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.date()
    return moment.astimezone(timezone.utc).date()


def select_faces(db, cohort_date: date) -> Selection:
    other = aliased(Face)
    candidates = db.execute(
        select(Face.id, Face.person_id, Face.det_score, MediaItem.taken_at, FaceEmbedding.embedding)
        .join(MediaItem, MediaItem.id == Face.photo_id)
        .join(FaceEmbedding, FaceEmbedding.face_id == Face.id)
        .where(
            Face.person_confirmed.is_(True),
            Face.person_id.isnot(None),
            MediaItem.uploaded_at >= IMPORT_START,
            ~select(other.id).where(other.photo_id == Face.photo_id, other.id != Face.id).exists(),
        )
    ).all()

    result = Selection(tier1_shaped=len(candidates))
    if not candidates:
        return result

    # Per person: face count and sum of unit embeddings over every face with an embedding, so the
    # "others" for a candidate are the totals minus the candidate itself.
    person_ids = {c.person_id for c in candidates}
    totals: dict = {}
    counts: Counter = Counter()
    for chunk in _chunks(person_ids):
        stream = db.execute(
            select(Face.person_id, FaceEmbedding.embedding)
            .join(FaceEmbedding, FaceEmbedding.face_id == Face.id)
            .where(Face.person_id.in_(chunk))
            .execution_options(yield_per=2000)
        )
        for person_id, embedding in stream:
            u = _unit(embedding)
            totals[person_id] = totals[person_id] + u if person_id in totals else u
            counts[person_id] += 1

    for face_id, person_id, det_score, taken_at, embedding in candidates:
        reasons = []
        if det_score is None or det_score < MIN_DET_SCORE:
            reasons.append("low_confidence")
        others = counts[person_id] - 1
        if others < MIN_OTHER_FACES:
            reasons.append("too_few_faces")
        else:
            u = _unit(embedding)
            mean_direction = _unit(totals[person_id] - u)
            if np.linalg.norm(mean_direction) > 0 and 1.0 - float(np.dot(u, mean_direction)) > MAX_DISTANCE:
                reasons.append("distance")
        in_cohort = _utc_date(taken_at) == cohort_date
        if in_cohort:
            result.cohort_total += 1
        for reason in reasons:
            result.by_rule[reason] += 1
        if in_cohort:
            result.by_rule["cohort"] += 1
            reasons.append("cohort")
        if reasons:
            result.selected[face_id] = (person_id, reasons[0])
    return result


def apply_selection(db, selection: Selection) -> int:
    """Unconfirm the selected faces and record why, in the caller's transaction. Returns the number changed."""
    changed = 0
    for chunk in _chunks(selection.selected):
        rows = db.execute(
            update(Face)
            .where(Face.id.in_(chunk), Face.person_confirmed.is_(True))
            .values(person_confirmed=False)
            .returning(Face.id, Face.person_id)
        ).all()
        for face_id, person_id in rows:
            db.add(PrenameDemotion(face_id=face_id, person_id=person_id, reason=selection.selected[face_id][1]))
        changed += len(rows)
    db.flush()
    return changed


def revert(db) -> tuple[int, int]:
    """Re-confirm faces still on the recorded person and drop their rows. Returns (reverted, skipped)."""
    reverted = skipped = 0
    for demotion in db.query(PrenameDemotion).all():
        face = db.get(Face, demotion.face_id)
        if face is not None and face.person_id == demotion.person_id:
            face.person_confirmed = True
            db.delete(demotion)
            reverted += 1
        else:
            skipped += 1
    db.flush()
    return reverted, skipped


def print_report(selection: Selection) -> None:
    print(f"tier-1-shaped faces: {selection.tier1_shaped}")
    print(f"low confidence (rule 1): {selection.by_rule['low_confidence']}")
    print(f"too few other faces (rule 2): {selection.by_rule['too_few_faces']}")
    print(f"far from the person's other faces (rule 3): {selection.by_rule['distance']}")
    print(f"in the cohort: {selection.cohort_total}")
    print(f"selected in total: {len(selection.selected)}")
    persons = selection.persons
    print(f"persons affected: {len(persons)}")
    print("top 20 persons by faces to demote (id: count):")
    for person_id, count in persons.most_common(20):
        print(f"  {person_id}: {count}")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cohort-date", type=_parse_date, help="UTC date YYYY-MM-DD; every tier-1-shaped face taken that day is demoted")
    parser.add_argument("--apply", action="store_true", help="write the change (default is a dry run)")
    parser.add_argument("--revert", action="store_true", help="undo recorded demotions")
    args = parser.parse_args(argv)
    if args.revert and (args.apply or args.cohort_date):
        parser.error("--revert takes no other options")
    if not args.revert and args.cohort_date is None:
        parser.error("--cohort-date is required")
    logging.basicConfig(level=logging.INFO)

    db = SessionLocal()
    try:
        if args.revert:
            reverted, skipped = revert(db)
            db.commit()
            logger.info("repair_prename_confirmations: reverted %d, skipped %d", reverted, skipped)
            print(f"reverted: {reverted}")
            print(f"skipped (moved or detached since): {skipped}")
            return

        selection = select_faces(db, args.cohort_date)
        print_report(selection)
        if not args.apply:
            print("dry run: nothing written (use --apply)")
            return

        changed = apply_selection(db, selection)
        db.commit()
        logger.info("repair_prename_confirmations: unconfirmed %d face(s)", changed)
        print(f"unconfirmed: {changed}")
        if not changed:
            return
        updateLoganne(
            type="facesUnconfirmed",
            humanReadable=f"Marked {changed} migrated Google tags as unconfirmed for review in lucos_photos",
            level="routine",
            url=None,
        )
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
