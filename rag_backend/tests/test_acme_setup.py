import os
import socket

import pytest

from rag_backend.tests.test_install import installer  # noqa: F401

LE = "https://acme-v02.api.letsencrypt.org/directory"
LE_STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"


def _ugid() -> dict[str, str]:
    if not hasattr(os, "getuid"):
        return {}
    return {"ARGUS_UID": str(os.getuid()), "ARGUS_GID": str(os.getgid())}


def _resolver(addrs):
    def resolve(host, port):
        if addrs is None:
            raise socket.gaierror("nicht aufloesbar")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0)) for a in addrs]
    return resolve


def _acme_env(**extra):
    env = {
        "COMPOSE_PROFILES": "proxy",
        "ARGUS_DOMAIN": "argus.example.org",
        "ARGUS_TLS_MODE": "acme",
        "ARGUS_TRUSTED_PROXIES": "172.31.250.0/29",
        "TRUSTED_HOSTS": "argus.example.org,127.0.0.1,localhost,rag-backend",
        "DASHBOARD_ORIGINS": "https://argus.example.org",
        **_ugid(),
    }
    env.update(extra)
    return env


# ---- .env-Werte -------------------------------------------------------------

def test_acme_writes_email_and_lets_encrypt_ca(installer):
    env = installer.compute_server_env("https://argus.example.org", use_proxy=True, tls_mode="acme",
                                       acme_email="it@example.org", acme_ca="staging")
    assert env["ARGUS_TLS_MODE"] == "acme"
    assert env["ARGUS_ACME_EMAIL"] == "it@example.org"
    assert env["ARGUS_ACME_CA"] == LE_STAGING
    prod = installer.compute_server_env("https://argus.example.org", use_proxy=True, tls_mode="acme")
    assert prod["ARGUS_ACME_CA"] == LE
    assert "ARGUS_ACME_EMAIL" not in prod


def test_acme_keeps_previous_lets_encrypt_choice(installer):
    env = installer.compute_server_env("https://argus.example.org", use_proxy=True, tls_mode="acme",
                                       existing_env={"ARGUS_ACME_CA": LE_STAGING})
    assert env["ARGUS_ACME_CA"] == LE_STAGING


def test_acme_rejects_foreign_ca_and_bad_email(installer):
    with pytest.raises(ValueError):
        installer.compute_server_env("https://argus.example.org", use_proxy=True, tls_mode="acme",
                                     acme_ca="https://ca.firma.example/acme/directory")
    with pytest.raises(ValueError):
        installer.compute_server_env("https://argus.example.org", use_proxy=True, tls_mode="acme",
                                     acme_email="keine-adresse")


def test_acme_intern_needs_own_ca(installer):
    with pytest.raises(ValueError):
        installer.compute_server_env("https://ki.firma.local", use_proxy=True, tls_mode="acme-intern")
    with pytest.raises(ValueError):
        installer.compute_server_env("https://ki.firma.local", use_proxy=True, tls_mode="acme-intern",
                                     acme_ca=LE)
    env = installer.compute_server_env("https://ki.firma.local", use_proxy=True, tls_mode="acme-intern",
                                       acme_ca="https://ca.firma.local/acme/acme/directory")
    assert env["ARGUS_ACME_CA"] == "https://ca.firma.local/acme/acme/directory"


@pytest.mark.parametrize("name,public", [
    ("argus.example.org", True), ("argus.westeurope.cloudapp.azure.com", True),
    ("localhost", False), ("argus", False), ("10.1.2.3", False), ("ki.firma.local", False),
    ("ki.firma.lan", False), ("ki.corp", False), ("x.internal", False),
])
def test_public_dns_name(installer, name, public):
    assert installer.is_public_dns_name(name) is public


# ---- doctor -----------------------------------------------------------------

def test_doctor_acme_ok_with_public_name(installer, tmp_path):
    env = _acme_env(ARGUS_ACME_CA=LE)
    errors, warnings = installer.doctor_acme_checks(env, "argus.example.org", tmp_path, 1000, 1000,
                                                    "acme", resolver=_resolver(["20.50.1.2"]))
    assert errors == [] and warnings == []


def test_doctor_acme_rejects_internal_name_and_closed_ports(installer, tmp_path):
    env = _acme_env(ARGUS_PROXY_HTTP_PORT="8080", ARGUS_PROXY_HTTPS_PORT="8443",
                    ARGUS_PROXY_BIND="127.0.0.1")
    errors, _ = installer.doctor_acme_checks(env, "ki.firma.local", tmp_path, 1000, 1000, "acme",
                                             resolver=_resolver(["10.0.0.5"]))
    text = " ".join(errors)
    assert "Port 80 oder 443" in text
    assert "ARGUS_PROXY_BIND" in text
    assert "kein oeffentlicher DNS-Name" in text


def test_doctor_acme_warnings(installer, tmp_path):
    env = _acme_env(ARGUS_ACME_CA=LE_STAGING, ARGUS_PROXY_HTTPS_PORT="8443")
    errors, warnings = installer.doctor_acme_checks(env, "argus.example.org", tmp_path, 1000, 1000,
                                                    "acme", resolver=_resolver(["10.0.0.5"]))
    text = " ".join(warnings)
    assert errors == []
    assert "Staging" in text and "HTTP-01" in text and "interne Adressen" in text
    _, w_dns = installer.doctor_acme_checks(_acme_env(), "argus.example.org", tmp_path, 1000, 1000,
                                            "acme", resolver=_resolver(None))
    assert any("loest" in w for w in w_dns)


def test_doctor_acme_intern_needs_ca_and_root(installer, tmp_path):
    env = _acme_env(ARGUS_TLS_MODE="acme-intern")
    uid = os.getuid() if hasattr(os, "getuid") else 1000
    gid = os.getgid() if hasattr(os, "getgid") else 1000
    errors, _ = installer.doctor_acme_checks(env, "ki.firma.local", tmp_path, uid, gid, "acme-intern")
    text = " ".join(errors)
    assert "ARGUS_ACME_CA" in text and "acme-root.pem" in text
    (tmp_path / "acme-root.pem").write_text("ROOT", encoding="utf-8")
    env["ARGUS_ACME_CA"] = "https://ca.firma.local/acme/acme/directory"
    errors, warnings = installer.doctor_acme_checks(env, "ki.firma.local", tmp_path, uid, gid, "acme-intern")
    assert errors == [] and warnings == []


def test_doctor_check_proxy_reports_acme_problems(installer, tmp_path):
    repo = tmp_path / "repo"
    (repo / "proxy" / "certs").mkdir(parents=True)
    (repo / "proxy" / "data").mkdir(parents=True)
    env = _acme_env(ARGUS_DOMAIN="argus.local", TRUSTED_HOSTS="argus.local,127.0.0.1",
                    DASHBOARD_ORIGINS="https://argus.local")
    r = installer.doctor_check_proxy(repo, env)
    assert r.status == "FEHLER" and "oeffentlicher DNS-Name" in r.message
