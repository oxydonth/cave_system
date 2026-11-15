"""The version check of concept section 4.5."""

from __future__ import annotations

from cave_ops.versioncheck import check, check_repository, locked_versions

LOCK = """
version = 1
[[package]]
name = "homeassistant"
version = "2026.10.4"

[[package]]
name = "pytest"
version = "9.0.0"
"""
COMPOSE = (
    "services:\n  homeassistant:\n    image: ghcr.io/home-assistant/home-assistant:2026.10.4\n"
)
CI = "  HASSFEST_IMAGE: ghcr.io/home-assistant/hassfest:2026.10.4@sha256:abc\n"


def manifest(**requirements):
    return lambda integration, library: requirements.get(library)


def lock_with(**extra):
    return LOCK + "".join(
        f'\n[[package]]\nname = "{name}"\nversion = "{version}"\n'
        for name, version in extra.items()
    )


def test_all_places_agree():
    assert check(LOCK, COMPOSE, CI) == []


def test_locked_versions_reads_package_tables():
    assert locked_versions(LOCK) == {"homeassistant": "2026.10.4", "pytest": "9.0.0"}


def test_compose_tag_that_differs_is_reported():
    problems = check(LOCK, COMPOSE.replace("2026.10.4", "2026.11.1"), CI)

    assert len(problems) == 1
    assert "compose.yaml image tag is 2026.11.1" in problems[0]
    assert "2026.10.4" in problems[0]


def test_hassfest_tag_that_differs_is_reported():
    problems = check(LOCK, COMPOSE, CI.replace("2026.10.4", "2026.9.0"))

    assert len(problems) == 1
    assert "hassfest" in problems[0]


def test_digest_after_the_tag_is_ignored():
    assert check(LOCK, COMPOSE.rstrip() + "@sha256:abc\n", CI) == []


def test_missing_image_or_lock_entry_is_reported():
    assert "not found" in check(LOCK, "services: {}\n", CI)[0]
    assert "not locked" in check("version = 1\n", COMPOSE, CI)[0]


def test_client_libraries_are_ignored_while_they_are_not_locked():
    assert check(LOCK, COMPOSE, CI, manifest(**{"paho-mqtt": "2.1.0"})) == []


def test_client_libraries_must_match_what_home_assistant_asks_for():
    lock = lock_with(**{"paho-mqtt": "2.1.0", "pyfritzhome": "0.6.21"})

    assert (
        check(lock, COMPOSE, CI, manifest(**{"paho-mqtt": "2.1.0", "pyfritzhome": "0.6.21"})) == []
    )

    problems = check(lock, COMPOSE, CI, manifest(**{"paho-mqtt": "2.2.0", "pyfritzhome": "0.6.21"}))
    assert len(problems) == 1
    assert "paho-mqtt is locked at 2.1.0" in problems[0]
    assert "mqtt integration asks for exactly 2.2.0" in problems[0]

    problems = check(lock, COMPOSE, CI, manifest(**{"paho-mqtt": "2.1.0"}))
    assert len(problems) == 1
    assert "pyfritzhome" in problems[0]
    assert "cannot be read" in problems[0]


def test_the_repository_agrees_with_itself(repo_root):
    # The three places of the Home Assistant version; the client libraries depend on what is
    # installed, which `make versions` covers.
    problems = [
        p for p in check_repository(repo_root) if "paho" not in p and "pyfritzhome" not in p
    ]

    assert problems == []
