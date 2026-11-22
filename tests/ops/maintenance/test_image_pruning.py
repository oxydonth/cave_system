"""``cave update`` removes the images of releases that are no longer needed (issue #25, WP11).

Every release here names its own Home Assistant and broker images in ``compose.yaml``; pulling a
release puts them into the fake image store (``server.runner.images``). Only the images of the
running and the previous release may remain among this project's images; an unrelated image is
never touched; and nothing is removed while another command holds the lock, an update is
interrupted, a restore is unfinished, or the stack is not running.
"""

from __future__ import annotations

import json

import pytest

from cave_ops.commands import _images
from cave_ops.lock import Lock

OLD = "v2026.11.14.1"
RELEASES = ["v2026.11.15.1", "v2026.11.16.1", "v2026.11.17.1", "v2026.11.18.1"]
UNRELATED = ("hello-world:latest", "postgres:16")


def images_of(tag: str) -> set[str]:
    day = tag.split(".")[2]
    return {f"ghcr.io/home-assistant/home-assistant:2026.11.{day}", f"eclipse-mosquitto:2.0.{day}"}


def compose_of(tag: str) -> str:
    ha, broker = sorted(images_of(tag), reverse=True)
    return (
        "name: cave\n"
        "services:\n"
        "  homeassistant:\n"
        f"    image: {ha}  # pinned\n"
        "  mosquitto:\n"
        f'    image: "{broker}"\n'
    )


@pytest.fixture
def store(server):
    images = server.runner.images
    images.add(*UNRELATED)

    def pulled(release):
        images.add(*images_of(release))

    server.stack.after["pull"] = pulled
    return images


def publish(server, tag):
    server.release(tag, files={"compose.yaml": compose_of(tag)})


def project_images(store) -> set[str]:
    return {name for name in store.store if name not in UNRELATED}


def install(server, run_cave, tag):
    publish(server, tag)
    result = run_cave("update")
    assert result.code == 0, result.err
    assert server.stack.running == tag
    return result


def test_after_a_series_of_releases_only_the_running_and_the_previous_remain(
    server, run_cave, store
):
    for tag in RELEASES:
        install(server, run_cave, tag)

    assert project_images(store) == images_of(RELEASES[-1]) | images_of(RELEASES[-2])
    assert set(UNRELATED) <= set(store.store), "an unrelated image was touched"
    assert set(store.removed) == images_of(RELEASES[0]) | images_of(RELEASES[1])


def test_an_image_of_the_project_pulled_by_hand_is_removed_and_one_in_use_is_kept(
    server, run_cave, store
):
    install(server, run_cave, RELEASES[0])
    store.add("eclipse-mosquitto:1.6", "ghcr.io/home-assistant/home-assistant:2025.1.0")
    store.in_use.add("ghcr.io/home-assistant/home-assistant:2025.1.0")

    result = install(server, run_cave, RELEASES[1])

    assert "eclipse-mosquitto:1.6" in store.removed
    assert "ghcr.io/home-assistant/home-assistant:2025.1.0" in store.store
    assert "Docker kept these images (a container uses them)" in result.out


def test_the_images_the_local_override_names_are_kept(server, run_cave, store):
    install(server, run_cave, RELEASES[0])
    store.add("ghcr.io/home-assistant/home-assistant:2026.12.0b1")
    (server.root / "compose.override.yaml").write_text(
        "services:\n  homeassistant:\n"
        "    image: ghcr.io/home-assistant/home-assistant:2026.12.0b1\n"
    )

    install(server, run_cave, RELEASES[1])
    install(server, run_cave, RELEASES[2])

    assert "ghcr.io/home-assistant/home-assistant:2026.12.0b1" in store.store


