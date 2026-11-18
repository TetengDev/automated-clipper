"""docker-compose.yml checks that need no Docker: the file is parsed as YAML (PyYAML
resolves the `<<` merge keys the way Compose does)."""

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]


def _compose(name: str) -> dict:
    return yaml.safe_load((REPO / name).read_text(encoding="utf-8"))


def _services(name: str) -> dict:
    return _compose(name)["services"]


def _mount_for(service: dict, path: str) -> tuple[str, bool]:
    """(host source, read-only) of the bind mount that serves `path` in the container:
    the one with the longest matching target, as Docker stacks nested mounts."""
    mounts = {}
    for volume in service["volumes"]:
        source, target, *mode = volume.split(":")
        mounts[target] = (source, bool(mode) and "ro" in mode[0].split(","))
    target = max((t for t in mounts if path == t or path.startswith(f"{t}/")), key=len)
    return mounts[target]


@pytest.mark.parametrize(
    ("service", "read_only"),
    [("review", True), ("monitor", True), ("worker", False)],
)
def test_only_the_worker_can_write_the_models_dir(service: str, read_only: bool) -> None:
    """CLP-75: the review web app and the monitor never fetch models, so they get the model
    store read-only; the worker downloads and repairs it."""
    config = _services("docker-compose.yml")[service]
    models = config["environment"]["CLIPPER_MODELS_DIR"]
    assert _mount_for(config, models) == (
        ("./data/models", True) if read_only else ("./data", False)
    )
    assert _mount_for(config, "/app/data/state.db") == ("./data", False)  # the DB stays writable
    assert config["read_only"] is True


def test_override_does_not_remount_volumes() -> None:
    for name, service in _services("docker-compose.override.yml").items():
        assert "volumes" not in service, name


@pytest.mark.parametrize("service", ["review", "monitor"])
def test_read_only_models_services_keep_the_shared_volumes(service: str) -> None:
    """The read-only anchor restates the shared volume list; it must not drift from it."""
    compose = _compose("docker-compose.yml")
    assert compose["services"][service]["volumes"] == [
        *compose["x-clipper"]["volumes"],
        "./data/models:/app/data/models:ro",
    ]
