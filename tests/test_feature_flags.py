"""
Tests for the feature-flag resolver that drives the phased ABAP → Python cutover.

Every flag must default to its legacy mode; env vars and config dicts can
enable shadow / validate_only / python globally or for a targeted cohort.
"""

import logging

from python_target.feature_flags import (
    INVENTORY_REPORT,
    ORDER_SYNC,
    VENDOR_LOOKUP,
    FieldDiff,
    FlagDecision,
    InventoryReportFlagMode,
    OrderSyncFlagMode,
    RoutedResult,
    ShadowDiff,
    VendorLookupFlagMode,
    emit_flag_event,
    resolve_flag,
)


class TestResolveFlag:

    def test_resolve_flag_defaults_to_legacy_mode(self):
        assert resolve_flag(INVENTORY_REPORT, env={}).mode == InventoryReportFlagMode.ABAP
        assert resolve_flag(VENDOR_LOOKUP, env={}).mode == VendorLookupFlagMode.ABAP
        assert resolve_flag(ORDER_SYNC, env={}).mode == OrderSyncFlagMode.IDOC_ABAP
        assert resolve_flag(ORDER_SYNC, env={}).reason == "default"

    def test_resolve_flag_reads_os_environ_by_default(self, monkeypatch):
        monkeypatch.setenv("FF_VENDOR_LOOKUP_MODE", "shadow")
        assert resolve_flag(VENDOR_LOOKUP).mode == VendorLookupFlagMode.SHADOW

    def test_resolve_flag_env_global_mode(self):
        env = {"FF_ORDER_SYNC_MODE": "VALIDATE_ONLY"}
        decision = resolve_flag(ORDER_SYNC, {"docnum": "0000000001"}, env=env)
        assert decision.mode == OrderSyncFlagMode.VALIDATE_ONLY
        assert decision.reason == "env:default"

    def test_resolve_flag_env_cohort_targets_only_matching_bukrs(self):
        env = {"FF_VENDOR_LOOKUP_MODE": "python", "FF_VENDOR_LOOKUP_BUKRS": "1000, 2000"}
        assert resolve_flag(VENDOR_LOOKUP, {"bukrs": "2000"}, env=env).mode == VendorLookupFlagMode.PYTHON
        outside = resolve_flag(VENDOR_LOOKUP, {"bukrs": "3000"}, env=env)
        assert outside.mode == VendorLookupFlagMode.ABAP
        assert outside.reason == "env:no_match"

    def test_resolve_flag_config_takes_precedence_over_env(self):
        env = {"FF_INVENTORY_REPORT_MODE": "python"}
        config = {INVENTORY_REPORT: {"default": "shadow"}}
        decision = resolve_flag(INVENTORY_REPORT, config=config, env=env)
        assert decision.mode == InventoryReportFlagMode.SHADOW
        assert decision.reason == "config:default"

    def test_resolve_flag_config_first_matching_rule_wins(self):
        config = {
            VENDOR_LOOKUP: {
                "rules": [
                    {"mode": "python", "match": {"bukrs": ["1000"], "lifnr": ["0000001000"]}},
                    {"mode": "shadow", "match": {"bukrs": ["1000"]}},
                ]
            }
        }
        canary = resolve_flag(VENDOR_LOOKUP, {"bukrs": "1000", "lifnr": "0000001000"}, config=config, env={})
        same_bukrs = resolve_flag(VENDOR_LOOKUP, {"bukrs": "1000", "lifnr": "0000002000"}, config=config, env={})
        assert (canary.mode, canary.reason) == (VendorLookupFlagMode.PYTHON, "config:rule[0]")
        assert (same_bukrs.mode, same_bukrs.reason) == (VendorLookupFlagMode.SHADOW, "config:rule[1]")

    def test_resolve_flag_list_attribute_requires_all_values_in_cohort(self):
        config = {INVENTORY_REPORT: {"rules": [{"mode": "python", "match": {"werks": ["1000", "2000"]}}]}}
        assert resolve_flag(INVENTORY_REPORT, {"werks": ["1000", "2000"]}, config=config).mode == InventoryReportFlagMode.PYTHON
        assert resolve_flag(INVENTORY_REPORT, {"werks": ["1000", "3000"]}, config=config).mode == InventoryReportFlagMode.ABAP
        assert resolve_flag(INVENTORY_REPORT, {}, config=config).mode == InventoryReportFlagMode.ABAP

    def test_resolve_flag_invalid_mode_falls_back_to_legacy(self, caplog):
        with caplog.at_level(logging.WARNING, logger="python_target.feature_flags"):
            decision = resolve_flag(ORDER_SYNC, env={"FF_ORDER_SYNC_MODE": "shadow"})
        assert decision.mode == OrderSyncFlagMode.IDOC_ABAP
        assert "Invalid mode" in caplog.text


class TestEmitFlagEvent:

    def test_emit_flag_event_logs_mode_and_diff(self, caplog):
        decision = FlagDecision(VENDOR_LOOKUP, VendorLookupFlagMode.SHADOW, "env:default", {"bukrs": "1000"})
        diff = ShadowDiff([FieldDiff("RETURN", "return_code", 0, 1)])
        with caplog.at_level(logging.INFO, logger="python_target.feature_flags"):
            emit_flag_event(RoutedResult(None, decision, "abap", diff))

        event = caplog.records[-1].flag_event
        assert caplog.records[-1].levelno == logging.WARNING
        assert event["flag"] == VENDOR_LOOKUP
        assert event["mode"] == "shadow"
        assert event["diff"]["clean"] is False
        assert event["diff"]["difference_count"] == 1
        assert '"event": "feature_flag.route"' in caplog.records[-1].getMessage()
