import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from pgvector.sqlalchemy import Vector

from .database import Base


class ProcessingState(enum.Enum):
    pending = "pending"
    processing = "processing"
    complete = "complete"
    failed = "failed"


class MediaItem(Base):
    __tablename__ = "media_item"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sha256_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    file_extension: Mapped[str] = mapped_column(String(10))
    media_type: Mapped[str] = mapped_column(String(10), nullable=False, default="photo", server_default="photo")
    taken_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    width: Mapped[Optional[int]] = mapped_column(Integer)
    height: Mapped[Optional[int]] = mapped_column(Integer)

    # Free-text description (optional; populated e.g. from Google Takeout sidecar JSON)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Video-specific columns (nullable; only populated for media_type='video')
    duration: Mapped[Optional[float]] = mapped_column(Float)
    codec: Mapped[Optional[str]] = mapped_column(String(50))
    video_width: Mapped[Optional[int]] = mapped_column(Integer)
    video_height: Mapped[Optional[int]] = mapped_column(Integer)
    fps: Mapped[Optional[float]] = mapped_column(Float)

    faces: Mapped[list["Face"]] = relationship(back_populates="media_item")
    processing_status: Mapped[Optional["ProcessingStatus"]] = relationship(back_populates="media_item", uselist=False)
    photo_people: Mapped[list["PhotoPerson"]] = relationship(back_populates="media_item")


# Keep Photo as an alias so existing imports don't break all at once.
# TODO: remove this alias once all call sites are updated to MediaItem.
Photo = MediaItem


def photo_date_label(photo: MediaItem) -> str:
    """Return a human-readable date identifier for use in loganne messages.

    Uses taken_at (the date the photo was captured) when available; falls back
    to uploaded_at (the date the file was received by the server).

    Examples: "taken 2024-05-15", "uploaded 2026-05-11"
    """
    if photo.taken_at is not None:
        return f"taken {photo.taken_at.date()}"
    return f"uploaded {photo.uploaded_at.date()}"


class ProcessingStatus(Base):
    __tablename__ = "processing_status"

    photo_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("media_item.id"), primary_key=True)
    state: Mapped[ProcessingState] = mapped_column(Enum(ProcessingState), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    error_message: Mapped[Optional[str]] = mapped_column(Text)

    media_item: Mapped["MediaItem"] = relationship(back_populates="processing_status")

    @property
    def photo(self) -> "MediaItem":
        return self.media_item

    @photo.setter
    def photo(self, value: "MediaItem") -> None:
        self.media_item = value


class Person(Base):
    __tablename__ = "person"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contact_id: Mapped[Optional[str]] = mapped_column(String, unique=True)
    display_name: Mapped[Optional[str]] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Profile picture: FK to the source media item and whether it was auto-generated
    profile_photo_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("media_item.id"), nullable=True)
    profile_auto_generated: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    # Background face: person was detected in a crowd and user does not want to identify them
    is_background: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # User has flagged this grouping as wrong but not yet corrected it (manually set/cleared, no auto-resolve)
    flagged_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    faces: Mapped[list["Face"]] = relationship(back_populates="person")
    photo_people: Mapped[list["PhotoPerson"]] = relationship(back_populates="person")
    profile_photo: Mapped[Optional["MediaItem"]] = relationship(foreign_keys=[profile_photo_id])


class Face(Base):
    __tablename__ = "face"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    photo_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("media_item.id"), index=True)
    person_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("person.id"), index=True)
    # Whether the person assignment has been manually confirmed (vs ML guess)
    person_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    # Bounding box as normalised coordinates (0.0–1.0, relative to image dimensions)
    bbox_x: Mapped[float] = mapped_column(Float)
    bbox_y: Mapped[float] = mapped_column(Float)
    bbox_width: Mapped[float] = mapped_column(Float)
    bbox_height: Mapped[float] = mapped_column(Float)
    # 512-dimension vector for InsightFace embeddings
    embedding: Mapped[Optional[list[float]]] = mapped_column(Vector(512))
    # Detection confidence score from InsightFace (proxy for sharpness/focus)
    det_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # 5-point keypoints [[x,y], ...] for eyes, nose, mouth corners — used for frontality estimation
    kps: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    media_item: Mapped["MediaItem"] = relationship(back_populates="faces")
    person: Mapped[Optional["Person"]] = relationship(back_populates="faces")

    @property
    def photo(self) -> "MediaItem":
        return self.media_item

    @photo.setter
    def photo(self, value: "MediaItem") -> None:
        self.media_item = value


class PhotoPerson(Base):
    __tablename__ = "photo_person"

    photo_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("media_item.id"), primary_key=True)
    person_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("person.id"), primary_key=True)

    media_item: Mapped["MediaItem"] = relationship(back_populates="photo_people")
    person: Mapped["Person"] = relationship(back_populates="photo_people")

    @property
    def photo(self) -> "MediaItem":
        return self.media_item

    @photo.setter
    def photo(self, value: "MediaItem") -> None:
        self.media_item = value


class TelemetryEvent(Base):
    """Telemetry event submitted by a client (e.g. the Android app).

    ``timestamp`` is the client-supplied event time (may be incorrect if the device clock
    is wrong or the event was queued before sending).  ``received_at`` is the server-side
    wall-clock time at which the row was inserted — always reliable.
    """
    __tablename__ = "telemetry_event"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    app_version: Mapped[Optional[str]] = mapped_column(String(50))
    # Client-supplied event timestamp (ISO-8601 string stored as UTC-aware datetime)
    timestamp: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Server-side receipt timestamp — always set on insert
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    # Flexible per-event-type payload; stored as JSONB in Postgres (JSON in SQLite for tests)
    data: Mapped[Optional[dict]] = mapped_column(JSON)
