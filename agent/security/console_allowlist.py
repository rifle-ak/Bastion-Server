"""Pterodactyl console command allowlist engine.

Separate from the shell command allowlist — game console commands are
a completely different security domain. A Minecraft ``/say`` is safe;
``/stop`` is destructive. A Rust ``status`` is read-only; ``quit`` kills
the server.

The engine:
1. Auto-detects the game type from the server egg/image/name
2. Applies game-specific rules (safe, needs-approval, blocked)
3. Falls back to conservative defaults for unknown games
4. Evolves: loads custom rules from config/console_commands.yaml
   so operators can add new games or override defaults without
   touching code.

This keeps your security posture tight even as you add new game types,
migrate from Pterodactyl to Pelican, or onboard weird modded servers.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import structlog
import yaml

logger = structlog.get_logger()


class CommandAction(str, Enum):
    """What to do with a console command."""
    ALLOW = "allow"           # Execute without approval
    APPROVE = "approve"       # Needs operator approval
    DENY = "deny"             # Never execute


@dataclass(frozen=True)
class CommandCheck:
    """Result of checking a console command against the allowlist."""
    action: CommandAction
    reason: str
    game_type: str
    matched_rule: str = ""


@dataclass
class GameRules:
    """Rules for a specific game type's console commands."""
    game_type: str
    # Patterns that are always safe (read-only, informational)
    safe_patterns: list[str] = field(default_factory=list)
    # Patterns that need operator approval (destructive but sometimes needed)
    approval_patterns: list[str] = field(default_factory=list)
    # Patterns that are NEVER allowed (catastrophic, no legitimate use via agent)
    blocked_patterns: list[str] = field(default_factory=list)
    # Detection patterns — how to identify this game type from container info
    detect_patterns: list[str] = field(default_factory=list)


# ── Built-in game rules (sensible defaults) ──
# These evolve: operators can override in config/console_commands.yaml

