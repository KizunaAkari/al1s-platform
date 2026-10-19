import json

import pytest

from al1s.meme_workshop.runtime_probe import prove_runtime


def test_headed_probe_requires_real_native_versions_and_display(tmp_path):
    manifest, package, supervisor = [tmp_path / s for s in ("manifest", "package", "supervisor")]
    manifest.write_text(json.dumps(dict(role="qq", runtime_version="2.0.0", native_version="8.3.2",
        source_sha256="a"*64, requirements_sha256="b"*64, qq_protocol_mode="pmhq",
        qq_client_version="3.2.34-53644", pmhq_version="9.0.6", qq_headed_v1=True)))
    package.write_text('{"version":"8.3.2"}')
    supervisor.write_text("command=python -m al1s.bots.qq_headed.pmhq_process")
    with pytest.raises(ValueError, match="headed"):
        prove_runtime(manifest, package, "qq", supervisor=supervisor)
