import io
from unittest.mock import MagicMock, patch

from PIL import Image

from app import repair_exif_profile_pictures as repair
from lucos_photos_common.models import MediaItem, Person


def write_jpeg(path, orientation):
    img = Image.new("RGB", (40, 20), (10, 20, 30))
    exif = img.getexif()
    if orientation is not None:
        exif[274] = orientation
    img.save(path, format="JPEG", exif=exif.tobytes())


def add_person(db, sha, auto_generated=True):
    photo = MediaItem(sha256_hash=sha * 64, file_extension="jpg", width=40, height=20)
    db.add(photo)
    db.flush()
    person = Person(profile_photo_id=photo.id, profile_auto_generated=auto_generated)
    db.add(person)
    db.flush()
    return person, photo


def test_find_affected_picks_only_oriented_profile_photos(db_session, tmp_path):
    plain, _ = add_person(db_session, "a")
    untagged, _ = add_person(db_session, "b")
    rotated, _ = add_person(db_session, "c")
    pinned, _ = add_person(db_session, "d", auto_generated=False)
    missing, _ = add_person(db_session, "e")
    no_picture = Person()
    db_session.add(no_picture)
    db_session.commit()
    write_jpeg(tmp_path / f"{'a' * 64}.jpg", 1)
    write_jpeg(tmp_path / f"{'b' * 64}.jpg", None)
    write_jpeg(tmp_path / f"{'c' * 64}.jpg", 6)
    write_jpeg(tmp_path / f"{'d' * 64}.jpg", 3)

    with patch("lucos_photos_common.jobs.ORIGINALS_DIR", tmp_path):
        affected = repair.find_affected(db_session)

    assert {(p, manual, o) for p, _, manual, o in affected} == {
        (str(rotated.id), False, 6),
        (str(pinned.id), True, 3),
    }


def test_dry_run_enqueues_nothing(db_session, tmp_path, capsys):
    add_person(db_session, "c")
    db_session.commit()
    write_jpeg(tmp_path / f"{'c' * 64}.jpg", 6)

    with patch("lucos_photos_common.jobs.ORIGINALS_DIR", tmp_path), \
         patch.object(repair, "SessionLocal", return_value=db_session), \
         patch.object(repair, "Queue") as queue_cls:
        repair.main([])

    queue_cls.assert_not_called()
    assert "affected persons: 1" in capsys.readouterr().out


def test_apply_enqueues_force_without_event_and_manual_path(db_session, tmp_path):
    auto, _ = add_person(db_session, "c")
    pinned, pinned_photo = add_person(db_session, "d", auto_generated=False)
    db_session.commit()
    auto_id, pinned_id, pinned_photo_id = str(auto.id), str(pinned.id), str(pinned_photo.id)
    write_jpeg(tmp_path / f"{'c' * 64}.jpg", 6)
    write_jpeg(tmp_path / f"{'d' * 64}.jpg", 8)
    queue = MagicMock()

    with patch("lucos_photos_common.jobs.ORIGINALS_DIR", tmp_path), \
         patch.object(repair, "SessionLocal", return_value=db_session), \
         patch.object(repair, "Queue", return_value=queue), \
         patch.object(repair, "Redis"):
        repair.main(["--apply"])

    calls = {c.args[1]: c for c in queue.enqueue.call_args_list}
    auto_call = calls[auto_id]
    assert auto_call.args[0] is repair.jobs.generate_profile_picture
    assert auto_call.kwargs["force"] is True and auto_call.kwargs["emit_event"] is False
    pinned_call = calls[pinned_id]
    assert pinned_call.args[0] is repair.jobs.generate_manual_profile_picture
    assert pinned_call.args[2] == pinned_photo_id


def test_limit_caps_enqueued(db_session, tmp_path):
    for sha in "cde":
        add_person(db_session, sha)
        write_jpeg(tmp_path / f"{sha * 64}.jpg", 6)
    db_session.commit()
    queue = MagicMock()

    with patch("lucos_photos_common.jobs.ORIGINALS_DIR", tmp_path), \
         patch.object(repair, "SessionLocal", return_value=db_session), \
         patch.object(repair, "Queue", return_value=queue), \
         patch.object(repair, "Redis"):
        repair.main(["--apply", "--limit", "2"])

    assert queue.enqueue.call_count == 2
