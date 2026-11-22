"""The variable table of concept section 4.2 and the files rendered from it."""

from __future__ import annotations

from cave_ops.envfile import (
    NAMES,
    apply_updates,
    fresh_values,
    generate_secret,
    missing_names,
    parse_env,
    render,
    render_example,
)
from cave_ops.mosquitto import accepts, password_line

# The table of concept section 4.2, spelled out so that the code cannot drift from it unnoticed.
CONCEPT_VARIABLES = [
    "CAVE_DATA",
    "TZ",
    "COMPOSE_PROFILES",
    "CAVE_MQTT_USER",
    "CAVE_MQTT_PASSWORD",
    "CAVE_HA_OWNER_NAME",
    "CAVE_HA_OWNER_USER",
    "CAVE_HA_OWNER_PASSWORD",
    "CAVE_Z2M_SERIAL_PORT",
    "CAVE_Z2M_ADAPTER",
    "CAVE_Z2M_FRONTEND_TOKEN",
    "CAVE_FRITZ_HOST",
    "CAVE_FRITZ_USER",
    "CAVE_FRITZ_PASSWORD",
    "CAVE_AUTO_UPDATE",
    "CAVE_BACKUP_DIR",
    "CAVE_BACKUP_KEEP",
    "CAVE_BACKUP_COPY_TO",
    "CAVE_ALERT_URL",
]


def test_variables_are_exactly_those_of_the_concept():
    assert list(NAMES) == CONCEPT_VARIABLES


def test_apply_updates_quotes_what_compose_would_read_otherwise():
    """Issue #25: ``apply_updates`` wrote values unquoted, so a value with ``$``, `` #`` or a
    space would have been read back differently by ``parse_env`` and by Docker Compose."""
    text = "# header\nCAVE_FRITZ_PASSWORD=\nTZ=Europe/Berlin\n"
    values = {
        "CAVE_FRITZ_PASSWORD": "pa$$ word #1",
        "CAVE_ALERT_URL": "http://ntfy.fritz.box/cave?auth=a#b",
    }

    updated = apply_updates(text, values)

    assert "CAVE_FRITZ_PASSWORD='pa$$ word #1'" in updated
    assert parse_env(updated)["CAVE_FRITZ_PASSWORD"] == "pa$$ word #1"
    assert parse_env(updated)["CAVE_ALERT_URL"] == "http://ntfy.fritz.box/cave?auth=a#b"
    assert parse_env(updated)["TZ"] == "Europe/Berlin"


def test_env_example_is_the_rendered_table(repo_root):
    committed = (repo_root / ".env.example").read_text(encoding="utf-8")

    assert committed == render_example(), (
        "regenerate with: PYTHONPATH=ops python3 -m cave_ops.envfile > .env.example"
    )
    assert set(parse_env(committed)) == set(CONCEPT_VARIABLES)


def test_env_example_contains_no_secret(repo_root):
    values = parse_env((repo_root / ".env.example").read_text(encoding="utf-8"))

    for name in ("CAVE_MQTT_PASSWORD", "CAVE_HA_OWNER_PASSWORD", "CAVE_Z2M_FRONTEND_TOKEN"):
        assert values[name] == ""


def test_parse_handles_comments_quotes_and_export():
    text = "# comment\n\nA=1\nB=\"two words\"\nexport C='x'\nD=\nnot a pair\n"

    assert parse_env(text) == {"A": "1", "B": "two words", "C": "x", "D": ""}


def test_parse_drops_inline_comments_like_compose_but_not_inside_quotes():
    text = "A=value # comment\nB='x # y'\nC=\"p # q\"  # trailing\nD=no#space\nE='pa$$word'\n"

    assert parse_env(text) == {
        "A": "value",
        "B": "x # y",
        "C": "p # q",
        "D": "no#space",
        "E": "pa$$word",
    }


def test_apply_updates_changes_empty_lines_in_place_and_appends_missing_ones():
    text = "# keep me\nTZ=UTC\nCAVE_MQTT_PASSWORD=\nOTHER=1\n"

    updated = apply_updates(text, {"CAVE_MQTT_PASSWORD": "secret", "CAVE_AUTO_UPDATE": "1"})

    assert updated.startswith("# keep me\nTZ=UTC\nCAVE_MQTT_PASSWORD=secret\nOTHER=1\n")
    assert parse_env(updated)["CAVE_AUTO_UPDATE"] == "1"
    assert updated.count("CAVE_MQTT_PASSWORD") == 1


def test_missing_names_treats_empty_secrets_and_data_as_missing_but_not_optional_values():
    existing = dict.fromkeys(NAMES, "x")
    existing.update({"CAVE_MQTT_PASSWORD": "", "CAVE_FRITZ_HOST": "", "CAVE_DATA": ""})

    assert missing_names(existing) == ["CAVE_DATA", "CAVE_MQTT_PASSWORD"]
    assert missing_names({}) == list(NAMES)


def test_password_file_check_accepts_only_the_right_login():
    line = password_line("cave", "right", salt=bytes(range(12)))
    other = password_line("other", "x")

    assert accepts(other + line, "cave", "right")
    assert not accepts(line, "cave", "wrong")
    assert not accepts(line, "nobody", "right")
    assert not accepts("cave:garbage\ncave:$7$x$y$z\n", "cave", "right")
    assert not accepts("", "cave", "right")


def test_render_round_trips_through_parse():
    values = {name: f"value-{index}" for index, name in enumerate(NAMES)}

    assert parse_env(render(values)) == values


def test_generated_secrets_come_from_the_generator(tmp_path):
    counter = iter(range(100))

    values = fresh_values(tmp_path, generate=lambda: f"secret{next(counter)}")

    assert values["CAVE_DATA"] == str(tmp_path)
    assert len({values[n] for n in ("CAVE_MQTT_PASSWORD", "CAVE_HA_OWNER_PASSWORD")}) == 2
    assert len(generate_secret()) == 32
    assert generate_secret() != generate_secret()


def test_fresh_values_skips_existing(tmp_path):
    values = fresh_values(tmp_path, existing={"TZ": "UTC"}, names=["TZ", "CAVE_AUTO_UPDATE"])

    assert values == {"CAVE_AUTO_UPDATE": "1"}
