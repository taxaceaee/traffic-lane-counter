"""Models API — CRUD + upload for registered detection models."""

import logging
import shutil
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel, Field

logger = logging.getLogger("trafficflow.models")

router = APIRouter(prefix="/api/models", tags=["models"])

_MODELS_PATH = Path("configs/models.yaml")
_WEIGHTS_DIR = Path("weights")

_ALLOWED_EXTENSIONS = {".pt", ".pth", ".onnx", ".engine", ".trt", ".torchscript"}


def _read_models() -> list[dict[str, Any]]:
    if not _MODELS_PATH.exists():
        return []
    with open(_MODELS_PATH, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return raw.get("models", [])


def _write_models(models: list[dict[str, Any]]) -> None:
    _MODELS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_MODELS_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump({"models": models}, f, sort_keys=False)


def _serialize_model(m: dict[str, Any]) -> dict[str, str]:
    return {
        "model_id": m.get("model_id", ""),
        "model_path": m.get("path", ""),
        "class_mode": m.get("class_mode", "coco_pretrained"),
        "description": m.get("description", ""),
    }


class ModelCreateRequest(BaseModel):
    model_id: str = Field(..., min_length=1, description="Unique model identifier")
    model_path: str = Field(..., min_length=1, description="Path or name of weights file (e.g. yolo11n.pt)")
    class_mode: str = Field(default="coco_pretrained", description="Class mode key")
    description: str = Field(default="", description="Human-readable description")


class ModelUpdateRequest(BaseModel):
    model_id: str | None = Field(None, description="New model_id for rename")
    model_path: str | None = None
    class_mode: str | None = None
    description: str | None = None


@router.get("")
async def list_models():
    return [_serialize_model(m) for m in _read_models()]


@router.post("", status_code=201)
async def create_model(req: ModelCreateRequest):
    """Register a model entry pointing to an existing weights file."""
    models = _read_models()
    if any(m.get("model_id") == req.model_id for m in models):
        raise HTTPException(409, f"Model already exists: {req.model_id}")
    models.append({
        "model_id": req.model_id,
        "path": req.model_path,
        "class_mode": req.class_mode,
        "description": req.description,
    })
    _write_models(models)
    logger.info("Model created: %s (%s)", req.model_id, req.model_path)
    return _serialize_model(models[-1])


@router.post("/upload", status_code=201)
async def upload_model(
    file: UploadFile = File(...),
    model_id: str = Form(...),
    class_mode: str = Form("coco_pretrained"),
    description: str = Form(""),
):
    """Upload a model weights file and register it."""
    # Validate extension
    ext = Path(file.filename or "").suffix.lower()
    if ext not in _ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file extension '{ext}'. Allowed: {', '.join(sorted(_ALLOWED_EXTENSIONS))}")

    # Check unique model_id
    models = _read_models()
    if any(m.get("model_id") == model_id for m in models):
        raise HTTPException(409, f"Model already exists: {model_id}")

    # Save file
    _WEIGHTS_DIR.mkdir(parents=True, exist_ok=True)
    dest = _WEIGHTS_DIR / f"{model_id}{ext}"
    try:
        with open(dest, "wb") as f:
            shutil.copyfileobj(file.file, f)
    except Exception as e:
        raise HTTPException(500, f"Failed to save weights file: {e}")
    finally:
        await file.close()

    weight_path = str(dest)
    models.append({
        "model_id": model_id,
        "path": weight_path,
        "class_mode": class_mode,
        "description": description,
    })
    _write_models(models)
    logger.info("Model uploaded + registered: %s -> %s", model_id, weight_path)
    return _serialize_model(models[-1])


@router.put("/{model_id}")
async def update_model(model_id: str, req: ModelUpdateRequest):
    """Update model fields. Supports rename via model_id in body."""
    models = _read_models()
    for i, m in enumerate(models):
        if m.get("model_id") == model_id:
            new_id = req.model_id
            if new_id is not None and new_id != model_id:
                # Check new ID doesn't clash (excluding self)
                if any(other.get("model_id") == new_id for j, other in enumerate(models) if j != i):
                    raise HTTPException(409, f"Model ID already exists: {new_id}")
                m["model_id"] = new_id
            if req.model_path is not None:
                m["path"] = req.model_path
            if req.class_mode is not None:
                m["class_mode"] = req.class_mode
            if req.description is not None:
                m["description"] = req.description
            _write_models(models)
            logger.info("Model updated: %s -> %s", model_id, new_id or model_id)
            return _serialize_model(m)
    raise HTTPException(404, f"Model not found: {model_id}")


@router.delete("/{model_id}")
async def delete_model(model_id: str, remove_file: bool = False):
    """Delete a model registration. Optionally remove weights file with ?remove_file=true."""
    models = _read_models()
    target = None
    filtered = []
    for m in models:
        if m.get("model_id") == model_id:
            target = m
        else:
            filtered.append(m)
    if target is None:
        raise HTTPException(404, f"Model not found: {model_id}")

    _write_models(filtered)

    # Optionally delete weights file
    if remove_file:
        weight_path = target.get("path", "")
        if weight_path:
            p = Path(weight_path)
            if p.exists() and p.is_file():
                p.unlink()
                logger.info("Weights file deleted: %s", weight_path)

    logger.info("Model deleted: %s", model_id)
    return {"status": "deleted", "model_id": model_id}
