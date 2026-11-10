from types import SimpleNamespace

import pytest

from al1s.bots.qq_headed.profile import prepare_client_profile
from al1s.components.host.qq_headed_identity import observe_headed_identity
from al1s.components.operations import compatible_release
from al1s.meme_workshop.runtime_identity import RuntimeIdentity
from al1s.components.host.bot_proof import RuntimeProof
from al1s.meme_workshop.operation_identity import stored_identity


def test_headed_identity_uses_client_instance_and_checks_account(tmp_path):
    profile = prepare_client_profile(tmp_path, "123456")
    identity = stored_identity("qq", {"AUTO_LOGIN_QQ": "123456", "PROTOCOL_MODE": "pmhq"}, tmp_path)
    assert identity.binding == {"account": "123456", "mode": "pmhq",
                                "client_instance_id": str(profile.instance_id)}
    with pytest.raises(ValueError, match="account"):
        observe_headed_identity(tmp_path, "654321")


def test_headed_preflight_rejects_direct_runtime_and_ignores_direct_pending():
    current = SimpleNamespace(qq_protocol_mode="pmhq", qq_identity=None, upgrade_unit="qq-bot",
        status="healthy", source_identity="a"*64, target=SimpleNamespace(kind="bot"),
        architecture="amd64", version="8.3.2")
    release = SimpleNamespace(component="qq-bot", runtime_version="1.1.0", architecture="amd64",
        native_version="8.3.2", compatible_from="8.3.2")
    assert "qq_headed_runtime_required" in compatible_release(current, release)
    release.runtime_version = "2.0.0"
    assert compatible_release(current, release) == []
    release.runtime_version = "2.0.1"
    assert compatible_release(current, release) == []
    release.runtime_version = "2.1.0"
    assert compatible_release(current, release) == []
    release.runtime_version = "2.99.0"
    assert "qq_headed_runtime_required" in compatible_release(current, release)
    current.qq_quant_v1 = True
    release.runtime_version = "2.0.1"
    assert "qq_quant_runtime_required" in compatible_release(current, release)
    release.runtime_version = '2.2.0'
    assert compatible_release(current, release) == []
    current.qq_freqtrade_v1 = True
    release.runtime_version = '2.1.0'
    assert 'qq_freqtrade_runtime_required' in compatible_release(current, release)
    release.runtime_version = '2.3.0'
    assert compatible_release(current, release) == []
    current.qq_freqtrade_futures_v1 = True
    release.runtime_version = '2.2.0'
    assert 'qq_freqtrade_futures_runtime_required' in compatible_release(current, release)
    release.runtime_version = '2.3.0'
    assert compatible_release(current, release) == []
    current.qq_freqtrade_visual_v1 = True
    assert 'qq_freqtrade_visual_runtime_required' in compatible_release(current, release)
    release.runtime_version = '2.4.0'
    assert compatible_release(current, release) == []
    release.runtime_version = '2.5.0'
    assert compatible_release(current, release) == []
    current.qq_freqtrade_capital_v1 = True
    release.runtime_version = '2.4.0'
    assert 'qq_freqtrade_capital_runtime_required' in compatible_release(current, release)
    release.runtime_version = '2.5.0'
    assert compatible_release(current, release) == []


