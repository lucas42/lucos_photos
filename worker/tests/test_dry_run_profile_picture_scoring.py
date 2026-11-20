from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import numpy as np
from PIL import Image

from app import dry_run_profile_picture_scoring as dry_run
from lucos_photos_common.models import Face, MediaItem, Person


def test_reports_old_and_new_pick_without_writing(db_session, tmp_path, capsys):
    person = Person(profile_auto_generated=True)
    db_session.add(person)
    db_session.flush()
    sharp = np.random.default_rng(0).integers(0, 255, (200, 200), dtype=np.uint8)
    for i, pixels in enumerate([sharp, np.full((200, 200), 128, dtype=np.uint8)]):
        sha = f"{i:064x}"
        photo = MediaItem(sha256_hash=sha, file_extension="png", width=200, height=200,
                          taken_at=datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(days=i))
        db_session.add(photo)
        db_session.flush()
        Image.fromarray(pixels).save(tmp_path / f"{sha}.png")
        db_session.add(Face(photo_id=photo.id, person_id=person.id, person_confirmed=False, bbox_x=0.1, bbox_y=0.1,
                            bbox_width=0.8, bbox_height=0.8, det_score=0.9,
                            kps=[[100, 100], [140, 100], [120, 120], [105, 140], [135, 140]]))
    db_session.commit()
    person_id = str(person.id)

    with patch("lucos_photos_common.jobs.ORIGINALS_DIR", tmp_path), patch.object(dry_run, "SessionLocal", return_value=db_session):
        dry_run.main(["--sample", "5"])

    out = capsys.readouterr().out
    assert "#1 faces=2 tier=2 changed=True" in out
    assert "changed: 1 of 1" in out
    assert person_id not in out
