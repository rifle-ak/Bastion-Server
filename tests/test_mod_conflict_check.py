"""Tests for mod_conflict_check report builder and helpers."""

from __future__ import annotations

from agent.tools.base import ToolResult
from agent.tools.mod_conflict_check import (
    _build_mod_report,
    _check_minecraft_conflicts,
    _detect_game_type,
    _find_conflicts,
    _normalize_plugin_names,
    _parse_crash_log,
    _MINECRAFT_CONFLICTS,
)


def _ok(output: str = "") -> ToolResult:
    return ToolResult(output=output)


def _err() -> ToolResult:
    return ToolResult(error="fail", exit_code=1)


class TestNormalizePluginNames:
    def test_strips_jar_extension_and_version(self):
        listing = "EssentialsX-2.20.1.jar\nLuckPerms-5.4.jar\n"
        result = _normalize_plugin_names(listing, {".jar"})
        assert "essentialsx" in result
        assert "luckperms" in result

    def test_handles_ls_la_format(self):
        listing = "-rw-r--r-- 1 root root 1234 Jan 15 12:00 WorldEdit-7.2.15.jar\n"
        result = _normalize_plugin_names(listing, {".jar"})
        assert "worldedit" in result

    def test_ignores_non_matching_extensions(self):
        listing = "README.md\nconfig.yml\nPlugin.jar\n"
        result = _normalize_plugin_names(listing, {".jar"})
        assert len(result) == 1
        assert "plugin" in result

    def test_rust_cs_files(self):
        listing = "GatherRate.cs\nKits.cs\n"
        result = _normalize_plugin_names(listing, {".cs", ".dll"})
        assert "gatherrate" in result
        assert "kits" in result

    def test_empty_listing(self):
        assert _normalize_plugin_names("", {".jar"}) == []


class TestDetectGameType:
    def test_minecraft_java(self):
        assert _detect_game_type("plugins/", "[Server thread/INFO]") == "minecraft_java"

    def test_rust(self):
        assert _detect_game_type("/oxide/plugins/", "RustDedicated") == "rust"

    def test_source(self):
        assert _detect_game_type("/addons/sourcemod/", "srcds") == "source"

    def test_valheim(self):
        assert _detect_game_type("/BepInEx/plugins/", "valheim") == "valheim"

    def test_unknown(self):
        assert _detect_game_type("", "") is None


class TestFindConflicts:
    def test_detects_two_conflicting_plugins(self):
        plugins = ["essentialsx", "cmi", "vault"]
        conflicts = _find_conflicts(plugins, _MINECRAFT_CONFLICTS)
        assert any("essentialsx" in c["conflicting_plugins"] and "cmi" in c["conflicting_plugins"]
                    for c in conflicts)

    def test_no_conflicts_clean_list(self):
        plugins = ["vault", "worldguard"]
        conflicts = _find_conflicts(plugins, _MINECRAFT_CONFLICTS)
        assert len(conflicts) == 0

    def test_single_advisory_plugin(self):
        plugins = ["protocollib"]
        conflicts = _find_conflicts(plugins, _MINECRAFT_CONFLICTS)
        assert any("protocollib" in c["conflicting_plugins"] for c in conflicts)


class TestCheckMinecraftConflicts:
    def test_detects_permission_plugin_conflict(self):
        listing = "LuckPerms-5.4.jar\nPermissionsEx-1.23.jar\n"
        issues = _check_minecraft_conflicts(listing, "")
        assert any("permission" in i["description"].lower() for i in issues)

    def test_detects_duplicate_jars(self):
        listing = "EssentialsX-2.19.jar\nEssentialsX-2.20.jar\n"
        issues = _check_minecraft_conflicts(listing, "")
        assert any("Duplicate" in i["description"] for i in issues)

    def test_detects_crash_pattern_in_logs(self):
        log = "java.lang.OutOfMemoryError: Java heap space"
        issues = _check_minecraft_conflicts("", log)
        assert any("OutOfMemoryError" in i["description"] for i in issues)

    def test_clean_server(self):
        listing = "Vault-1.7.jar\nWorldGuard-7.0.jar\n"
        issues = _check_minecraft_conflicts(listing, "Normal startup complete")
        assert len(issues) == 0


class TestParseCrashLog:
    def test_detects_generic_segfault(self):
        log = "Segmentation fault (core dumped)"
        issues = _parse_crash_log(log, "unknown")
        assert len(issues) >= 1
        assert issues[0]["severity"] == "critical"

    def test_detects_oom(self):
        log = "Cannot allocate memory"
        issues = _parse_crash_log(log, "rust")
        assert any("memory" in i["description"].lower() for i in issues)

    def test_empty_log_returns_empty(self):
        assert _parse_crash_log("", "minecraft_java") == []
        assert _parse_crash_log("   ", "rust") == []

    def test_deduplicates_issues(self):
        log = "java.lang.OutOfMemoryError\njava.lang.OutOfMemoryError\n"
        issues = _parse_crash_log(log, "minecraft_java")
        oom_issues = [i for i in issues if "OutOfMemoryError" in i["description"]]
        assert len(oom_issues) == 1


class TestBuildModReport:
    def test_clean_report(self):
        report = _build_mod_report("srv", "mc_01", "minecraft_java", [], [])
        assert "MOD/PLUGIN CONFLICT REPORT" in report
        assert "Issues found: 0" in report
        assert "No conflicts or crash patterns detected" in report

    def test_report_with_conflicts_and_crashes(self):
        conflicts = [{"severity": "critical", "conflicting_plugins": "essentialsx, cmi",
                       "description": "Command overlap", "recommendation": "Remove one"}]
        crashes = [{"severity": "warning", "description": "OOM warning",
                     "recommendation": "Add RAM"}]
        report = _build_mod_report("srv", "mc_01", "minecraft_java", conflicts, crashes)
        assert "1 critical" in report
        assert "1 warnings" in report
        assert "ACTION REQUIRED" in report

    def test_report_sorts_by_severity(self):
        conflicts = [
            {"severity": "info", "description": "Info msg", "recommendation": "ok"},
        ]
        crashes = [
            {"severity": "critical", "description": "Critical crash", "recommendation": "fix"},
        ]
        report = _build_mod_report("srv", "mc_01", "minecraft_java", conflicts, crashes)
        crit_pos = report.index("CRITICAL")
        info_pos = report.index("INFO")
        assert crit_pos < info_pos

    def test_report_includes_server_and_container(self):
        report = _build_mod_report("gameserver-01", "mc_survival", "rust", [], [])
        assert "gameserver-01" in report
        assert "mc_survival" in report
        assert "rust" in report
