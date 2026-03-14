"""Tests for the Pterodactyl console command allowlist engine."""

from __future__ import annotations

from agent.security.console_allowlist import (
    CommandAction,
    CommandCheck,
    ConsoleAllowlist,
    GameRules,
    _DEFAULT_RULES,
)


class TestGameDetection:
    def setup_method(self):
        self.al = ConsoleAllowlist()

    def test_detect_minecraft_java(self):
        assert self.al.detect_game_type("mc_server_minecraft") == "minecraft_java"
        assert self.al.detect_game_type("itzg/minecraft-server") == "minecraft_java"
        assert self.al.detect_game_type("paper-1.20.4") == "minecraft_java"
        assert self.al.detect_game_type("purpur-survival") == "minecraft_java"

    def test_detect_minecraft_bedrock(self):
        assert self.al.detect_game_type("bedrock-server") == "minecraft_bedrock"
        assert self.al.detect_game_type("pocketmine-mp") == "minecraft_bedrock"

    def test_detect_rust(self):
        assert self.al.detect_game_type("rust-survival-01") == "rust"
        assert self.al.detect_game_type("oxide-modded") == "rust"

    def test_detect_valheim(self):
        assert self.al.detect_game_type("valheim-dedicated") == "valheim"

    def test_detect_source(self):
        assert self.al.detect_game_type("cs2-competitive") == "source"
        assert self.al.detect_game_type("gmod-darkrp") == "source"

    def test_detect_ark(self):
        assert self.al.detect_game_type("ark-island-pve") == "ark"

    def test_detect_terraria(self):
        assert self.al.detect_game_type("terraria-vanilla") == "terraria"
        assert self.al.detect_game_type("tshock-modded") == "terraria"

    def test_detect_unknown(self):
        assert self.al.detect_game_type("some-random-container") == "unknown"


class TestMinecraftJavaCommands:
    def setup_method(self):
        self.al = ConsoleAllowlist()

    def test_safe_commands(self):
        safe_cmds = ["list", "tps", "version", "plugins", "say hello everyone"]
        for cmd in safe_cmds:
            check = self.al.check_command(cmd, game_type="minecraft_java")
            assert check.action == CommandAction.ALLOW, f"{cmd} should be safe"

    def test_approval_commands(self):
        approval_cmds = ["kick player1", "ban badguy", "op admin", "give player1 diamond 64"]
        for cmd in approval_cmds:
            check = self.al.check_command(cmd, game_type="minecraft_java")
            assert check.action == CommandAction.APPROVE, f"{cmd} should need approval"

    def test_blocked_commands(self):
        blocked_cmds = ["stop", "restart", "end", "shutdown"]
        for cmd in blocked_cmds:
            check = self.al.check_command(cmd, game_type="minecraft_java")
            assert check.action == CommandAction.DENY, f"{cmd} should be blocked"

    def test_unknown_command_needs_approval(self):
        check = self.al.check_command("some_random_mod_command", game_type="minecraft_java")
        assert check.action == CommandAction.APPROVE

    def test_save_all_needs_approval(self):
        check = self.al.check_command("save-all", game_type="minecraft_java")
        assert check.action == CommandAction.APPROVE

    def test_game_type_in_result(self):
        check = self.al.check_command("list", game_type="minecraft_java")
        assert check.game_type == "minecraft_java"


class TestRustCommands:
    def setup_method(self):
        self.al = ConsoleAllowlist()

    def test_safe_commands(self):
        safe_cmds = ["status", "serverinfo", "players", "fps"]
        for cmd in safe_cmds:
            check = self.al.check_command(cmd, game_type="rust")
            assert check.action == CommandAction.ALLOW, f"{cmd} should be safe"

    def test_blocked_commands(self):
        blocked_cmds = ["quit", "server.stop", "global.quit"]
        for cmd in blocked_cmds:
            check = self.al.check_command(cmd, game_type="rust")
            assert check.action == CommandAction.DENY, f"{cmd} should be blocked"

    def test_approval_commands(self):
        check = self.al.check_command("kick player1", game_type="rust")
        assert check.action == CommandAction.APPROVE


class TestSourceCommands:
    def setup_method(self):
        self.al = ConsoleAllowlist()

    def test_safe_commands(self):
        safe_cmds = ["status", "stats", "users", "meta list"]
        for cmd in safe_cmds:
            check = self.al.check_command(cmd, game_type="source")
            assert check.action == CommandAction.ALLOW, f"{cmd} should be safe"

    def test_blocked_commands(self):
        check = self.al.check_command("quit", game_type="source")
        assert check.action == CommandAction.DENY

    def test_map_change_needs_approval(self):
        check = self.al.check_command("changelevel de_dust2", game_type="source")
        assert check.action == CommandAction.APPROVE