_BUILTIN_RULES: dict[str, GameRules] = {
    "minecraft_java": GameRules(
        game_type="minecraft_java",
        detect_patterns=[
            "*minecraft*", "*paper*", "*spigot*", "*bukkit*",
            "*purpur*", "*fabric*", "*forge*", "*velocity*",
            "*waterfall*", "*bungeecord*", "*sponge*",
            "*itzg/minecraft*",
        ],
        safe_patterns=[
            "say *",            # Broadcast message
            "list",             # List online players
            "tps",              # Server TPS (Spigot/Paper)
            "mspt",             # Milliseconds per tick (Paper)
            "timings *",        # Performance timings
            "spark *",          # Spark profiler
            "version",          # Server version
            "plugins",          # List plugins
            "pl",               # Alias for plugins
            "help",             # Command help
            "help *",
            "status",           # Server status
            "gc",               # Garbage collection info
            "mem",              # Memory info
            "tps",
            "seed",             # World seed
            "difficulty",       # Current difficulty
            "gamerule *",       # View game rules (read-only when no value)
            "scoreboard *",     # View scoreboards
            "trigger *",        # Trigger objectives
            "data get *",       # Read NBT data
            "execute * run say *",  # Say via execute
        ],
        approval_patterns=[
            "kick *",           # Kick a player
            "ban *",            # Ban a player
            "ban-ip *",
            "pardon *",
            "pardon-ip *",
            "whitelist *",      # Whitelist management
            "op *",             # Give operator
            "deop *",           # Remove operator
            "gamemode *",       # Change gamemode
            "give *",           # Give items
            "tp *",             # Teleport
            "teleport *",
            "kill *",           # Kill entities
            "weather *",        # Change weather
            "time set *",       # Change time
            "difficulty *",     # Change difficulty (with arg)
            "gamerule * *",     # Change game rules (with value)
            "setblock *",       # Place blocks
            "fill *",           # Fill blocks
            "clone *",          # Clone blocks
            "summon *",         # Summon entities
            "effect *",         # Apply effects
            "enchant *",        # Enchant items
            "xp *",             # Give XP
            "clear *",          # Clear inventory
            "title *",          # Display titles
            "tellraw *",        # Raw JSON messages
            "save-all",         # Save the world
            "save-on",          # Enable saving
            "save-off",         # Disable saving
            "reload",           # Reload plugins/datapacks
            "reload *",
        ],
        blocked_patterns=[
            "stop",             # Shutdown the server — use pterodactyl_power
            "restart",          # Restart — use pterodactyl_power
            "end",
            "shutdown",
        ],
    ),
    "minecraft_bedrock": GameRules(
        game_type="minecraft_bedrock",
        detect_patterns=[
            "*bedrock*", "*bds*", "*pocketmine*", "*nukkit*",
        ],
        safe_patterns=[
            "say *", "list", "help", "help *", "version",
            "gamerule",  # View rules
            "seed",
        ],
        approval_patterns=[
            "kick *", "ban *", "whitelist *", "op *", "deop *",
            "gamemode *", "give *", "tp *", "kill *", "effect *",
            "summon *", "weather *", "time set *", "gamerule * *",
            "save hold", "save resume", "save query",
            "reload",
        ],
        blocked_patterns=["stop", "shutdown"],
    ),
    "rust": GameRules(
        game_type="rust",
        detect_patterns=[
            "*rust*", "*oxide*", "*umod*", "*carbon*",
        ],
        safe_patterns=[
            "status",            # Server status + player count
            "serverinfo",        # Server info
            "players",           # Player list
            "fps",               # Server FPS
            "perf *",            # Performance stats
            "entity.count",      # Entity count
            "world.size",        # World size
            "oxide.version",     # Oxide version
            "version",           # Game version
            "find *",            # Find commands
            "oxide.plugins",     # List plugins
            "plugins",           # Plugin list
            "o.plugins",
        ],
        approval_patterns=[
            "kick *",
            "ban *", "banid *", "unban *",
            "say *",             # Server message
            "inventory.give *",  # Give items
            "teleport.topos *",  # Teleport
            "teleportpos *",
            "server.save",       # Force save
            "oxide.reload *",    # Reload plugins
            "o.reload *",
            "oxide.load *",
            "oxide.unload *",
            "server.writecfg",   # Write config
            "env.time *",        # Set time
            "weather.*",         # Weather control
        ],
        blocked_patterns=[
            "quit",              # Server shutdown
            "server.stop",
            "global.quit",
            "restart *",
            "server.restart",
        ],
    ),
    "valheim": GameRules(
        game_type="valheim",
        detect_patterns=[
            "*valheim*", "*valheimplus*",
        ],
        safe_patterns=[
            "info", "ping", "lodbias *", "help",
        ],
        approval_patterns=[
            "kick *", "ban *", "unban *", "banned",
            "save",
        ],
        blocked_patterns=["shutdown", "quit", "exit"],
    ),
    "source": GameRules(
        game_type="source",
        detect_patterns=[
            "*cs2*", "*csgo*", "*gmod*", "*garrysmod*",
            "*tf2*", "*l4d*", "*srcds*",
        ],
        safe_patterns=[
            "status",            # Server status
            "stats",             # Performance stats
            "users",             # User list
            "maps",              # Map list
            "cvarlist *",        # List convars
            "version",           # Version info
            "sm version",        # SourceMod version
            "sm plugins *",      # List SourceMod plugins
            "meta list",         # MetaMod plugins
            "net_status",        # Network status
            "mem_dump",          # Memory dump
        ],
        approval_patterns=[
            "sm_kick *",         # Kick via SourceMod
            "sm_ban *",          # Ban via SourceMod
            "sm_map *",          # Change map
            "changelevel *",     # Change level
            "map *",             # Change map
            "say *",             # Chat message
            "sm_rcon *",         # SourceMod RCON
            "sv_password *",     # Set password
            "exec *",           # Execute config file
            "mp_restartgame *",  # Restart round
            "bot_add *",         # Add bots
            "bot_kick *",        # Remove bots
        ],
        blocked_patterns=["quit", "exit", "_restart", "shutdown"],
    ),
    "ark": GameRules(
        game_type="ark",
        detect_patterns=[
            "*ark*", "*arkse*", "*survival*evolved*",
        ],
        safe_patterns=[
            "listplayers", "getchat", "getgamelog",
            "ServerChatToPlayer *", "ServerChatTo *",
        ],
        approval_patterns=[
            "broadcast *",
            "KickPlayer *", "BanPlayer *", "UnbanPlayer *",
            "SaveWorld", "SetTimeOfDay *",
            "GiveItemToPlayer *", "AddExperience *",
            "SetPlayerPos *",
            "DestroyWildDinos",
        ],
        blocked_patterns=[
            "DoExit", "shutdown", "quit",
        ],
    ),
    "terraria": GameRules(
        game_type="terraria",
        detect_patterns=[
            "*terraria*", "*tshock*", "*tmodloader*",
        ],
        safe_patterns=[
            "playing", "who", "help", "version",
            "motd", "rules", "seed", "world",
        ],
        approval_patterns=[
            "say *", "kick *", "ban *", "unban *",
            "give *", "tp *", "spawn *", "godmode",
            "time *", "wind *", "rain *", "settitle *",
            "save", "reload",
        ],
        blocked_patterns=["exit", "off", "stop"],
    ),
}

# Default rules for unknown game types — conservative
_DEFAULT_RULES = GameRules(
    game_type="unknown",
    safe_patterns=[
        "help", "help *", "version", "status", "list", "info",
    ],
    approval_patterns=["*"],  # Everything else needs approval
    blocked_patterns=[
        "stop", "quit", "exit", "shutdown", "end",
        "restart", "reboot", "halt",
    ],
)


