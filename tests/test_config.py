import pytest

from paiper.config import PanelProfile, load_profile


def test_profile_defaults_to_unconfirmed_clockwise_compensation():
    profile = PanelProfile()
    assert profile.rotation == 270
    assert profile.confirmed is False
    assert profile.native_size == (400, 600)


def test_no_profile_path_uses_unconfirmed_defaults():
    assert load_profile(None) == PanelProfile()


@pytest.mark.parametrize("rotation", [90, 270])
@pytest.mark.parametrize("confirmed", [True, False])
def test_profile_accepts_supported_values(rotation, confirmed):
    profile = PanelProfile(rotation=rotation, confirmed=confirmed)
    assert profile.rotation == rotation
    assert profile.confirmed is confirmed
    assert profile.native_size == (400, 600)


@pytest.mark.parametrize("rotation", [True, False, 0, 180, -90, 450, 90.0, "270", None])
def test_profile_rejects_invalid_rotation(rotation):
    with pytest.raises(ValueError):
        PanelProfile(rotation=rotation)


@pytest.mark.parametrize("confirmed", [0, 1, "true", "false", None, [], {}])
def test_profile_rejects_non_boolean_confirmation(confirmed):
    with pytest.raises(ValueError):
        PanelProfile(confirmed=confirmed)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("panel: {}", PanelProfile()),
        ("panel:\n  rotation: 90", PanelProfile(rotation=90)),
        ("panel:\n  confirmed: true", PanelProfile(confirmed=True)),
        ("panel:\n  rotation: 270\n  confirmed: false", PanelProfile()),
        (
            "panel:\n  rotation: 90\n  confirmed: true",
            PanelProfile(rotation=90, confirmed=True),
        ),
    ],
)
def test_load_profile_reads_valid_mapping(tmp_path, text, expected):
    path = tmp_path / "panel.yaml"
    path.write_text(text, encoding="utf-8")
    assert load_profile(path) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "null",
        "[]",
        "- rotation: 90",
        "270",
        "true",
        "profile",
        "{}",
        "rotation: 90",
        "confirmed: true",
        "rotation: 270\nconfirmed: false",
        "panel:",
        "panel: null",
        "panel: []",
        "panel: 270",
        "panel: true",
        "panel: profile",
        "panel:\n  - rotation: 90",
        "panel:\n  rotation: [",
        "panel: {}\n---\npanel: {}",
        "unexpected: true",
        "native_size: [600, 400]",
        "1: true",
        "panel: {}\nunexpected: true",
        "panel: {}\nrotation: 90",
        "panel: {}\n1: true",
        "panel:\n  unexpected: true",
        "panel:\n  native_size: [600, 400]",
        "panel:\n  rotation: 90\n  unexpected: true",
        "panel:\n  1: true",
        "panel:\n  rotation: true",
        "panel:\n  rotation: false",
        "panel:\n  rotation: 180",
        "panel:\n  rotation: 90.0",
        'panel:\n  rotation: "270"',
        "panel:\n  rotation: null",
        "panel:\n  rotation: []",
        "panel:\n  rotation: {}",
        "panel:\n  confirmed: 1",
        'panel:\n  confirmed: "false"',
        "panel:\n  confirmed: null",
        "panel:\n  confirmed: []",
        "panel:\n  confirmed: {}",
    ],
)
def test_load_profile_rejects_invalid_yaml_or_values(tmp_path, text):
    path = tmp_path / "panel.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_profile(path)
