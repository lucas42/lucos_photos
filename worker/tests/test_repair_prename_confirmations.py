from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from app import repair_prename_confirmations as repair
from lucos_photos_common.models import Face, FaceEmbedding, MediaItem, Person, PrenameDemotion

COHORT = date(2031, 3, 4)  # placeholder; the real date is never in the repository
AFTER_IMPORT = datetime(2026, 10, 20, tzinfo=timezone.utc)
BEFORE_IMPORT = datetime(2026, 9, 1, tzinfo=timezone.utc)
OTHER_DAY = datetime(2031, 3, 5, 12, tzinfo=timezone.utc)
COHORT_DAY = datetime(2031, 3, 4, 12, tzinfo=timezone.utc)


def axis(i, j=None):
    v = [0.0] * 512
    v[i] = 1.0
    if j is not None:
        v[j] = 0.05
    return v


_counter = [0]


def photo(db, uploaded_at=AFTER_IMPORT, taken_at=OTHER_DAY):
    _counter[0] += 1
    p = MediaItem(sha256_hash=f"{_counter[0]:064d}", file_extension="jpg", uploaded_at=uploaded_at, taken_at=taken_at)
    db.add(p)
    db.flush()
    return p


def face(db, on_photo, person, embedding, confirmed=False, det_score=0.9):
    f = Face(
        photo_id=on_photo.id, person_id=person.id if person else None, person_confirmed=confirmed, det_score=det_score,
        bbox_x=0, bbox_y=0, bbox_width=0.1, bbox_height=0.1, embedding_row=FaceEmbedding(embedding=embedding),
    )
    db.add(f)
    db.flush()
    return f


def person_with_others(db, others=4, axis_index=0):
    """A person with `others` unconfirmed faces, all pointing along one axis (so the 'average' is that axis)."""
    p = Person(display_name="Alice")
    db.add(p)
    db.flush()
    for _ in range(others):
        face(db, photo(db, uploaded_at=BEFORE_IMPORT), p, axis(axis_index, 7))
    return p


def tier1(db, person, embedding=None, **kwargs):
    on_photo = kwargs.pop("on_photo", None) or photo(db, taken_at=kwargs.pop("taken_at", OTHER_DAY))
    return face(db, on_photo, person, embedding or axis(0, 7), confirmed=True, **kwargs)


def select(db):
    db.commit()
    return repair.select_faces(db, COHORT)


def test_low_confidence_or_null_score_is_selected(db_session):
    p = person_with_others(db_session)
    low, none, ok = tier1(db_session, p, det_score=0.5), tier1(db_session, p, det_score=None), tier1(db_session, p)

    sel = select(db_session)

    assert sel.selected[low.id] == (p.id, "low_confidence")
    assert sel.selected[none.id][1] == "low_confidence"
    assert ok.id not in sel.selected


def test_too_few_other_faces_is_selected(db_session):
    p = person_with_others(db_session, others=2)
    f = tier1(db_session, p)

    assert select(db_session).selected[f.id] == (p.id, "too_few_faces")


def test_far_from_the_persons_other_faces_is_selected(db_session):
    p = person_with_others(db_session, others=5, axis_index=1)
    f = tier1(db_session, p, embedding=axis(0))

    assert select(db_session).selected[f.id] == (p.id, "distance")


def test_clean_face_is_not_selected_unless_on_the_cohort_date(db_session):
    p = person_with_others(db_session)
    clean = tier1(db_session, p)
    cohort = tier1(db_session, p, taken_at=COHORT_DAY)

    sel = select(db_session)

    assert clean.id not in sel.selected
    assert sel.selected[cohort.id] == (p.id, "cohort")
    assert sel.cohort_total == 1


def test_confirmed_face_on_a_photo_with_two_faces_is_never_selected(db_session):
    p = person_with_others(db_session, others=0)
    shared = photo(db_session)
    only_confirmed = tier1(db_session, p, on_photo=shared, det_score=0.1)
    face(db_session, shared, None, axis(3), confirmed=False)  # even a not-a-face row counts

    sel = select(db_session)

    assert only_confirmed.id not in sel.selected
    assert sel.tier1_shaped == 0


def test_photo_uploaded_before_the_import_is_never_selected(db_session):
    p = person_with_others(db_session, others=0)
    f = tier1(db_session, p, on_photo=photo(db_session, uploaded_at=BEFORE_IMPORT), det_score=0.1)

    assert f.id not in select(db_session).selected


