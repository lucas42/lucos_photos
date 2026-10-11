"""Tests for enqueue_profile_picture's per-person job dedupe, against a real rq.Queue on fakeredis."""

import fakeredis
import pytest
from rq import Queue
from rq.job import Job, JobStatus

from lucos_photos_common.jobs import enqueue_profile_picture


@pytest.fixture
def queue():
    return Queue("photos", connection=fakeredis.FakeRedis())


def _ids(queue):
    return queue.job_ids


def _set_status(queue, job_id, status):
    Job.fetch(job_id, connection=queue.connection).set_status(status)


def test_collapses_repeats(queue):
    for _ in range(3):
        enqueue_profile_picture(queue, "p1")
    assert _ids(queue) == ["profilepic-p1"]


def test_follow_up_queued_in_rerun_slot_while_running(queue):
    enqueue_profile_picture(queue, "p1")
    _set_status(queue, "profilepic-p1", JobStatus.STARTED)
    queue.remove("profilepic-p1")  # a running job is no longer on the queue

    enqueue_profile_picture(queue, "p1")
    enqueue_profile_picture(queue, "p1")

    assert _ids(queue) == ["profilepic-p1-rerun"]


def test_rearms_primary_slot_after_finish(queue):
    enqueue_profile_picture(queue, "p1")
    _set_status(queue, "profilepic-p1", JobStatus.FINISHED)
    queue.remove("profilepic-p1")

    enqueue_profile_picture(queue, "p1")

    assert _ids(queue) == ["profilepic-p1"]
    assert Job.fetch("profilepic-p1", connection=queue.connection).get_status() == JobStatus.QUEUED


def test_scheduled_retry_not_duplicated(queue):
    enqueue_profile_picture(queue, "p1")
    _set_status(queue, "profilepic-p1", JobStatus.SCHEDULED)
    queue.remove("profilepic-p1")

    enqueue_profile_picture(queue, "p1")

    assert _ids(queue) == []


def test_both_slots_running_enqueues_nothing(queue):
    for job_id in ("profilepic-p1", "profilepic-p1-rerun"):
        enqueue_profile_picture(queue, "p1")
        _set_status(queue, job_id, JobStatus.STARTED)
        queue.remove(job_id)

    enqueue_profile_picture(queue, "p1")

    assert _ids(queue) == []


def test_different_persons_independent(queue):
    enqueue_profile_picture(queue, "p1")
    enqueue_profile_picture(queue, "p2")
    assert sorted(_ids(queue)) == ["profilepic-p1", "profilepic-p2"]


def test_errors_are_not_raised():
    class Boom:
        connection = None

    enqueue_profile_picture(Boom(), "p1")  # must not raise
