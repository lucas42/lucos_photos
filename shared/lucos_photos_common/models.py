import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class ProcessingState(enum.Enum):
    pending = "pending"
    processing = "processing"
    complete = "complete"
    failed = "failed"


class Photo(Base):
    __tablename__ = "photo"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    file_extension: Mapped[str] = mapped_column(String(10))
    taken_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    width: Mapped[Optional[int]] = mapped_column(Integer)
    height: Mapped[Optional[int]] = mapped_column(Integer)

    faces: Mapped[list["Face"]] = relationship(back_populates="photo")
    processing_status: Mapped[Optional["ProcessingStatus"]] = relationship(back_populates="photo", uselist=False)
    photo_persons: Mapped[list["PhotoPerson"]] = relationship(back_populates="photo")


class ProcessingStatus(Base):
    __tablename__ = "processing_status"

    photo_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("photo.id"), primary_key=True)
    state: Mapped[ProcessingState] = mapped_column(Enum(ProcessingState))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    error_message: Mapped[Optional[str]] = mapped_column(Text)

    photo: Mapped["Photo"] = relationship(back_populates="processing_status")


class Person(Base):
    __tablename__ = "person"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contact_id: Mapped[Optional[str]] = mapped_column(String, unique=True)
    display_name: Mapped[Optional[str]] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    faces: Mapped[list["Face"]] = relationship(back_populates="person")
    photo_persons: Mapped[list["PhotoPerson"]] = relationship(back_populates="person")


class Face(Base):
    __tablename__ = "face"

    # id is also used directly as the Qdrant point ID for face embeddings
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    photo_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("photo.id"))
    person_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("person.id"))
    # Whether the person assignment has been manually confirmed (vs ML guess)
    person_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    # Bounding box as normalised coordinates (0.0–1.0, relative to image dimensions)
    bbox_x: Mapped[float] = mapped_column(Float)
    bbox_y: Mapped[float] = mapped_column(Float)
    bbox_width: Mapped[float] = mapped_column(Float)
    bbox_height: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    photo: Mapped["Photo"] = relationship(back_populates="faces")
    person: Mapped[Optional["Person"]] = relationship(back_populates="faces")


class PhotoPerson(Base):
    __tablename__ = "photo_person"

    photo_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("photo.id"), primary_key=True)
    person_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("person.id"), primary_key=True)

    photo: Mapped["Photo"] = relationship(back_populates="photo_persons")
    person: Mapped["Person"] = relationship(back_populates="photo_persons")
