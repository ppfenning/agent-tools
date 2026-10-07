import pytest

from agent_tools import route
from agent_tools.notify_core import NotifyConfig, config_from_profile


def test_the_inline_notify_mapping_parses_to_a_nested_dict():
    parsed = route.parse_profile("notify: {ntfy: https://ntfy.sh/topic}\n")
    assert parsed == {"notify": {"ntfy": "https://ntfy.sh/topic"}, "assume": "a"}


def test_the_parsed_notify_mapping_reaches_the_notifier_config():
    parsed = route.parse_profile("forge: github\nnotify: {ntfy: https://ntfy.sh/topic} # my phone\n")
    assert config_from_profile(parsed) == NotifyConfig("https://ntfy.sh/topic")


def test_a_profile_without_notify_parses_as_before():
    assert route.parse_profile("umbrella_dir: /u\n") == {"umbrella_dir": "/u", "assume": "a"}


@pytest.mark.parametrize(
    "text",
    [
        "notify:\n",
        "notify:\n  ntfy: https://ntfy.sh/topic\n",
        "notify: https://ntfy.sh/topic\n",
        "notify: {ntfy: }\n",
        "notify: {}\n",
        "notify: {other: https://ntfy.sh/topic}\n",
        "notify: {ntfy: not-a-url}\n",
        "notify: {ntfy: https://ntfy.sh/a, other: b}\n",
        "  notify: {ntfy: https://ntfy.sh/topic}\n",
        "bogus: 1\n",
        "spend:\n  bogus: 1\n",
    ],
)
def test_every_other_notify_shape_and_unknown_line_stays_rejected(text):
    with pytest.raises(route.ProfileError):
        route.parse_profile(text)