def test_visual_capability_requires_futures_in_manifest_and_proof():
    identity = dict(
        role="qq", runtime_version="2.4.0", source_sha256="a" * 64,
        requirements_sha256="b" * 64, qq_protocol_mode="pmhq", qq_headed_v1=True,
        qq_client_version="3.0.0", pmhq_version="1.0.0", qq_quant_v1=True,
        qq_freqtrade_v1=True, qq_freqtrade_visual_v1=True,
    )
    with pytest.raises(ValueError, match="visual_manifest"):
        RuntimeIdentity.model_validate(identity)
    identity["qq_freqtrade_futures_v1"] = True
    assert RuntimeIdentity.model_validate(identity).qq_freqtrade_visual_v1
    identity["qq_freqtrade_visual_v1"] = False
    identity["qq_freqtrade_capital_v1"] = True
    with pytest.raises(ValueError, match="capital_manifest"):
        RuntimeIdentity.model_validate(identity)
    identity["qq_freqtrade_visual_v1"] = True
    assert RuntimeIdentity.model_validate(identity).qq_freqtrade_capital_v1

    proof = dict(
        role="qq", native_version="8.3.2", runtime_version="2.4.0", render_proof=True,
        qq_protocol_mode="pmhq", qq_headed_v1=True, qq_client_version="3.0.0",
        pmhq_version="1.0.0", qq_quant_v1=True, qq_freqtrade_v1=True,
        qq_freqtrade_visual_v1=True,
    )
    with pytest.raises(ValueError, match="visual_proof"):
        RuntimeProof.model_validate(proof)
    proof["qq_freqtrade_futures_v1"] = True
    assert RuntimeProof.model_validate(proof).qq_freqtrade_visual_v1
    proof["qq_freqtrade_visual_v1"] = False
    proof["qq_freqtrade_capital_v1"] = True
    with pytest.raises(ValueError, match="capital_proof"):
        RuntimeProof.model_validate(proof)
    proof["qq_freqtrade_visual_v1"] = True
    assert RuntimeProof.model_validate(proof).qq_freqtrade_capital_v1


def test_reply_templates_runtime_cannot_be_replaced_by_an_older_runtime():
    current = SimpleNamespace(qq_protocol_mode="pmhq", qq_identity=None, upgrade_unit="qq-bot",
        status="healthy", source_identity="a"*64, target=SimpleNamespace(kind="bot"),
        architecture="amd64", version="8.3.2", qq_quant_v1=True, qq_freqtrade_v1=True,
        qq_freqtrade_futures_v1=True, qq_freqtrade_visual_v1=True, qq_freqtrade_capital_v1=True,
        qq_command_replies_v1=True)
    release = SimpleNamespace(component="qq-bot", runtime_version="2.5.0", architecture="amd64",
        native_version="8.3.2", compatible_from="8.3.2")
    assert "qq_command_replies_runtime_required" in compatible_release(current, release)
    release.runtime_version="2.6.0"
    assert compatible_release(current, release)==[]


def test_unified_ai_release_preserves_existing_capabilities():
    current = SimpleNamespace(qq_protocol_mode='pmhq', qq_identity=None, upgrade_unit='qq-bot',
        status='healthy', source_identity='a' * 64, target=SimpleNamespace(kind='bot'),
        architecture='amd64', version='8.3.2', qq_quant_v1=True, qq_freqtrade_v1=True,
        qq_freqtrade_futures_v1=True, qq_freqtrade_visual_v1=True, qq_freqtrade_capital_v1=True,
        qq_command_replies_v1=True, qq_ai_v1=True, qq_ai_personas_v1=True,
        qq_ai_network_routes_v1=True, qq_ai_group_reply_v1=True, qq_ai_smart_reply_v1=True)
    release = SimpleNamespace(component='qq-bot', runtime_version='2.12.0', architecture='amd64',
                              native_version='8.3.2', compatible_from='8.3.2')
    assert compatible_release(current, release) == []
    release.runtime_version = '2.10.0'
    assert 'qq_ai_smart_reply_runtime_required' in compatible_release(current, release)


def test_drawing_release_cannot_be_replaced_by_a_runtime_without_drawing():
    current = SimpleNamespace(qq_protocol_mode='pmhq', qq_identity=None, upgrade_unit='qq-bot',
        status='healthy', source_identity='a' * 64, target=SimpleNamespace(kind='bot'),
        architecture='amd64', version='8.3.2', qq_comfy_drawing_v1=True)
    release = SimpleNamespace(component='qq-bot', runtime_version='2.12.0', architecture='amd64',
                              native_version='8.3.2', compatible_from='8.3.2')
    assert 'qq_comfy_drawing_runtime_required' in compatible_release(current, release)
    release.runtime_version = '2.13.0'
    assert compatible_release(current, release) == []

    proof = dict(role='qq', native_version='8.3.2', runtime_version='2.13.0', render_proof=True,
                 qq_comfy_drawing_v1=True)
    with pytest.raises(ValueError, match='drawing'):
        RuntimeProof.model_validate(proof)


