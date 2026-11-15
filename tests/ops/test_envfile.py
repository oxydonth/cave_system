"""The variable table of concept section 4.2 and the files rendered from it."""

from __future__ import annotations

from cave_ops.envfile import (
    NAMES,
    fresh_values,
    generate_secret,
    parse_env,
    render,
    render_example,
)

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
]


def test_variables_are_exactly_those_of_the_concept():
    assert list(NAMES) == CONCEPT_VARIABLES


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
