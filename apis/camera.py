from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional

from .wait_time import _count_people_from_image_b64, set_wait_for_hospital, get_wait_for_hospital

router = APIRouter(prefix="/camera-frame", tags=["camera"])

class CameraPushIn(BaseModel):
    hospital_id: str
    images_b64: List[str] = Field(default_factory=list)
    per_person_minutes: Optional[int] = Field(None, ge=1, le=60)

class CameraUploadResponse(BaseModel):
    hospital_id: str
    people: int
    per_person_minutes: int
    estimated_wait_minutes: int
    cameras: List[Dict[str, Any]]
    ts: str

@router.post("", response_model=CameraUploadResponse, summary="Push base64 frames for a hospital (simulating CCTV)")
def push_frames(body: CameraPushIn):
    if not body.hospital_id:
        raise HTTPException(400, "hospital_id required")
    if not body.images_b64:
        raise HTTPException(400, "images_b64 required")

    per_person = body.per_person_minutes or 10
    counts, cams = [], []
    for i, b64 in enumerate(body.images_b64):
        n = _count_people_from_image_b64(b64)
        counts.append(n)
        cams.append({"camera_id": f"cam-{i+1}", "people": n})

    total_people = sum(counts)
    est = int(total_people * per_person)

    record = {
        "hospital_id": body.hospital_id,
        "people": total_people,
        "per_person_minutes": per_person,
        "estimated_wait_minutes": est,
        "cameras": cams,
    }
    set_wait_for_hospital(body.hospital_id, record)
    latest = get_wait_for_hospital(body.hospital_id)
    if not latest:
        raise HTTPException(500, "Failed to store")
    return CameraUploadResponse(**latest)  # type: ignore

@router.get("/{hospital_id}", response_model=CameraUploadResponse, summary="Check the latest pushed wait estimate for a hospital")
def get_latest_camera_estimate(hospital_id: str):
    rec = get_wait_for_hospital(hospital_id)
    if not rec:
        raise HTTPException(status_code=404, detail="No upload yet for this hospital")
    return CameraUploadResponse(**rec)  # type: ignore
