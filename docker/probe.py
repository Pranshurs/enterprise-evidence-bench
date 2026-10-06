"""Egress probe, run in the SUT's network namespace (stdlib only). Prints one JSON line."""

import json
import socket
import sys


def tcp(host, port):
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


def dns(name):
    try:
        socket.getaddrinfo(name, 443)
        return True
    except OSError:
        return False


def default_route():
    try:
        with open("/proc/net/route") as fh:
            return any(line.split()[1] == "00000000" for line in fh.readlines()[1:])
    except OSError:
        return True  # unknown counts as present (fail closed)


gateway, db = sys.argv[1], sys.argv[2]
print(json.dumps({
    "external_ip_reachable": tcp("1.1.1.1", 443) or tcp("8.8.8.8", 53),
    "external_dns_resolves": dns("example.com"),
    "host_reachable": tcp("host.docker.internal", 22) or tcp("host.docker.internal", 55432),
    "gateway_reachable": tcp(gateway, 8080),
    "db_reachable": tcp(db, 5432),
    "default_route_present": default_route(),
}, sort_keys=True))
