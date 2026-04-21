"""Serialization helpers: convert ORM models to API response dicts."""

import os
from pathlib import Path
from typing import Optional

from lucos_photos_common.models import Face, MediaItem, Person

DERIVATIVES_DIR = Path("/data/photos/derivatives")


def photo_url(photo_id) -> str:
    """Return the absolute URL for a photo's HTML view."""
    app_origin = os.environ.get("APP_ORIGIN", "")
    return f"{app_origin}/photos/{photo_id}"


def person_profile_picture_url(person_id) -> Optional[str]:
    """Return the absolute URL for a person's profile picture, or None if none exists."""
    profile_path = DERIVATIVES_DIR / f"{person_id}_profile.jpg"
    if not profile_path.exists():
        return None
    app_origin = os.environ.get("APP_ORIGIN", "")
    return f"{app_origin}/people/{person_id}/profile-picture"


def photo_file_urls(photo: MediaItem) -> tuple[str, str]:
    """Return (originalUrl, thumbnailUrl) for a photo, using the canonical file-serving routes."""
    ext = photo.file_extension or "bin"
    app_origin = os.environ.get("APP_ORIGIN", "")
    original_url = f"{app_origin}/photo_files/original/{photo.id}.{ext}"
    thumbnail_url = f"{app_origin}/photo_files/thumbnail/{photo.id}.{ext}"
    return original_url, thumbnail_url


def photo_to_dict(photo: MediaItem) -> dict:
    original_url, thumbnail_url = photo_file_urls(photo)
    return {
        "id": str(photo.id),
        "sha256Hash": photo.sha256_hash,
        "fileExtension": photo.file_extension,
        "mediaType": photo.media_type,
        "takenAt": photo.taken_at.isoformat() if photo.taken_at else None,
        "takenAtDisplay": photo.taken_at.strftime("%-d %B %Y") if photo.taken_at else None,
        "uploadedAt": photo.uploaded_at.isoformat() if photo.uploaded_at else None,
        "width": photo.width,
        "height": photo.height,
        "originalUrl": original_url,
        "thumbnailUrl": thumbnail_url,
    }


def face_to_dict_simple(face: Face) -> dict:
    """Minimal face dict for embedding inside a photo response."""
    return {
        "id": str(face.id),
        "personId": str(face.person_id) if face.person_id else None,
        "personConfirmed": face.person_confirmed,
        "boundingBox": {
            "x": face.bbox_x,
            "y": face.bbox_y,
            "width": face.bbox_width,
            "height": face.bbox_height,
        },
    }


def face_to_dict(face: Face) -> dict:
    return {
        "id": str(face.id),
        "photoId": str(face.photo_id),
        "personId": str(face.person_id) if face.person_id else None,
        "personConfirmed": face.person_confirmed,
        "boundingBox": {
            "x": face.bbox_x,
            "y": face.bbox_y,
            "width": face.bbox_width,
            "height": face.bbox_height,
        },
    }


def person_to_dict(person: Person, photo_count: Optional[int] = None) -> dict:
    data = {
        "id": str(person.id),
        "name": person.display_name,
        "contactId": person.contact_id,
        "isBackground": person.is_background,
        "createdAt": person.created_at.isoformat() if person.created_at else None,
        "profilePictureUrl": person_profile_picture_url(str(person.id)),
    }
    if photo_count is not None:
        data["photoCount"] = photo_count
    return data
