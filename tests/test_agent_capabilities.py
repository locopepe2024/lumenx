import pytest
from pydantic import ValidationError

from src.apps.agent_capabilities import CapabilityDraft, prepare_capability


def binding(order=1, role="reference_image"):
    return {"asset_id": "asset-1", "order": order, "role": role,
            "may_control": ["appearance"], "must_not_control": ["scene"]}


def test_reference_generation_preview_requires_confirmation_and_preserves_roles():
    draft = CapabilityDraft(capability="video.reference_generate", model="uniart/minimax-h3-vip",
                            idempotency_key="turn-12345678", duration=15,
                            references=[binding(), {**binding(2, "reference_video"), "asset_id": "asset-2"}])
    preview = prepare_capability(draft)
    assert preview["requires_confirmation"] is True
    assert preview["reference_roles"] == ["reference_image", "reference_video"]


def test_keyframe_and_reusable_reference_mix_is_rejected():
    with pytest.raises(ValidationError, match="cannot mix"):
        CapabilityDraft(capability="video.reference_generate", idempotency_key="turn-12345678",
                        references=[binding(1, "first_frame"), binding(2, "reference_image")])


def test_reference_audio_policy_requires_audio_binding():
    with pytest.raises(ValidationError, match="audio binding"):
        CapabilityDraft(capability="video.reference_generate", idempotency_key="turn-12345678",
                        audio_policy="reference", references=[binding()])


def test_reference_scopes_must_not_overlap():
    with pytest.raises(ValidationError, match="overlap"):
        CapabilityDraft(capability="video.reference_generate", idempotency_key="turn-12345678",
                        references=[{**binding(), "must_not_control": ["appearance"]}])