def test_a_failed_release_s_images_go_once_an_update_ended_well(server, run_cave, store):
    install(server, run_cave, RELEASES[0])
    publish(server, RELEASES[1])
    server.stack.unhealthy.add(RELEASES[1])
    assert run_cave("update").code == 2  # rolled back to RELEASES[0]

    assert project_images(store) == images_of(RELEASES[0])
    assert set(store.removed) == images_of(RELEASES[1])


# ------------------------------------------------------------- when nothing may be removed
def _obsolete(server, run_cave, store):
    """Three releases installed; images of the first put back, so there is something to remove."""
    for tag in RELEASES[:3]:
        install(server, run_cave, tag)
    store.add(*images_of(RELEASES[0]))
    store.removed.clear()


def test_nothing_is_removed_while_the_stack_is_stopped(server, run_cave, store):
    _obsolete(server, run_cave, store)
    server.stack.running = None

    result = run_cave("update")

    assert store.removed == []
    assert images_of(RELEASES[0]) <= set(store.store)
    assert "Old images were not removed: the stack is not running and healthy" in result.out


def test_nothing_is_removed_while_another_command_holds_the_lock(server, run_cave, store, ctx):
    _obsolete(server, run_cave, store)
    asked = len(server.runner.fake.matching("docker", "image"))

    with Lock(ctx.paths.lock_file, "backup"):
        assert run_cave("update").code == 1

    assert store.removed == []
    assert len(server.runner.fake.matching("docker", "image")) == asked, "Docker was asked"


def test_nothing_is_removed_while_a_restore_is_unfinished(server, run_cave, store):
    _obsolete(server, run_cave, store)
    marker = server.data / ".cave" / "ops" / "restore.json"
    marker.write_text(
        json.dumps({"archive": "a.tar.gz", "aside": "before-restore-x", "original_entries": []})
    )

    result = run_cave("update")

    assert result.code == 1
    assert store.removed == []
    assert "Old images were not removed: a restore is unfinished" in result.out


def test_nothing_is_removed_while_an_update_is_interrupted(server, run_cave, store):
    _obsolete(server, run_cave, store)
    publish(server, RELEASES[3])
    server.stack.fail_up.add(RELEASES[3])
    server.stack.fail_up.add(RELEASES[2])  # the way back cannot start either: still in flight

    result = run_cave("update")

    assert result.code == 3
    assert server.record()["switch"] is not None
    assert store.removed == []
    assert "Old images were not removed: an update is interrupted" in result.out


def test_nothing_is_removed_without_a_record_of_the_previous_release(server, run_cave, store):
    _obsolete(server, run_cave, store)
    record = server.record()
    record.pop("previous")
    (server.data / ".cave/ops/update.json").write_text(json.dumps(record))

    result = run_cave("update")

    assert store.removed == []
    assert "the previous release is not recorded yet" in result.out


def test_a_variable_in_an_image_line_removes_nothing(server, run_cave, store):
    install(server, run_cave, RELEASES[0])
    server.release(
        RELEASES[1],
        files={"compose.yaml": "services:\n  x:\n    image: ${CAVE_IMAGE:-busybox}\n"},
    )
    server.stack.after["pull"] = lambda release: None
    store.add(*images_of("v2026.11.13.1"))

    result = run_cave("update")

    assert store.removed == []
    assert "names a variable" in result.out


@pytest.mark.parametrize(
    ("reference", "normalised"),
    [
        ("eclipse-mosquitto:2.0.23", "eclipse-mosquitto:2.0.23"),
        ("docker.io/library/eclipse-mosquitto:2.0.23", "eclipse-mosquitto:2.0.23"),
        ("docker.io/koenkk/zigbee2mqtt", "koenkk/zigbee2mqtt:latest"),
        ("localhost:5000/cave/sim", "localhost:5000/cave/sim:latest"),
        ("ghcr.io/a/b:1@sha256:abc", "ghcr.io/a/b@sha256:abc"),
    ],
)
def test_references_are_named_as_docker_lists_them(reference, normalised):
    assert _images.normalise(reference) == normalised