def test_unconfirmed_faces_are_not_tier1_shaped(db_session):
    p = person_with_others(db_session, others=0)
    face(db_session, photo(db_session), p, axis(0), confirmed=False, det_score=0.1)

    assert select(db_session).tier1_shaped == 0


def test_apply_unconfirms_without_moving_records_reasons_and_is_idempotent(db_session):
    p = person_with_others(db_session, others=1)
    f = tier1(db_session, p)
    sel = select(db_session)

    assert repair.apply_selection(db_session, sel) == 1
    db_session.commit()
    db_session.expire_all()

    assert db_session.get(Face, f.id).person_confirmed is False
    assert db_session.get(Face, f.id).person_id == p.id
    row = db_session.get(PrenameDemotion, f.id)
    assert (row.person_id, row.reason) == (p.id, "too_few_faces")
    assert repair.select_faces(db_session, COHORT).selected == {}
    assert repair.apply_selection(db_session, repair.select_faces(db_session, COHORT)) == 0


def test_revert_restores_faces_still_on_their_person_and_skips_moved_ones(db_session):
    p, q = person_with_others(db_session, others=1), person_with_others(db_session, others=1)
    stays, moved, detached = (tier1(db_session, p, det_score=0.1) for _ in range(3))
    repair.apply_selection(db_session, select(db_session))
    db_session.commit()
    moved.person_id = q.id
    detached.person_id = None
    db_session.commit()

    reverted, skipped = repair.revert(db_session)
    db_session.commit()
    db_session.expire_all()

    assert (reverted, skipped) == (1, 2)
    assert db_session.get(Face, stays.id).person_confirmed is True
    assert db_session.get(Face, moved.id).person_confirmed is False
    assert db_session.get(Face, detached.id).person_confirmed is False
    assert {r.face_id for r in db_session.query(PrenameDemotion)} == {moved.id, detached.id}


def test_demotion_rows_cascade_with_the_face(db_session):
    engine = db_session.get_bind()
    event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
    conn = engine.raw_connection()
    conn.execute("PRAGMA foreign_keys=ON")
    conn.close()
    p = person_with_others(db_session, others=1)
    f = tier1(db_session, p)
    repair.apply_selection(db_session, select(db_session))
    db_session.commit()
    db_session.execute(Face.__table__.delete().where(Face.id == f.id))
    db_session.commit()

    assert db_session.query(PrenameDemotion).count() == 0


def run_main(db_session, monkeypatch, argv):
    engine = db_session.get_bind()
    monkeypatch.setattr(repair, "SessionLocal", sessionmaker(bind=engine))
    with patch.object(repair, "updateLoganne") as loganne:
        repair.main(argv)
    return loganne


def test_the_cohort_date_never_appears_in_output_or_logs(db_session, monkeypatch, capsys, caplog):
    p = person_with_others(db_session)
    tier1(db_session, p, taken_at=COHORT_DAY)
    db_session.commit()

    with caplog.at_level("DEBUG"):
        loganne = run_main(db_session, monkeypatch, ["--cohort-date", "2031-03-04"])
        loganne_applied = run_main(db_session, monkeypatch, ["--cohort-date", "2031-03-04", "--apply"])

    shown = capsys.readouterr().out + caplog.text + str(loganne.call_args_list) + str(loganne_applied.call_args_list)
    assert "2031" not in shown and "03-04" not in shown
    loganne.assert_not_called()
    assert run_main(db_session, monkeypatch, ["--cohort-date", "2031-03-04", "--apply"]).call_args is None  # nothing left: no event
    assert loganne_applied.call_args.kwargs["type"] == "facesUnconfirmed"
    assert "Marked 1 migrated Google tags" in loganne_applied.call_args.kwargs["humanReadable"]


def test_dry_run_writes_nothing_and_reports_counts_only(db_session, monkeypatch, capsys):
    p = person_with_others(db_session, others=1)
    tier1(db_session, p)
    db_session.commit()

    run_main(db_session, monkeypatch, ["--cohort-date", "2031-03-04"])
    out = capsys.readouterr().out

    assert "selected in total: 1" in out and "persons affected: 1" in out and str(p.id) in out
    assert "Alice" not in out
    assert db_session.query(PrenameDemotion).count() == 0


def test_cohort_date_is_required_except_for_revert(db_session, monkeypatch):
    with pytest.raises(SystemExit):
        repair.main([])
    with pytest.raises(SystemExit):
        repair.main(["--revert", "--cohort-date", "2031-03-04"])
    with pytest.raises(SystemExit):
        repair.main(["--cohort-date", "not-a-date"])
