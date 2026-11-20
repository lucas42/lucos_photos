from unittest.mock import MagicMock, patch

from app import repair_profile_picture_scoring as repair
from lucos_photos_common import jobs
from lucos_photos_common.models import Person


def add_people(db):
    auto = Person(profile_auto_generated=True)
    pinned = Person(profile_auto_generated=False)
    none_yet = Person()
    db.add_all([auto, pinned, none_yet])
    db.commit()
    return auto, pinned


def test_find_auto_generated_skips_pinned_and_unpictured(db_session):
    auto, _ = add_people(db_session)
    assert repair.find_auto_generated(db_session) == [str(auto.id)]


def test_dry_run_enqueues_nothing(db_session, capsys):
    add_people(db_session)
    with patch.object(repair, "SessionLocal", return_value=db_session), patch.object(repair, "Queue") as queue_cls:
        repair.main([])
    queue_cls.assert_not_called()
    assert "auto-generated profile pictures: 1" in capsys.readouterr().out


def test_apply_enqueues_without_event_or_force(db_session):
    auto, _ = add_people(db_session)
    auto_id = str(auto.id)
    queue = MagicMock()
    with patch.object(repair, "SessionLocal", return_value=db_session), \
         patch.object(repair, "Queue", return_value=queue), \
         patch.object(repair, "Redis"):
        repair.main(["--apply"])
    queue.enqueue.assert_called_once()
    args, kwargs = queue.enqueue.call_args
    assert args == (jobs.generate_profile_picture, auto_id)
    assert kwargs["emit_event"] is False
    assert "force" not in kwargs


def test_limit_caps_enqueued(db_session):
    for _ in range(3):
        db_session.add(Person(profile_auto_generated=True))
    db_session.commit()
    queue = MagicMock()
    with patch.object(repair, "SessionLocal", return_value=db_session), \
         patch.object(repair, "Queue", return_value=queue), \
         patch.object(repair, "Redis"):
        repair.main(["--apply", "--limit", "2"])
    assert queue.enqueue.call_count == 2
