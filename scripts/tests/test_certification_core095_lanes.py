"""Historical contexts own inherited lanes while preserving their old budget."""
import json
from types import SimpleNamespace

import certification_v070
import certification_v080
import ci_images
import pytest
from ci.certification_lanes import UNUSED_CORE095_LANES, UNUSED_CORE095_PROFILE
from ci.local_resources import memory_bytes


@pytest.mark.parametrize("module", [certification_v070, certification_v080])
@pytest.mark.parametrize("verified", [False, True])
def test_context_generators_bound_and_disable_new_lanes_without_changing_active_quotas(tmp_path, monkeypatch, module, verified):
    monkeypatch.setattr(module, "ROOT", tmp_path)
    if module is certification_v080:
        monkeypatch.setattr(module, "PRIVATE_ROOT", tmp_path / ".codex-local/v080")
        monkeypatch.setattr(module, "preflight", lambda *_: {"status": "PASS"})
        monkeypatch.setattr(module, "inventory", lambda _: {"containers": [], "volumes": []})
    else:
        monkeypatch.setattr(module, "inventory", lambda _: [])
    monkeypatch.setattr(module.shutil, "disk_usage", lambda _: SimpleNamespace(free=100 * 1024**3))
    monkeypatch.setattr(module.socket, "socket", lambda: _UnusedSocket())
    monkeypatch.delenv("TRACKVANCE_LOCAL_EXECUTION_ID", raising=False)
    images = {"backend": "sha256:" + "a" * 64, "web": "sha256:" + "b" * 64} if verified else None
    image_checks = []
    monkeypatch.setattr(ci_images, "verified_images", lambda: image_checks.append(True) or images)
    if module is certification_v080:
        directory = module.init("guard", 32080, "trackvance-certification")
    else:
        module.init("guard", 32070)
        directory, = (tmp_path / ".codex-local/v070").iterdir()
    context = json.loads((directory / "isolation.json").read_text())
    services = json.loads((directory / "compose.json").read_text())["services"]
    expected_image = images["backend"] if images else context["image"]
    assert image_checks == [True]
    active = set(module.SERVICES if module is certification_v070 else module.CORE_SERVICES)
    assert not active & UNUSED_CORE095_LANES
    for name in UNUSED_CORE095_LANES:
        lane = services[name]
        assert lane["profiles"] == [UNUSED_CORE095_PROFILE]
        assert lane["image"] == expected_image
        assert (lane["cpus"], lane["mem_limit"], lane["pids_limit"]) == (0.125, "128m", 64)
        assert lane["environment"]["TRACKVANCE_CERTIFICATION_PROJECT"] == context["project"]
        assert len(lane["volumes"]) == 3 and all(item["read_only"] for item in lane["volumes"])
    for name, lane in services.items():
        if name not in {"postgres", "web"}:
            assert lane["environment"]["TRACKVANCE_CERTIFICATION_PROJECT"] == context["project"]
            assert lane["image"] == expected_image
    assert sum(memory_bytes(lane["mem_limit"]) for lane in services.values()) <= 9 * 1024**3
    if module is certification_v080:
        assert (services["worker"]["mem_limit"], services["worker"]["cpus"]) == ("2048m", 2)
        assert (services["report-worker"]["mem_limit"], services["report-worker"]["cpus"]) == ("2048m", 2)
    else:
        assert (services["worker"]["mem_limit"], services["worker"]["cpus"]) == ("3g", 2)
        assert (services["acquisition-worker"]["mem_limit"], services["acquisition-worker"]["cpus"]) == ("1536m", 2)


class _UnusedSocket:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def bind(self, _):
        pass
