"""Prueft die Netz-Abschottung des Stapels anhand der aufgeloesten Compose-Datei.

Aufruf im Repo-Ordner (auch im CI):
  docker compose config --format json | python setup/check_networks.py

Gibt nur Dienst- und Netznamen aus, keine Umgebungswerte. Endet mit 1, wenn eine
Regel verletzt ist.
"""
import json
import sys

# Nur diese Dienste duerfen in einem Netz mit Ausgang haengen.
EGRESS_ALLOWED = {
    "rag-backend": "Websuche, Seitenabruf, Cloud",
    "searxng": "Websuche",
    "local-proxy": "veroeffentlicht SearXNG",
    "intern-proxy": "veroeffentlicht die Dienste aus 'intern'",
    "caddy": "TLS-Reverse-Proxy (Profil proxy)",
}
# Nur diese duerfen zugleich in einem internen Netz und einem mit Ausgang haengen.
BRIDGES = {"rag-backend", "intern-proxy"}
# Ihr Netz mit Ausgang darf kein anderer Dienst benutzen, sonst waeren sie eine
# Bruecke zwischen 'intern' und dem Rest.
EXCLUSIVE_EGRESS = {"intern-proxy"}
# Nur diese veroeffentlichen Ports; ausser caddy nur auf 127.0.0.1.
PUBLISHERS = {"rag-backend", "local-proxy", "intern-proxy", "caddy"}
# caddy darf Ports auch auf anderen Adressen als 127.0.0.1 veroeffentlichen,
# aber ausschliesslich 80 und 443 auf Container-Seite.
CADDY_TARGET_PORTS = {80, 443}
# Das Netz 'edge' ist ausschliesslich fuer caddy und rag-backend reserviert.
EDGE_ALLOWED = {"caddy", "rag-backend"}
# Diese haengen ausschliesslich im Netz 'intern'.
INTERN_ONLY = {"llm", "embed", "rerank", "postgres", "qdrant", "phoenix",
               "tts-service", "stt-service"}
# Optionale Dienste (Profile 'voice' und 'tracing'): fehlen sie, kein Fehler;
# sind sie aktiv, gelten die Regeln fuer INTERN_ONLY.
OPTIONAL_INTERN_ONLY = {"phoenix", "tts-service", "stt-service"}
REQUIRED_INTERNAL_NETWORKS = {"intern", "sandbox"}


def check(cfg: dict) -> list[str]:
    errors: list[str] = []
    networks = cfg.get("networks") or {}
    services = cfg.get("services") or {}

    def internal(net: str) -> bool:
        return bool((networks.get(net) or {}).get("internal"))

    for net in sorted(REQUIRED_INTERNAL_NETWORKS):
        if net not in networks:
            errors.append(f"Netz '{net}' fehlt")
        elif not internal(net):
            errors.append(f"Netz '{net}' ist nicht intern")

    members: dict[str, set[str]] = {}
    for name, svc in services.items():
        mode = svc.get("network_mode")
        if mode and mode != "none":
            errors.append(f"{name}: network_mode '{mode}' umgeht die Netze")
            continue
        nets = set(svc.get("networks") or ({"default"} if not mode else set()))
        for net in nets:
            members.setdefault(net, set()).add(name)
        egress = {n for n in nets if not internal(n)}
        if egress and name not in EGRESS_ALLOWED:
            errors.append(f"{name}: haengt in einem Netz mit Ausgang ({', '.join(sorted(egress))})")
        if egress and egress != nets and name not in BRIDGES:
            errors.append(f"{name}: verbindet interne Netze mit einem Netz mit Ausgang")
        if name in INTERN_ONLY and nets != {"intern"}:
            errors.append(f"{name}: soll nur im Netz 'intern' haengen, haengt in {sorted(nets)}")
        if name == "caddy" and nets != {"edge"}:
            errors.append(f"caddy: soll nur im Netz 'edge' haengen, haengt in {sorted(nets)}")
        for port in svc.get("ports") or []:
            if name not in PUBLISHERS:
                errors.append(f"{name}: veroeffentlicht Port {port.get('published')}")
            elif name == "caddy":
                raw_target = port.get("target", port.get("published"))
                try:
                    target_port = int(raw_target)
                except (TypeError, ValueError):
                    target_port = None
                if target_port not in CADDY_TARGET_PORTS:
                    errors.append(
                        f"caddy: veroeffentlicht unerlaubten Container-Port {raw_target} (nur 80 und 443 erlaubt)"
                    )
            elif port.get("host_ip") != "127.0.0.1":
                errors.append(f"{name}: Port {port.get('published')} nicht nur auf 127.0.0.1")

    edge_others = members.get("edge", set()) - EDGE_ALLOWED
    if edge_others:
        errors.append(f"Netz 'edge' wird von unerlaubten Diensten benutzt: {sorted(edge_others)}")

    for name in sorted((INTERN_ONLY - OPTIONAL_INTERN_ONLY) - services.keys()):
        errors.append(f"Dienst '{name}' fehlt (umbenannt? Regeln anpassen)")
    for name in sorted(EXCLUSIVE_EGRESS & services.keys()):
        nets = set(services[name].get("networks") or ())
        for net in sorted(n for n in nets if not internal(n)):
            others = members.get(net, set()) - {name}
            if others:
                errors.append(f"Netz '{net}' von {name} wird auch von {sorted(others)} benutzt")
    return errors


def main() -> int:
    cfg = json.load(sys.stdin)
    errors = check(cfg)
    for e in errors:
        print("FEHLER", e)
    if errors:
        return 1
    print(f"Netz-Abschottung: {len(cfg.get('services') or {})} Dienste geprueft, keine Abweichung.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
