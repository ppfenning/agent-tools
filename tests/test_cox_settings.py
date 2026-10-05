from __future__ import annotations

from agent_tools import cox_settings
from agent_tools.settings_model import SettingRow

GROUPED = {
    "lanes and machines": [
        SettingRow("lanes and machines", "cartridge", "policy.dispatch.max_in_flight", 4, "cartridge.yaml", True),
    ],
    "spend and pacing": [],
    "models and tiers": [
        SettingRow("models and tiers", "cartridge", "policy.review_tier", "opus", "cartridge.yaml", False),
    ],
    "profile files": [
        SettingRow("profile files", "profile", "team", "pat", "profile.yaml", False),
    ],
}


def test_json_shape():
    assert cox_settings.to_json(GROUPED) == {
        "sections": [
            {"name": "lanes and machines", "rows": [{
                "section": "lanes and machines", "scope": "cartridge", "key": "policy.dispatch.max_in_flight",
                "value": 4, "source_file": "cartridge.yaml", "tracked": True, "pat_only": False,
            }]},
            {"name": "spend and pacing", "rows": []},
            {"name": "models and tiers", "rows": [{
                "section": "models and tiers", "scope": "cartridge", "key": "policy.review_tier",
                "value": "opus", "source_file": "cartridge.yaml", "tracked": False, "pat_only": True,
            }]},
            {"name": "profile files", "rows": [{
                "section": "profile files", "scope": "profile", "key": "team",
                "value": "pat", "source_file": "profile.yaml", "tracked": False, "pat_only": False,
            }]},
        ]
    }


def test_text_rendering_marks_pat_only_rows():
    assert cox_settings.render_text(GROUPED) == (
        "lanes and machines\n"
        "  cartridge:policy.dispatch.max_in_flight = 4  (cartridge.yaml, tracked)\n"
        "\n"
        "models and tiers\n"
        '  cartridge:policy.review_tier = "opus"  (cartridge.yaml, local)  [pat-only]\n'
        "\n"
        "profile files\n"
        '  profile:team = "pat"  (profile.yaml, local)'
    )
