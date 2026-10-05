from agent_tools.settings_hosts import host_rows
from agent_tools.settings_model import setting_for

TRACKED = {"cartridge.yaml": True, "profile.yaml": False}


def _host(name, capacity):
    return {"section": "lanes and machines", "scope": "host", "key": f"{name}.capacity",
            "value": capacity, "source_file": "store: hosts", "tracked": False, "pat_only": False}


def _lanes(value, tracked=True):
    return {"section": "lanes and machines", "scope": "cartridge", "key": "policy.dispatch.local_lanes",
            "value": value, "source_file": "cartridge.yaml", "tracked": tracked, "pat_only": False}


def test_hosts_sorted_then_local_lanes():
    hosts = {"mini": 2, "air": 1, "studio": 4}
    profile = {"policy": {"dispatch": {"local_lanes": "none"}}}
    assert host_rows(hosts, profile, TRACKED) == [_host("air", 1), _host("mini", 2), _host("studio", 4), _lanes("none")]


def test_host_less_table():
    profile = {"policy": {"dispatch": {"local_lanes": 0}}}
    assert host_rows({}, profile, {}) == [_lanes(0, tracked=False)]
    assert host_rows({}, {}, TRACKED) == []


def test_missing_local_lanes_key_emits_no_row():
    profile = {"policy": {"dispatch": {"max_in_flight": 3}}}
    assert host_rows({"air": 1}, profile, TRACKED) == [_host("air", 1)]


def test_local_lanes_row_resolves_in_the_registry():
    [row] = host_rows({}, {"policy": {"dispatch": {"local_lanes": "any"}}}, TRACKED)
    setting = setting_for(row["scope"], row["key"])
    assert (setting.section, setting.source_file, setting.pat_only) == (row["section"], row["source_file"], row["pat_only"])
