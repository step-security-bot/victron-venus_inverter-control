"""Battery telemetry absence is neither zero charge nor permission to actuate."""

import time
from unittest.mock import Mock

import pytest

from inverter_control.console_ui import ConsoleUI
from inverter_control.victron import (
    BATTERY_CHAIN_1,
    SHUNT_READ_MAX_AGE,
    VictronDBus,
)
from inverter_control.victron_parse import calculate_battery_soc_from_voltage


@pytest.fixture
def device(monkeypatch):
    monkeypatch.setattr(VictronDBus, "_discover_services", lambda self: None)
    value = VictronDBus(test_mode=True)
    value._shunt_service = "com.victronenergy.battery.shunt"
    value._system_data = {"_last_update": time.time(), "bv": 52, "bc": 0, "bp": 0}
    monkeypatch.setattr(value, "_safe_subprocess", Mock(return_value=None))
    return value


@pytest.mark.parametrize("voltage", [None, 0, -1, float("nan"), float("inf"), "bad"])
def test_invalid_voltage_is_unknown_not_empty_battery(voltage):
    assert calculate_battery_soc_from_voltage(voltage) is None


def test_valid_empty_battery_and_zero_current_remain_real_numbers(device):
    device._system_data["bv"] = 40
    data = device.get_system_data()
    assert data["battery_data"]["available"]
    assert data["bc"] == data["bp"] == 0
    assert device.get_battery_soc_local(data) == 0


def test_missing_shunt_cannot_reuse_previous_identity_values(device):
    device._shunt_service = None
    data = device.get_system_data()
    assert [data[key] for key in ("bv", "bc", "bp")] == [None] * 3
    assert data["battery_data"]["reason"] == "missing_source"
    assert device.get_battery_soc_local(data) is None


@pytest.mark.parametrize("raw", [None, "nan", "0", "bad"])
def test_invalid_voltage_signal_immediately_clears_last_good(device, raw):
    device._apply_fast_value(device._shunt_service, "/Dc/0/Voltage", raw)
    data = device.get_system_data()
    assert data["bv"] is None
    assert device.get_battery_soc_local(data) is None


def test_disconnect_signal_clears_all_values(device):
    device._apply_fast_value(device._shunt_service, "/Connected", "0")
    data = device.get_system_data()
    assert [data[key] for key in ("bv", "bc", "bp")] == [None] * 3
    assert data["battery_data"]["reason"] == "disconnected"


def test_local_read_age_expires_without_claiming_physical_sample_age(device, monkeypatch):
    now = [100.0]
    monkeypatch.setattr("inverter_control.victron.time.monotonic", lambda: now[0])
    device._accept_shunt_data({"bv": 52, "bc": 0, "bp": 0})
    data = device.get_system_data()
    assert data["battery_data"]["read_age_seconds"] == 0
    assert data["battery_data"]["sample_age_seconds"] is None
    now[0] += SHUNT_READ_MAX_AGE + 1
    data = device.get_system_data()
    assert data["battery_data"]["reason"] == "stale_read"
    assert [data[key] for key in ("bv", "bc", "bp")] == [None] * 3


def test_battery_source_frozen_timestamp_expires_and_recovers(device, monkeypatch):
    now = [100.0]
    monkeypatch.setattr("inverter_control.victron.time.monotonic", lambda: now[0])
    monkeypatch.setattr(device, "_dbus_get", lambda *_: "1")
    metadata = {
        "/Info/DataComplete": "1",
        "/Info/LastMeasurementMonotonic": "100",
        "/Info/DataTimeout": "60",
    }
    monkeypatch.setattr(device, "_dbus_get_native_only", lambda _, path: metadata.get(path))
    assert device._battery_source_status(BATTERY_CHAIN_1)["available"]
    now[0] = 160
    assert device._battery_source_status(BATTERY_CHAIN_1)["reason"] == "stale_sample"
    metadata["/Info/LastMeasurementMonotonic"] = "160"
    assert device._battery_source_status(BATTERY_CHAIN_1)["available"]
    metadata["/Info/DataComplete"] = "0"
    assert device._battery_source_status(BATTERY_CHAIN_1)["reason"] == "incomplete"


def test_virtual_readiness_without_timestamp_and_real_zero_soc(device, monkeypatch):
    monkeypatch.setattr(
        device,
        "_dbus_get_native_only",
        lambda _, path: "1" if path == "/Info/DataComplete" else None,
    )
    monkeypatch.setattr(device, "_dbus_get", lambda _, path: "1" if path == "/Connected" else "0")
    assert device._query_battery_chain_socs() == [0.0, 0.0]
    monkeypatch.setattr(device, "_dbus_get", lambda *_: None)
    assert device._query_battery_chain_socs() == [None, None]
    device._reconcile_all_batteries()
    for battery in device.get_all_batteries():
        assert not battery["available"]
        assert all(battery[key] is None for key in ("voltage", "current", "power", "soc"))


def test_console_handles_unknown_and_real_zero_separately(device):
    device.get_inverter_state = Mock(return_value=(9, "Inverting"))
    ui = ConsoleUI(Mock(), device)
    unknown = ui._fmt_battery_section(
        {"bv": None, "bc": None, "bp": None, "battery_socs": [None, 0]}
    )
    assert "—W" in unknown and "—%,0%" in unknown
    zero = ui._fmt_battery_section({"bv": 40, "bc": 0, "bp": 0, "battery_socs": [0, 0]})
    assert "0W,0%,0%,0%" in zero
