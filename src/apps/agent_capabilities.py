"""Provider-neutral capability drafts for Agent mediated media generation.

This module validates a proposed action only. It never submits a provider job.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


CapabilityKey = Literal[
    "media.analyze",
    "image.generate",
    "video.reference_generate",
    "video.assemble",
]


class ReferenceBinding(BaseModel):
    asset_id: str = Field(min_length=1, max_length=200)
    order: int = Field(ge=1)
    role: Literal[
        "reference_image", "reference_video", "reference_audio", "first_frame", "last_frame"
    ]
    may_control: list[str] = Field(min_length=1)
    must_not_control: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def scopes_do_not_overlap(self):
        overlap = set(self.may_control) & set(self.must_not_control)
        if overlap:
            raise ValueError("reference control scopes overlap")
        return self


class CapabilityDraft(BaseModel):
    capability: CapabilityKey
    idempotency_key: str = Field(min_length=8, max_length=200)
    prompt: str = Field(default="", max_length=7000)
    model: str = Field(default="", max_length=200)
    duration: int | None = Field(default=None, ge=1, le=900)
    references: list[ReferenceBinding] = Field(default_factory=list)
    audio_policy: Literal["silent", "provider_generated", "reference"] = "silent"
    source_revision: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_shape(self):
        orders = [item.order for item in self.references]
        if orders != list(range(1, len(orders) + 1)):
            raise ValueError("reference orders must be contiguous and start at one")
        roles = {item.role for item in self.references}
        if "first_frame" in roles or "last_frame" in roles:
            if roles & {"reference_image", "reference_video", "reference_audio"}:
                raise ValueError("keyframe references cannot mix with reusable references")
        if self.capability == "video.reference_generate" and not self.references:
            raise ValueError("reference video generation requires references")
        if self.capability == "image.generate" and not self.prompt:
            raise ValueError("image generation requires a prompt")
        if self.capability == "video.assemble" and self.duration is None:
            raise ValueError("video assembly requires a target duration")
        if self.audio_policy == "reference" and "reference_audio" not in roles:
            raise ValueError("reference audio policy requires an audio binding")
        return self


class CapabilityConfirmation(BaseModel):
    draft: CapabilityDraft
    confirmation_token: str = Field(min_length=8, max_length=300)


def prepare_capability(draft: CapabilityDraft) -> dict[str, object]:
    """Return a bounded preview suitable for an explicit confirmation card."""
    return {
        "status": "proposed",
        "capability": draft.capability,
        "model": draft.model,
        "duration": draft.duration,
        "reference_count": len(draft.references),
        "reference_roles": [item.role for item in draft.references],
        "audio_policy": draft.audio_policy,
        "idempotency_key": draft.idempotency_key,
        "requires_confirmation": True,
    }