def test_unified_candidate_rejects_missing_smart_reply_proof(monkeypatch):
    import json
    from al1s.components.host.qq_candidate_builder import QqCandidateBuilder

    image_id = 'sha256:' + 'a' * 64
    class Runner:
        def run(self, args, *unused):
            if args[:3] == ['docker', 'image', 'inspect']:
                return json.dumps([{'Id': image_id, 'Architecture': 'amd64',
                    'Config': {'Labels': {'org.al1s.bot-role': 'qq',
                        'org.al1s.runtime-version': '2.12.0', 'org.al1s.qq-protocol-mode': 'pmhq'}}}])
            return ''

    proof = RuntimeProof(role='qq', native_version='8.3.2', runtime_version='2.12.0',
        render_proof=True, qq_protocol_mode='pmhq', qq_headed_v1=True, qq_client_version='3.2.34',
        pmhq_version='9.0.6', qq_quant_v1=True, qq_freqtrade_v1=True,
        qq_freqtrade_futures_v1=True, qq_freqtrade_visual_v1=True, qq_freqtrade_capital_v1=True,
        qq_command_replies_v1=True, qq_ai_v1=True, qq_ai_personas_v1=True,
        qq_ai_network_routes_v1=True, qq_ai_group_reply_v1=True)
    monkeypatch.setattr('al1s.components.host.qq_candidate_builder.probe_bot_image', lambda *_: proof)
    release = SimpleNamespace(platform_manifest_digest='sha256:' + 'b' * 64,
        runtime_artifact_digest=image_id, runtime_version='2.12.0', native_version='8.3.2',
        architecture='amd64')
    builder = QqCandidateBuilder(None, None, Runner())
    with pytest.raises(ValueError, match='qq_headed_candidate_unverified'):
        builder.build(release)
    proof = proof.model_copy(update={'qq_ai_smart_reply_v1': True})
    assert builder.build(release).proof is True


def test_drawing_candidate_requires_drawing_proof(monkeypatch):
    import json
    from al1s.components.host.qq_candidate_builder import QqCandidateBuilder
    image_id = 'sha256:' + 'a' * 64
    class Runner:
        def run(self, args, *unused):
            if args[:3] == ['docker', 'image', 'inspect']:
                return json.dumps([{'Id': image_id, 'Architecture': 'amd64', 'Config': {'Labels': {
                    'org.al1s.bot-role': 'qq', 'org.al1s.runtime-version': '2.13.0',
                    'org.al1s.qq-protocol-mode': 'pmhq'}}}])
            return ''
    proof = RuntimeProof(role='qq', native_version='8.3.2', runtime_version='2.13.0', render_proof=True,
        qq_protocol_mode='pmhq', qq_headed_v1=True, qq_client_version='3.2.34', pmhq_version='9.0.6',
        qq_quant_v1=True, qq_freqtrade_v1=True, qq_freqtrade_futures_v1=True, qq_freqtrade_visual_v1=True,
        qq_freqtrade_capital_v1=True, qq_command_replies_v1=True, qq_ai_v1=True, qq_ai_personas_v1=True,
        qq_ai_network_routes_v1=True, qq_ai_group_reply_v1=True, qq_ai_smart_reply_v1=True)
    monkeypatch.setattr('al1s.components.host.qq_candidate_builder.probe_bot_image', lambda *_: proof)
    release = SimpleNamespace(platform_manifest_digest='sha256:' + 'b' * 64, runtime_artifact_digest=image_id,
        runtime_version='2.13.0', native_version='8.3.2', architecture='amd64')
    builder = QqCandidateBuilder(None, None, Runner())
    with pytest.raises(ValueError, match='qq_headed_candidate_unverified'):
        builder.build(release)
    proof = proof.model_copy(update={'qq_comfy_drawing_v1': True})
    assert builder.build(release).proof is True
