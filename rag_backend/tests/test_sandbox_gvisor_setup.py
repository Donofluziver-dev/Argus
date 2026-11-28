from rag_backend.tests.test_install import installer  # noqa: F401


def test_gvisor_file_is_added_after_base_and_gpu_files(installer):
    env = {"COMPOSE_FILE": "docker-compose.yml:compose/gpu-suche.yml:docker-compose.bundle.yml",
           "COMPOSE_PATH_SEPARATOR": ":"}
    assert installer.compose_file_with_gvisor(env, True) == (
        "docker-compose.yml:compose/gpu-suche.yml:compose/sandbox-gvisor.yml:docker-compose.bundle.yml")
    assert installer.compose_file_with_gvisor({}, True) == "docker-compose.yml:compose/sandbox-gvisor.yml"
    with_gvisor = {"COMPOSE_FILE": "docker-compose.yml:compose/sandbox-gvisor.yml"}
    assert installer.compose_file_with_gvisor(with_gvisor, True) is None
    assert installer.compose_file_with_gvisor(with_gvisor, False) == "docker-compose.yml"
    assert installer.compose_file_with_gvisor({}, False) is None


def test_gpu_selection_keeps_gvisor_file(installer):
    env = {"COMPOSE_FILE": "docker-compose.yml:compose/sandbox-gvisor.yml"}
    value = installer.compose_file_value(env, ["suche"])
    assert "compose/sandbox-gvisor.yml" in value.split(":")


def test_runtimes_override(installer, monkeypatch):
    monkeypatch.setenv("ARGUS_DOCKER_RUNTIMES", "runc, runsc")
    assert installer.docker_runtimes() == {"runc", "runsc"}


def test_doctor_sandbox_runtime_states(installer):
    use = {"COMPOSE_FILE": "docker-compose.yml:compose/sandbox-gvisor.yml"}
    assert installer.doctor_check_sandbox_runtime(use, {"runc"}, []).status == "FEHLER"
    assert installer.doctor_check_sandbox_runtime(use, {"runc", "runsc"}, []).status == "OK"
    assert installer.doctor_check_sandbox_runtime(use, {"runc", "runsc"}, ["runc", "runsc"]).status == "WARNUNG"
    assert installer.doctor_check_sandbox_runtime({}, {"runc", "runsc"}, []).status == "WARNUNG"
    assert installer.doctor_check_sandbox_runtime({}, {"runc"}, []).status == "OK"