class TestUnknownGameType:
    def setup_method(self):
        self.al = ConsoleAllowlist()

    def test_safe_defaults(self):
        safe_cmds = ["help", "version", "status", "list", "info"]
        for cmd in safe_cmds:
            check = self.al.check_command(cmd, game_type="unknown")
            assert check.action == CommandAction.ALLOW, f"{cmd} should be safe for unknown"

    def test_blocked_defaults(self):
        blocked_cmds = ["stop", "quit", "exit", "shutdown"]
        for cmd in blocked_cmds:
            check = self.al.check_command(cmd, game_type="unknown")
            assert check.action == CommandAction.DENY, f"{cmd} should be blocked for unknown"

    def test_everything_else_needs_approval(self):
        check = self.al.check_command("some random command", game_type="unknown")
        assert check.action == CommandAction.APPROVE


class TestAutoDetectionIntegration:
    def setup_method(self):
        self.al = ConsoleAllowlist()

    def test_detect_and_check(self):
        """Auto-detect game type from container info and check command."""
        check = self.al.check_command(
            "list",
            container_info="itzg/minecraft-server:latest",
        )
        assert check.action == CommandAction.ALLOW
        assert check.game_type == "minecraft_java"

    def test_detect_rust_and_block_quit(self):
        check = self.al.check_command(
            "quit",
            container_info="rust-survival-modded",
        )
        assert check.action == CommandAction.DENY
        assert check.game_type == "rust"

    def test_explicit_game_type_overrides_detection(self):
        """If game_type is explicitly set, don't auto-detect."""
        check = self.al.check_command(
            "status",
            game_type="rust",
            container_info="minecraft-server",  # Would detect as MC
        )
        assert check.game_type == "rust"


class TestCommandMatching:
    def setup_method(self):
        self.al = ConsoleAllowlist()

    def test_exact_match(self):
        check = self.al.check_command("list", game_type="minecraft_java")
        assert check.action == CommandAction.ALLOW

    def test_glob_match(self):
        check = self.al.check_command("say hello world", game_type="minecraft_java")
        assert check.action == CommandAction.ALLOW

    def test_case_insensitive(self):
        check = self.al.check_command("LIST", game_type="minecraft_java")
        assert check.action == CommandAction.ALLOW

    def test_no_partial_match_without_glob(self):
        """'list' should not match 'listplayers' — that's a different command."""
        # In Minecraft, 'list' is safe. But 'listplayers' doesn't match 'list'.
        check = self.al.check_command("listplayers", game_type="minecraft_java")
        # Should fall through to default (approval), not match 'list'
        assert check.action != CommandAction.ALLOW or check.matched_rule != "list"

    def test_whitespace_stripped(self):
        check = self.al.check_command("  list  ", game_type="minecraft_java")
        assert check.action == CommandAction.ALLOW


class TestCustomRules:
    def test_custom_game_rules(self):
        """Manually add custom rules and verify they work."""
        al = ConsoleAllowlist()
        al._rules["my_game"] = GameRules(
            game_type="my_game",
            detect_patterns=["*mygame*"],
            safe_patterns=["status", "info"],
            approval_patterns=["kick *"],
            blocked_patterns=["crash_server"],
        )

        assert al.detect_game_type("mygame-server-01") == "my_game"

        check = al.check_command("status", game_type="my_game")
        assert check.action == CommandAction.ALLOW

        check = al.check_command("kick player", game_type="my_game")
        assert check.action == CommandAction.APPROVE

        check = al.check_command("crash_server", game_type="my_game")
        assert check.action == CommandAction.DENY

    def test_list_game_types(self):
        al = ConsoleAllowlist()
        types = al.list_game_types()
        assert "minecraft_java" in types
        assert "rust" in types
        assert "source" in types


class TestCommandCheckDataclass:
    def test_check_fields(self):
        check = CommandCheck(
            action=CommandAction.ALLOW,
            reason="Safe command",
            game_type="minecraft_java",
            matched_rule="list",
        )
        assert check.action == CommandAction.ALLOW
        assert check.reason == "Safe command"
        assert check.game_type == "minecraft_java"
        assert check.matched_rule == "list"

    def test_check_default_matched_rule(self):
        check = CommandCheck(
            action=CommandAction.APPROVE,
            reason="Unknown",
            game_type="unknown",
        )
        assert check.matched_rule == ""
