"""Models API — CRUD + upload for registered detection models."""

import logging
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from tf_api.api.routes_auth import get_current_user, require_admin
from tf_common.safe_path import safe_join, validate_identifier
from tf_core.config.validators import CLASS_MODES

logger = logging.getLogger("trafficflow.models")

router = APIRouter(prefix="/api/models", tags=["models"])

_MODELS_PATH = Path("configs/models.yaml")
_WEIGHTS_DIR = Path("weights")

_ALLOWED_EXTENSIONS = {".pt", ".pth", ".onnx", ".engine", ".trt", ".torchscript"}
_MAX_MODEL_BYTES = 512 * 1024 * 1024


def _read_models() -> list[dict[str, Any]]:
    if not _MODELS_PATH.exists():
        return []
    with open(_MODELS_PATH, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return raw.get("models", [])


def _write_models(models: list[dict[str, Any]]) -> None:
    _MODELS_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if _MODELS_PATH.exists():
        with open(_MODELS_PATH, encoding="utf-8") as f:
            existing = yaml.safe_load(f) or {}
    existing["models"] = models
    with open(_MODELS_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(existing, f, sort_keys=False)


def _model_path(value: str, *, require_exists: bool = True) -> Path:
    candidate = Path(value)
    if candidate.is_absolute() or candidate.parts[:1] == (_WEIGHTS_DIR.name,):
        path = candidate.resolve()
    else:
        path = safe_join(_WEIGHTS_DIR, value)
    try:
        path.relative_to(_WEIGHTS_DIR.resolve())
    except ValueError:
        raise HTTPException(422, "Model path must stay under the weights directory") from None
    if require_exists and not path.is_file():
        raise HTTPException(422, f"Model file does not exist: {path.name}")
    return path


def _validate_class_mode(class_mode: str) -> str:
    if class_mode not in CLASS_MODES:
        raise HTTPException(422, f"Unsupported class_mode: {class_mode}")
    return class_mode


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
async def list_models(_user: dict = Depends(get_current_user)):
    return [_serialize_model(m) for m in _read_models()]


@router.post("", status_code=201)
async def create_model(req: ModelCreateRequest, _user: dict = Depends(require_admin)):
    validate_identifier(req.model_id, name="model_id")
    _validate_class_mode(req.class_mode)
    path = _model_path(req.model_path)
    models = _read_models()
    if any(m.get("model_id") == req.model_id for m in models):
        raise HTTPException(409, f"Model already exists: {req.model_id}")
    models.append({
        "model_id": req.model_id,
        "path": str(path),
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
    _user: dict = Depends(require_admin),
):
    validate_identifier(model_id, name="model_id")
    _validate_class_mode(class_mode)
    ext = Path(file.filename or "").suffix.lower()
    if ext not in _ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file extension '{ext}'. Allowed: {', '.join(sorted(_ALLOWED_EXTENSIONS))}")

    models = _read_models()
    if any(m.get("model_id") == model_id for m in models):
        raise HTTPException(409, f"Model already exists: {model_id}")

    _WEIGHTS_DIR.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - bounded local upload setup
    dest = _WEIGHTS_DIR / f"{model_id}{ext}"
    bytes_written = 0
    try:
        with open(dest, "wb") as f:  # noqa: ASYNC230 - streamed bounded upload
            while chunk := file.file.read(1024 * 1024):
                bytes_written += len(chunk)
                if bytes_written > _MAX_MODEL_BYTES:
                    raise HTTPException(413, "Model file exceeds 512 MiB")
                f.write(chunk)
    except Exception as e:
        dest.unlink(missing_ok=True)
        if isinstance(e, HTTPException):
            raise
        raise HTTPException(500, f"Failed to save weights file: {e}") from e
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
async def update_model(
    model_id: str,
    req: ModelUpdateRequest,
    _user: dict = Depends(require_admin),
):
    validate_identifier(model_id, name="model_id")
    if req.class_mode is not None:
        _validate_class_mode(req.class_mode)
    models = _read_models()
    for i, m in enumerate(models):
        if m.get("model_id") == model_id:
            new_id = req.model_id
            if new_id is not None and new_id != model_id:
                validate_identifier(new_id, name="model_id")
                if any(other.get("model_id") == new_id for j, other in enumerate(models) if j != i):
                    raise HTTPException(409, f"Model ID already exists: {new_id}")
                m["model_id"] = new_id
            if req.model_path is not None:
                m["path"] = str(_model_path(req.model_path))
            if req.class_mode is not None:
                m["class_mode"] = req.class_mode
            if req.description is not None:
                m["description"] = req.description
            _write_models(models)
            logger.info("Model updated: %s -> %s", model_id, new_id or model_id)
            return _serialize_model(m)
    raise HTTPException(404, f"Model not found: {model_id}")


@router.delete("/{model_id}")
async def delete_model(
    model_id: str,
    remove_file: bool = False,
    _user: dict = Depends(require_admin),
):
    validate_identifier(model_id, name="model_id")
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

    if remove_file:
        weight_path = target.get("path", "")
        if weight_path:
            p = _model_path(weight_path, require_exists=False)
            if p.exists() and p.is_file():
                p.unlink()
                logger.info("Weights file deleted: %s", weight_path)

    logger.info("Model deleted: %s", model_id)
    return {"status": "deleted", "model_id": model_id}
