"""Tests for backup audit report builder and helper functions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent.tools.backup_audit import (
    _build_backup_report,
    _format_backup_section,
    _parse_backup_date,
    _parse_ls_output,
    _parse_size_bytes,
)


def _ok(output: str) -> dict:
    return {"output": output, "error": "", "exit_code": 0}


def _err(msg: str = "command failed") -> dict:
    return {"output": "", "error": msg, "exit_code": 1}


# ---------------------------------------------------------------------------
# _parse_size_bytes
# ---------------------------------------------------------------------------

class TestParseSizeBytes:
    def test_gigabytes(self):
        assert _parse_size_bytes("1.5G") == int(1.5 * 1024**3)

    def test_megabytes(self):
        assert _parse_size_bytes("200M") == 200 * 1024**2

    def test_kilobytes(self):
        assert _parse_size_bytes("512K") == 512 * 1024

    def test_plain_integer(self):
        assert _parse_size_bytes("4096") == 4096

    def test_unknown_returns_none(self):
        assert _parse_size_bytes("unknown") is None

    def test_empty_returns_none(self):
        assert _parse_size_bytes("") is None

    def test_garbage_returns_none(self):
        assert _parse_size_bytes("not-a-size") is None

    def test_terabytes(self):
        assert _parse_size_bytes("2T") == 2 * 1024**4


# ---------------------------------------------------------------------------
# _parse_backup_date
# ---------------------------------------------------------------------------

class TestParseBackupDate:
    def test_iso_date_time(self):
        dt = _parse_backup_date("2026-03-10 14:30")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 3
        assert dt.day == 10

    def test_iso_date_only(self):
        dt = _parse_backup_date("2026-01-15")
        assert dt is not None
        assert dt.month == 1

    def test_ls_date_format(self):
        dt = _parse_backup_date("Mar 10 14:30")
        assert dt is not None
        assert dt.month == 3
        assert dt.day == 10

    def test_empty_returns_none(self):
        assert _parse_backup_date("") is None

    def test_garbage_returns_none(self):
        assert _parse_backup_date("not-a-date") is None


# ---------------------------------------------------------------------------
# _parse_ls_output
# ---------------------------------------------------------------------------

class TestParseLsOutput:
    def test_typical_ls_output(self):
        raw = (
            "total 1.2G\n"
            "-rw-r--r-- 1 root root 500M Mar 10 14:30 backup-2026-03-10.tar.gz\n"
            "-rw-r--r-- 1 root root 450M Mar  9 02:00 backup-2026-03-09.tar.gz\n"
        )
        entries = _parse_ls_output(raw)
        assert len(entries) == 2
        assert entries[0]["name"] == "backup-2026-03-10.tar.gz"
        assert entries[0]["size"] == "500M"
        assert "Mar" in entries[0]["date"]

    def test_empty_output(self):
        assert _parse_ls_output("") == []

    def test_total_only(self):
        assert _parse_ls_output("total 0") == []

    def test_short_lines_skipped(self):
        raw = "drwxr-xr-x 2 root root\n"
        assert _parse_ls_output(raw) == []


# ---------------------------------------------------------------------------
# _format_backup_section
# ---------------------------------------------------------------------------

class TestFormatBackupSection:
    def test_no_backups_found_critical_warning(self):
        lines: list[str] = []
        recommendations: list[str] = []
        _format_backup_section(lines, recommendations, {"files": []}, "cpanel")
        assert any("No backup files found" in l for l in lines)
        assert any("CRITICAL" in r and "cpanel" in r for r in recommendations)

    def test_not_found_directory(self):
        lines: list[str] = []
        recommendations: list[str] = []
        _format_backup_section(lines, recommendations, {"not_found": True}, "pterodactyl")
        assert any("No pterodactyl backup directory found" in l for l in lines)
        assert recommendations == []

    def test_error_produces_critical(self):
        lines: list[str] = []
        recommendations: list[str] = []
        _format_backup_section(
            lines, recommendations, {"error": "permission denied"}, "mysql"
        )
        assert any("permission denied" in l for l in lines)
        assert any("CRITICAL" in r and "mysql" in r for r in recommendations)

    def test_recent_backups_no_warnings(self):
        now = datetime.now(timezone.utc)
        recent = now - timedelta(hours=2)
        date_str = recent.strftime("%Y-%m-%d %H:%M")
        section = {
            "files": [
                {"name": "backup.tar.gz", "size": "500M", "date": date_str},
            ]
        }
        lines: list[str] = []
        recommendations: list[str] = []
        _format_backup_section(lines, recommendations, section, "cpanel")
        assert any("Found 1 backup(s)" in l for l in lines)
        assert recommendations == []

    def test_stale_backup_warn_threshold(self):
        now = datetime.now(timezone.utc)
        old = now - timedelta(hours=30)
        date_str = old.strftime("%Y-%m-%d %H:%M")
        section = {
            "files": [
                {"name": "old-backup.tar.gz", "size": "1G", "date": date_str},
            ]
        }
        lines: list[str] = []
        recommendations: list[str] = []
        _format_backup_section(lines, recommendations, section, "cpanel")
        assert any("WARN" in r and "30h" in r for r in recommendations)
        assert not any("CRITICAL" in r and "old" in r.lower() for r in recommendations)

    def test_stale_backup_critical_threshold(self):
        now = datetime.now(timezone.utc)
        very_old = now - timedelta(hours=72)
        date_str = very_old.strftime("%Y-%m-%d %H:%M")
        section = {
            "files": [
                {"name": "ancient-backup.tar.gz", "size": "2G", "date": date_str},
            ]
        }
        lines: list[str] = []
        recommendations: list[str] = []
        _format_backup_section(lines, recommendations, section, "pterodactyl")
        assert any("CRITICAL" in r and "72h" in r for r in recommendations)

    def test_suspiciously_small_backup(self):
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y-%m-%d %H:%M")
        section = {
            "files": [
                {"name": "tiny.tar.gz", "size": "512", "date": date_str},
            ]
        }
        lines: list[str] = []
        recommendations: list[str] = []
        _format_backup_section(lines, recommendations, section, "mysql")
        assert any("suspiciously small" in r.lower() for r in recommendations)

    def test_backup_sizes_shown_in_report(self):
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y-%m-%d %H:%M")
        section = {
            "files": [
                {"name": "backup1.tar.gz", "size": "1.2G", "date": date_str},
                {"name": "backup2.tar.gz", "size": "900M", "date": date_str},
            ]
        }
        lines: list[str] = []
        recommendations: list[str] = []
        _format_backup_section(lines, recommendations, section, "cpanel")
        text = "\n".join(lines)
        assert "size=1.2G" in text
        assert "size=900M" in text


# ---------------------------------------------------------------------------
# _build_backup_report
# ---------------------------------------------------------------------------

class TestBuildBackupReport:
    def test_empty_data_healthy(self):
        report = _build_backup_report("web01", {})
        assert "BACKUP AUDIT: web01" in report
        assert "No issues detected" in report

    def test_storage_section_rendered(self):
        data = {
            "storage": (
                "Filesystem      Size  Used Avail Use% Mounted on\n"
                "/dev/sda1        50G   20G   28G  42% /\n"
            ),
        }
        report = _build_backup_report("web01", data)
        assert "STORAGE" in report
        assert "42%" in report
        # 42% is healthy, no recommendations
        assert "CRITICAL" not in report
        assert "No issues detected" in report

    def test_storage_high_usage_critical(self):
        data = {
            "storage": (
                "Filesystem      Size  Used Avail Use% Mounted on\n"
                "/dev/sda1        50G   46G   2G   93% /backup\n"
            ),
        }
        report = _build_backup_report("web01", data)
        assert "CRITICAL" in report
        assert "93%" in report

    def test_storage_moderate_usage_warn(self):
        data = {
            "storage": (
                "Filesystem      Size  Used Avail Use% Mounted on\n"
                "/dev/sda1        50G   42G   6G   85% /backup\n"
            ),
        }
        report = _build_backup_report("web01", data)
        assert "WARN" in report
        assert "85%" in report

    def test_cpanel_section_present(self):
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y-%m-%d %H:%M")
        data = {
            "cpanel": {
                "files": [
                    {"name": "/backup/cpbackup/daily/user.tar.gz", "size": "2G", "date": date_str},
                ]
            },
        }
        report = _build_backup_report("web01", data)
        assert "CPANEL BACKUPS" in report
        assert "cpbackup" in report

    def test_pterodactyl_section_present(self):
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y-%m-%d %H:%M")
        data = {
            "pterodactyl": {
                "files": [
                    {"name": "/srv/pterodactyl/backups/server1.tar.gz", "size": "5G", "date": date_str},
                ]
            },
        }
        report = _build_backup_report("game01", data)
        assert "PTERODACTYL" in report
        assert "pterodactyl/backups" in report

    def test_mysql_section_with_binlog(self):
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y-%m-%d %H:%M")
        data = {
            "mysql": {
                "files": [
                    {"name": "/var/backups/all-databases.sql.gz", "size": "300M", "date": date_str},
                ],
                "binlog": "log_bin\tON",
            },
        }
        report = _build_backup_report("db01", data)
        assert "MYSQL" in report
        assert "log_bin" in report

    def test_no_backups_critical_recommendation(self):
        data = {
            "cpanel": {"files": []},
        }
        report = _build_backup_report("web01", data)
        assert "CRITICAL" in report
        assert "No cpanel backup files found" in report

    def test_integrity_checks_rendered(self):
        data = {
            "integrity": [
                {"file": "/backup/test.tar.gz", "valid": True},
                {"file": "/backup/bad.tar.gz", "valid": False, "error": "corrupt archive"},
            ],
        }
        report = _build_backup_report("web01", data)
        assert "INTEGRITY CHECKS" in report
        assert "[OK]" in report
        assert "[FAILED]" in report
        assert "corrupt archive" in report

    def test_cpanel_config_rendered(self):
        data = {
            "cpanel_config": "BACKUPTYPE=compressed\nBACKUPDAYS=1,2,3",
        }
        report = _build_backup_report("web01", data)
        assert "CPANEL BACKUP CONFIG" in report
        assert "BACKUPTYPE=compressed" in report

    def test_jetbackup_installed(self):
        data = {
            "jetbackup": {"installed": True, "details": "config.yaml\nlicense.dat"},
        }
        report = _build_backup_report("web01", data)
        assert "JETBACKUP" in report
        assert "installed" in report
        assert "config.yaml" in report

    def test_jetbackup_not_installed(self):
        data = {
            "jetbackup": {"installed": False},
        }
        report = _build_backup_report("web01", data)
        assert "not installed" in report

    def test_full_report_multiple_sections(self):
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y-%m-%d %H:%M")
        data = {
            "storage": "Filesystem Size Used Avail Use% Mounted on\n/dev/sda1 50G 20G 28G 42% /\n",
            "cpanel": {
                "files": [
                    {"name": "/backup/daily/user.tar.gz", "size": "2G", "date": date_str},
                ]
            },
            "pterodactyl": {
                "files": [
                    {"name": "/srv/pterodactyl/backups/s1.tar.gz", "size": "5G", "date": date_str},
                ]
            },
            "mysql": {
                "files": [
                    {"name": "/var/backups/db.sql.gz", "size": "300M", "date": date_str},
                ],
                "binlog": "log_bin\tON",
            },
        }
        report = _build_backup_report("prod01", data)
        assert "BACKUP AUDIT: prod01" in report
        assert "STORAGE" in report
        assert "CPANEL BACKUPS" in report
        assert "PTERODACTYL" in report
        assert "MYSQL" in report
        assert "No issues detected" in report