class ConsoleAllowlist:
    """Game-aware console command allowlist.

    Auto-detects game type and applies appropriate rules.
    Custom rules from config/console_commands.yaml override
    built-in defaults, so the system evolves with your setup.
    """

    def __init__(self, config_dir: str | Path | None = None) -> None:
        self._rules: dict[str, GameRules] = dict(_BUILTIN_RULES)
        self._custom_loaded = False

        # Load custom rules if config dir provided
        if config_dir:
            self._load_custom_rules(Path(config_dir))

    def _load_custom_rules(self, config_dir: Path) -> None:
        """Load custom rules from console_commands.yaml."""
        rules_file = config_dir / "console_commands.yaml"
        if not rules_file.exists():
            return

        try:
            with open(rules_file) as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            logger.warning("console_rules_load_error", path=str(rules_file), error=str(e))
            return

        games = data.get("games", {})
        for game_type, rules_data in games.items():
            rules = GameRules(
                game_type=game_type,
                detect_patterns=rules_data.get("detect_patterns", []),
                safe_patterns=rules_data.get("safe_commands", []),
                approval_patterns=rules_data.get("approval_commands", []),
                blocked_patterns=rules_data.get("blocked_commands", []),
            )
            self._rules[game_type] = rules
            logger.info("custom_console_rules_loaded", game_type=game_type)

        self._custom_loaded = True

    def detect_game_type(self, container_info: str) -> str:
        """Detect game type from container name, image, or metadata.

        Args:
            container_info: Container name, image name, or other identifying info.

        Returns:
            Detected game type string, or 'unknown'.
        """
        info_lower = container_info.lower()
        for game_type, rules in self._rules.items():
            for pattern in rules.detect_patterns:
                if fnmatch.fnmatch(info_lower, pattern):
                    return game_type
        return "unknown"

    def check_command(
        self,
        command: str,
        game_type: str = "unknown",
        container_info: str = "",
    ) -> CommandCheck:
        """Check a console command against the allowlist.

        Args:
            command: The game console command to check.
            game_type: Game type if known. Auto-detected from container_info if empty.
            container_info: Container name/image for auto-detection.

        Returns:
            CommandCheck with action, reason, and matched rule.
        """
        # Auto-detect if not specified
        if game_type == "unknown" and container_info:
            game_type = self.detect_game_type(container_info)

        rules = self._rules.get(game_type, _DEFAULT_RULES)
        cmd_stripped = command.strip()
        cmd_lower = cmd_stripped.lower()

        # 1. Check blocked first (highest priority)
        for pattern in rules.blocked_patterns:
            if self._matches(cmd_lower, pattern.lower()):
                logger.warning(
                    "console_command_blocked",
                    command=command,
                    game_type=game_type,
                    pattern=pattern,
                )
                return CommandCheck(
                    action=CommandAction.DENY,
                    reason=f"Blocked for {game_type}: use pterodactyl_power instead",
                    game_type=game_type,
                    matched_rule=pattern,
                )

        # 2. Check safe patterns (allow without approval)
        for pattern in rules.safe_patterns:
            if self._matches(cmd_lower, pattern.lower()):
                return CommandCheck(
                    action=CommandAction.ALLOW,
                    reason=f"Safe {game_type} command",
                    game_type=game_type,
                    matched_rule=pattern,
                )

        # 3. Check approval patterns
        for pattern in rules.approval_patterns:
            if self._matches(cmd_lower, pattern.lower()):
                return CommandCheck(
                    action=CommandAction.APPROVE,
                    reason=f"Needs approval for {game_type}",
                    game_type=game_type,
                    matched_rule=pattern,
                )

        # 4. Default: require approval for unknown commands
        return CommandCheck(
            action=CommandAction.APPROVE,
            reason=f"Unknown command for {game_type} — requires approval",
            game_type=game_type,
        )

    def get_rules(self, game_type: str) -> GameRules:
        """Get rules for a game type."""
        return self._rules.get(game_type, _DEFAULT_RULES)

    def list_game_types(self) -> list[str]:
        """List all known game types."""
        return sorted(self._rules.keys())

    @staticmethod
    def _matches(command: str, pattern: str) -> bool:
        """Check if a command matches a pattern.

        Supports:
        - Exact match: "stop" matches "stop"
        - Glob: "say *" matches "say hello world"
        - Bare command: "list" matches "list" but not "listplayers"
        """
        # Exact match
        if command == pattern:
            return True
        # Glob match
        if "*" in pattern:
            return fnmatch.fnmatch(command, pattern)
        # For patterns without *, only match as a standalone command
        # (not as a prefix of another command)
        return False


# Singleton instance — lazy-loaded
_instance: ConsoleAllowlist | None = None


def get_console_allowlist(config_dir: str | Path | None = None) -> ConsoleAllowlist:
    """Get or create the global ConsoleAllowlist instance."""
    global _instance
    if _instance is None:
        config_path = config_dir or os.environ.get("BASTION_AGENT_CONFIG", "./config")
        _instance = ConsoleAllowlist(config_path)
    return _instance


def reset_console_allowlist() -> None:
    """Reset the singleton (for testing)."""
    global _instance
    _instance = None
