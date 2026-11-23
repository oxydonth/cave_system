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
        ("eclipse-mosquitto:2.0.23", {"eclipse-mosquitto:2.0.23"}),
        ("docker.io/library/eclipse-mosquitto:2.0.23", {"eclipse-mosquitto:2.0.23"}),
        ("docker.io/koenkk/zigbee2mqtt", {"koenkk/zigbee2mqtt:latest"}),
        ("localhost:5000/cave/sim", {"localhost:5000/cave/sim:latest"}),
        ("ghcr.io/a/b:1@sha256:abc", {"ghcr.io/a/b:1", "ghcr.io/a/b@sha256:abc"}),
        ("ghcr.io/a/b@sha256:abc", {"ghcr.io/a/b@sha256:abc"}),
    ],
)
def test_references_are_named_as_docker_lists_them(reference, normalised):
    assert _images.normalise(reference) == normalised


def test_a_removal_cut_off_half_way_is_finished_by_the_next_update(server, run_cave, store, killed):
    """Killed between two `docker image rm`: what is gone was not needed, what is kept is all
    there, and the next update removes the rest."""
    _obsolete(server, run_cave, store)
    real_handle = store.handle

    def cut_after_one(args):
        if args[2] == "rm" and store.removed:
            raise killed
        return real_handle(args)

    store.handle = cut_after_one
    with pytest.raises(killed):
        run_cave("update")
    store.handle = real_handle
    assert len(store.removed) == 1
    assert images_of(RELEASES[1]) | images_of(RELEASES[2]) <= set(store.store)

    run_cave("update")

    assert project_images(store) == images_of(RELEASES[1]) | images_of(RELEASES[2])


# ------------------------------------------------------ the first review of #30: what is kept
def test_a_digest_pin_keeps_the_previous_release_s_image(server, run_cave, store):
    """The reviewer's test. The previous release names its broker by tag and digest; the image
    is also tagged locally. `docker image ls --digests` lists it as `name tag digest`."""
    install(server, run_cave, RELEASES[0])
    digest = "sha256:" + "a" * 64
    pinned = "eclipse-mosquitto:2.0.99"

    def compose(ha, broker):
        return (
            "name: cave\nservices:\n"
            f"  homeassistant:\n    image: {ha}\n"
            f"  mosquitto:\n    image: {broker}\n"
        )

    server.stack.after["pull"] = lambda release: None
    store.store[pinned] = digest
    store.store["ghcr.io/home-assistant/home-assistant:2026.12.1"] = "sha256:" + "b" * 64
    server.release(
        RELEASES[1],
        files={
            "compose.yaml": compose(
                "ghcr.io/home-assistant/home-assistant:2026.12.1", f"{pinned}@{digest}"
            )
        },
    )
    assert run_cave("update").code == 0
    store.store["ghcr.io/home-assistant/home-assistant:2026.12.2"] = "sha256:" + "c" * 64
    store.store["eclipse-mosquitto:2.0.100"] = "sha256:" + "d" * 64
    server.release(
        RELEASES[2],
        files={
            "compose.yaml": compose(
                "ghcr.io/home-assistant/home-assistant:2026.12.2", "eclipse-mosquitto:2.0.100"
            )
        },
    )
    assert run_cave("update").code == 0
    assert server.record()["previous"]["release"].startswith(RELEASES[1])

    assert pinned in store.store, f"the previous release's image was removed: {store.removed}"


def test_a_way_back_that_was_not_clean_removes_no_image_of_the_release_before(
    server, run_cave, store
):
    """The reviewer's test. The update fails to start the new release; the way back cannot check
    out the previous release, starts whatever is checked out (the new one) and ends `broken`
    (exit 3), the update no longer in flight. The release the owner is told to go back to must
    keep its images."""
    install(server, run_cave, RELEASES[0])
    install(server, run_cave, RELEASES[1])
    publish(server, RELEASES[2])
    server.stack.fail_times["up"] = 1  # the switch's start fails once; the way back's works
    checkouts = []

    def fail_the_way_back(argv):
        checkouts.append(argv)
        if len(checkouts) >= 2:
            server.runner.git_fail.setdefault("checkout", []).append((1, "error: unable"))
            server.runner.git_fail.setdefault("reset", []).append((1, "error: unable"))

    server.runner.git_before["checkout"] = fail_the_way_back

    result = run_cave("update")
    server.runner.git_before.clear()

    assert result.code == 3
    assert server.record()["switch"] is None
    assert images_of(RELEASES[1]) <= set(store.store), (
        f"the release the owner was on lost its images: removed {store.removed}"
    )
    # And not on the nights after either, until an update has ended well again.
    assert run_cave("update").code == 0  # nothing newer: up to date
    assert images_of(RELEASES[1]) <= set(store.store)


