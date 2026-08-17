"""Learning, decay, feature and killswitch management routes."""

from __future__ import annotations

from typing import Any, NoReturn
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from brain_v42.codex_gateway.audit import log_write
from brain_v42.codex_gateway.dependencies import GatewayServices
from brain_v42.codex_gateway.schemas import EntityType, FeaturePatchPayload
from brain_v42.services.entity_maintenance_service import UnknownEntityTypeError
from brain_v42.services.feature_service import FeatureStateConflictError

_CODEX_ACTOR = "red-codex"
_CODEX_PROJECT_GROUP = "red"


def _not_found(label: str, entity_id: UUID) -> NoReturn:
    raise HTTPException(status_code=404, detail=f"{label} '{entity_id}' not found")


def _json(value: Any) -> JSONResponse:
    return JSONResponse(content=jsonable_encoder(value))


def build_management_router(services: GatewayServices) -> APIRouter:
    router = APIRouter(tags=["management"])

    @router.post("/learnings/{learning_id}/validate", response_model=None)
    async def validate_learning(learning_id: UUID) -> JSONResponse:
        learning = await services.learning.validate(
            learning_id,
            project_group=_CODEX_PROJECT_GROUP,
        )
        if learning is None:
            _not_found("Learning", learning_id)
        log_write("learning.validate", _CODEX_ACTOR, learning_id=str(learning_id))
        return _json(learning)

    @router.post("/entities/{entity_type}/{entity_id}/refresh", response_model=None)
    async def refresh_entity(entity_type: EntityType, entity_id: UUID) -> JSONResponse:
        try:
            refreshed = await services.entity_maintenance.refresh(
                entity_type,
                entity_id,
                project_group=_CODEX_PROJECT_GROUP,
            )
        except UnknownEntityTypeError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if refreshed is None:
            _not_found(entity_type.title(), entity_id)
        log_write(
            "entity.refresh",
            _CODEX_ACTOR,
            entity_type=entity_type,
            entity_id=str(entity_id),
        )
        return _json(refreshed)

    @router.patch("/features/{feature_id}", response_model=None)
    async def patch_feature(
        feature_id: UUID,
        payload: FeaturePatchPayload,
    ) -> JSONResponse:
        try:
            feature = await services.feature.patch(
                feature_id,
                status=payload.status,
                pinned=payload.pinned,
                archived=payload.archived,
                project_group=_CODEX_PROJECT_GROUP,
            )
        except FeatureStateConflictError as error:
            raise HTTPException(
                status_code=409,
                detail="Feature state changed; review required",
            ) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if feature is None:
            _not_found("Feature", feature_id)
        log_write("feature.patch", _CODEX_ACTOR, feature_id=str(feature_id))
        return _json(feature)

    @router.get("/killswitches", response_model=None)
    async def get_killswitches() -> JSONResponse:
        return _json(await services.killswitch.read())

    return router
