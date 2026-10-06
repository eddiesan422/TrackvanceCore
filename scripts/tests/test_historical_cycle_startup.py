"""Historical suites start only services with their prepared private images."""

import importlib
import json
import sys

import pytest


class StartupValidated(Exception):
    """Stop before running the expensive real population after startup validation."""


@pytest.mark.parametrize("module_name", ["corrections_cycle", "v070_cycle"])
def test_unprepared_new_base_services_never_start_and_owned_cleanup_remains(monkeypatch, tmp_path, module_name):
    cycle = importlib.import_module(module_name)
    certification = cycle.certification
    context = {"project": "trackvance-v070-test-startup-012345abcdef", "image": "private-fixture:backend"}
    prepared = set(certification.SERVICES)
    merged_services = prepared | {"report-worker", "future-unprepared-worker"}
    calls = []
    main_checked = []

    def initialize(*_arguments):
        print(json.dumps({"context": str(tmp_path)}))

    def compose(directory, identity, arguments, **_options):
        assert directory == tmp_path and identity is context
        calls.append(arguments)
        if arguments[0] != "up":
            return ""
        selected = []
        iterator = iter(arguments[1:])
        for argument in iterator:
            if argument == "--wait-timeout":
                next(iterator)
            elif not argument.startswith("-"):
                selected.append(argument)
        # Compose starts every merged service when no explicit service is
        # selected. With --no-build, a new inherited service has no image.
        effective = set(selected) if selected else merged_services
        if "--no-build" in arguments and effective - prepared:
            raise RuntimeError("An unprepared inherited service would start.")
        assert tuple(selected) == certification.SERVICES
        assert "--wait" in arguments
        raise StartupValidated()

    monkeypatch.setattr(certification, "init", initialize)
    monkeypatch.setattr(certification, "load_context", lambda _path: (tmp_path, context))
    monkeypatch.setattr(certification, "compose", compose)
    monkeypatch.setattr(certification, "assert_main_unchanged", lambda identity: main_checked.append(identity))
    monkeypatch.setattr(cycle, "run", lambda *_arguments, **_options: None)
    if module_name == 'corrections_cycle':
        monkeypatch.setattr(cycle, 'prepare_images', lambda *_arguments: {})
    arguments = [module_name]
    if module_name == "v070_cycle":
        arguments.extend(["--tier-mib", "1024", "--reuse-images"])
    monkeypatch.setattr(sys, "argv", arguments)

    with pytest.raises(StartupValidated):
        cycle.main()

    assert calls[-1] == ["down", "--volumes", "--remove-orphans"]
    assert main_checked == [context]