def test_nothing_is_removed_while_the_last_update_ended_broken(server, run_cave, store):
    """Exit status 3 leaves maintenance to the owner: no image goes, not even one no kept release
    names, until an update ends otherwise."""
    install(server, run_cave, RELEASES[0])
    install(server, run_cave, RELEASES[1])
    publish(server, RELEASES[2])
    server.stack.fail_up.update({RELEASES[2]})
    server.stack.after["up"] = lambda release: server.stack.unhealthy.add(RELEASES[1])
    assert run_cave("update").code == 3  # the previous release came back, but not healthy
    server.stack.after.clear()
    server.stack.after["pull"] = lambda release: store.add(*images_of(release))
    server.stack.unhealthy.clear()
    store.add(*images_of("v2026.11.13.1"))  # obsolete: no kept release names it

    result = run_cave("update")  # nothing newer that has not failed: up to date

    assert result.code == 0
    assert images_of("v2026.11.13.1") <= set(store.store)
    assert "the last update ended with exit status 3" in result.out


def test_the_release_a_way_back_by_hand_returns_to_keeps_its_images_later_too(
    server, run_cave, store
):
    """After a way back that was not clean, the next update that ends otherwise (here a newer
    release whose download fails) removes images again, and still keeps those of the release the
    last switch started from: the owner may not have put it back yet."""
    install(server, run_cave, RELEASES[0])
    install(server, run_cave, RELEASES[1])
    publish(server, RELEASES[2])
    server.stack.fail_times["up"] = 1
    checkouts = []

    def fail_the_way_back(argv):
        checkouts.append(argv)
        if len(checkouts) >= 2:
            server.runner.git_fail.setdefault("checkout", []).append((1, "error: unable"))
            server.runner.git_fail.setdefault("reset", []).append((1, "error: unable"))

    server.runner.git_before["checkout"] = fail_the_way_back
    assert run_cave("update").code == 3
    server.runner.git_before.clear()
    publish(server, RELEASES[3])
    server.stack.fail["pull"] = 1

    assert run_cave("update").code == 1  # not updated: the download failed

    assert server.record()["last_result"]["outcome"] == "pull-failed"
    assert images_of(RELEASES[1]) <= set(store.store), (
        f"the release the owner was on lost its images: removed {store.removed}"
    )


@pytest.mark.parametrize(
    ("in_store", "named"),
    [
        # Pulled by its digest only: Docker lists it with the tag <none>.
        ("eclipse-mosquitto@sha256:" + "e" * 64, "eclipse-mosquitto:2.0.99@sha256:" + "e" * 64),
        # Pulled by its tag before; the release names it by digest alone.
        ("eclipse-mosquitto:2.0.99", "eclipse-mosquitto@sha256:" + "e" * 64),
    ],
)
def test_an_image_named_by_digest_is_kept_by_its_digest(server, run_cave, store, in_store, named):
    install(server, run_cave, RELEASES[0])
    store.store[in_store] = "sha256:" + "e" * 64
    server.stack.after["pull"] = lambda release: None
    server.release(
        RELEASES[1],
        files={"compose.yaml": f"name: cave\nservices:\n  mosquitto:\n    image: {named}\n"},
    )

    assert run_cave("update").code == 0

    assert in_store in store.store, f"removed: {store.removed}"


def test_the_recovery_removes_no_image(server, run_cave, store, killed):
    """`--recover` runs at boot and after every nightly update; it finishes what is unfinished and
    nothing else, so it never removes an image, not even an obsolete one."""
    _obsolete(server, run_cave, store)
    asked = len(server.runner.fake.matching("docker", "image"))

    assert run_cave("update", "--recover").code == 0

    assert store.removed == []
    assert len(server.runner.fake.matching("docker", "image")) == asked
