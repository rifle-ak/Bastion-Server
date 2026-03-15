"""Game server plugin/mod conflict detection and crash analysis.

Detects conflicting plugins, incompatible mod combinations, and parses
crash logs for Minecraft (Java/Bedrock), Rust (Oxide/uMod), Source
engine games (CS2, Garry's Mod), Valheim, ARK, and Terraria.

Commands are built programmatically and run via _run_on_server — no raw
shell strings from the model.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from agent.inventory import Inventory
from agent.tools.base import BaseTool, ToolResult
from agent.tools.docker_tools import _run_on_server

# ---------------------------------------------------------------------------
# Known conflict definitions
# ---------------------------------------------------------------------------

# Each entry: (set of plugin name patterns, severity, description, recommendation)
# Plugin names are matched case-insensitively against filenames (without .jar/.lua etc.)

_MINECRAFT_CONFLICTS: list[tuple[set[str], str, str, str]] = [
    (
        {"essentialsx", "essentials", "cmi"},
        "critical",
        "EssentialsX and CMI both register overlapping commands (/home, /warp, /tpa, etc.). "
        "Running both causes command conflicts and unpredictable behavior.",
        "Choose one: EssentialsX (free, community-maintained) or CMI (paid, all-in-one). "
        "Remove the other and migrate configurations.",
    ),
    (
        {"worldedit", "fastasyncworldedit"},
        "warning",
        "WorldEdit and FastAsyncWorldEdit (FAWE) both provide world editing. "
        "FAWE replaces WorldEdit — having both loaded causes class conflicts.",
        "Remove the standalone WorldEdit JAR. FAWE bundles its own WorldEdit internally.",
    ),
    (
        {"luckperms", "permissionsex", "groupmanager", "bpermissions", "ultraperms"},
        "critical",
        "Multiple permission plugins detected. Only one permission backend should manage "
        "groups and permissions — running multiple causes authorization failures.",
        "Keep LuckPerms (actively maintained, best performance). Remove all others and "
        "import existing permissions via '/lp import'.",
    ),
    (
        {"vault", "essentialseco", "economy", "iconomy", "fe-economy"},
        "warning",
        "Multiple economy providers detected alongside Vault. Vault is a bridge, not an "
        "economy itself — you need exactly one economy plugin registered with Vault.",
        "Keep Vault plus ONE economy plugin (EssentialsX Eco or a dedicated economy plugin). "
        "Remove duplicates.",
    ),
    (
        {"viaversion", "viabackwards"},
        "info",
        "ViaVersion and ViaBackwards detected. These must be version-matched — mismatched "
        "versions cause packet errors and client disconnects.",
        "Ensure both are from the same release. Update together, never independently.",
    ),
    (
        {"protocollib"},
        "info",
        "ProtocolLib detected. Many plugins depend on it — version mismatches between "
        "ProtocolLib and dependent plugins cause AbstractMethodError crashes.",
        "Keep ProtocolLib updated to the latest build matching your server version.",
    ),
    (
        {"citizens", "denizen"},
        "info",
        "Citizens and Denizen detected. Denizen requires a specific Citizens build — "
        "mismatched versions cause NPC failures.",
        "Use the Citizens build recommended by the Denizen release notes.",
    ),
    (
        {"clearlagg", "entitytracker", "laggremover"},
        "warning",
        "Multiple entity-clearing plugins detected. These will fight over entity removal "
        "schedules and double-clear, causing item loss complaints.",
        "Keep only one entity management plugin. ClearLagg is the most established.",
    ),
    (
        {"anticheat", "nocheatplus", "spartan", "grim", "vulcan", "matrix"},
        "critical",
        "Multiple anti-cheat plugins detected. These interfere with each other's movement "
        "checks, causing false positives and rubberbanding for legitimate players.",
        "Run exactly ONE anti-cheat. Grim or Vulcan are recommended for modern versions.",
    ),
]

_RUST_CONFLICTS: list[tuple[set[str], str, str, str]] = [
    (
        {"gatherrate", "gathercontrol", "gatherbonus", "gatherrewards", "quicksmelt"},
        "critical",
        "Multiple gather rate modification plugins detected. These override the same hooks "
        "and produce unpredictable multiplier stacking.",
        "Keep one gather plugin. GatherManager is the most configurable.",
    ),
    (
        {"kits", "givekits", "kitmanager"},
        "warning",
        "Multiple kit plugins detected. Duplicate kit commands confuse players and may "
        "allow kit exploits through overlapping cooldowns.",
        "Use a single kit plugin. The 'Kits' plugin by Reneb is well-maintained.",
    ),
    (
        {"stackmodifier", "stacksizes", "itemstacker"},
        "warning",
        "Multiple stack size plugins detected. Conflicting stack modifications can cause "
        "item duplication or loss when transferring between containers.",
        "Keep one stack modifier. Verify no item duplication after configuration.",
    ),
    (
        {"economycore", "economics", "serverrewards", "scrapeconomy"},
        "warning",
        "Multiple economy/reward plugins detected. Players may exploit different currency "
        "systems or encounter balance inconsistencies.",
        "Standardize on one economy plugin and configure other plugins to use it as the backend.",
    ),
    (
        {"adminradar", "vanish", "godmode"},
        "info",
        "Multiple admin tools detected. Ensure permissions are locked down — overlapping "
        "admin plugins can leak admin status to players.",
        "Verify Oxide permission groups. Only admins should have access to these plugins.",
    ),
]

_SOURCE_CONFLICTS: list[tuple[set[str], str, str, str]] = [
    (
        {"adminmenu", "sourceadmin", "baseadmin"},
        "warning",
        "Multiple admin menu plugins detected. Conflicting admin command registrations "
        "can cause menu overlap and permission bypasses.",
        "Use one admin management system. SourceMod's built-in admin is usually sufficient.",
    ),
    (
        {"anticheat", "smac", "sourceac"},
        "critical",
        "Multiple anti-cheat plugins for Source engine. These hook the same game events "
        "and cause false bans or missed detections.",
        "Run one anti-cheat. SMAC is the most compatible with SourceMod.",
    ),
]

# ---------------------------------------------------------------------------
# Crash log patterns
# ---------------------------------------------------------------------------

_MINECRAFT_CRASH_PATTERNS: list[tuple[str, str, str]] = [
    (
        r"java\.lang\.ClassNotFoundException:\s*(\S+)",
        "critical",
        "ClassNotFoundException — a plugin is referencing a class that doesn't exist. "
        "Usually means a dependency is missing or the wrong server JAR version.",
    ),
    (
        r"java\.lang\.NoClassDefFoundError:\s*(\S+)",
        "critical",
        "NoClassDefFoundError — a class was available at compile time but missing at "
        "runtime. A dependency JAR is missing or has the wrong version.",
    ),
    (
        r"java\.lang\.AbstractMethodError:\s*(\S+)",
        "critical",
        "AbstractMethodError — a plugin was compiled against a different API version. "
        "Usually a ProtocolLib or NMS version mismatch.",
    ),
    (
        r"org\.bukkit\.plugin\.InvalidPluginException.*?(?:Caused by:.+?)?\n",
        "critical",
        "InvalidPluginException — a plugin JAR failed to load. Check that it matches "
        "your server software (Spigot/Paper/Purpur) and Minecraft version.",
    ),
    (
        r"PluginClassLoader.*?cannot find.*?(\S+)",
        "critical",
        "PluginClassLoader failure — cross-plugin class resolution failed. A plugin "
        "dependency is missing or loaded in the wrong order.",
    ),
    (
        r"java\.lang\.OutOfMemoryError",
        "critical",
        "OutOfMemoryError — the JVM ran out of heap space. The server needs more RAM "
        "or a plugin has a memory leak.",
    ),
    (
        r"Could not load '([^']+)'",
        "warning",
        "Plugin failed to load. Check dependencies and version compatibility.",
    ),
    (
        r"Error occurred while enabling (\S+)",
        "warning",
        "Plugin error during enable phase. Check the plugin's config file for syntax "
        "errors and review its dependencies.",
    ),
    (
        r"Failed to load plugin\.yml",
        "critical",
        "Corrupted or invalid plugin JAR — plugin.yml is missing or unreadable. "
        "Re-download the plugin.",
    ),
    (
        r"Ambiguous plugin name `([^`]+)`.*?for files `([^`]+)` and `([^`]+)`",
        "critical",
        "Duplicate plugin detected — two JARs provide the same plugin. Remove the "
        "duplicate file.",
    ),
]

_RUST_CRASH_PATTERNS: list[tuple[str, str, str]] = [
    (
        r"NullReferenceException.*?at\s+Oxide\.Plugins\.(\w+)",
        "critical",
        "NullReferenceException in Oxide plugin — the plugin is accessing an object "
        "that doesn't exist. Usually a version mismatch or missing dependency.",
    ),
    (
        r"Failed to call hook '(\w+)' on plugin '(\w+)'",
        "warning",
        "Oxide hook failure — a plugin's hook method threw an exception. Check the "
        "plugin's compatibility with the current Oxide and Rust version.",
    ),
    (
        r"Plugin '(\w+)' has been unloaded due to error",
        "critical",
        "Plugin auto-unloaded after repeated errors. The plugin is incompatible with "
        "the current server version.",
    ),
    (
        r"Compilation error.*?(\w+\.cs)",
        "warning",
        "Plugin compilation failed. The C# source has errors or references unavailable "
        "APIs. Update the plugin or check for syntax errors.",
    ),
]

_SOURCE_CRASH_PATTERNS: list[tuple[str, str, str]] = [
    (
        r"Plugin (\S+\.smx) failed to load",
        "critical",
        "SourceMod plugin binary failed to load. Recompile or download a build "
        "matching your SourceMod version.",
    ),
    (
        r"Native \"(\w+)\" was not found",
        "critical",
        "Missing native function — a required extension is not loaded. Install the "
        "extension the plugin depends on.",
    ),
]

_GENERIC_CRASH_PATTERNS: list[tuple[str, str, str]] = [
    (
        r"Segmentation fault|SIGSEGV",
        "critical",
        "Segmentation fault — a native crash in the server binary or a native plugin. "
        "Check for corrupted game files or incompatible native mods.",
    ),
    (
        r"Out of memory|Cannot allocate memory|OOM",
        "critical",
        "Out of memory — the server process exceeded available RAM. Increase memory "
        "allocation or reduce loaded mods/plugins.",
    ),
    (
        r"(?:Connection|Read) timed out|ETIMEDOUT",
        "warning",
        "Network timeout detected in logs. Check network connectivity and firewall "
        "rules between services.",
    ),
]


# ---------------------------------------------------------------------------
# Game type auto-detection
# ---------------------------------------------------------------------------

def _detect_game_type(file_listing: str, log_text: str) -> str | None:
    """Auto-detect game type from container files and log output.

    Examines directory listings and log content for game-specific markers.
    Returns one of: 'minecraft_java', 'minecraft_bedrock', 'rust', 'source',
    'valheim', 'ark', 'terraria', or None if undetectable.
    """
    combined = (file_listing + "\n" + log_text).lower()

    # Minecraft Java — look for Bukkit/Spigot/Paper/Purpur markers or plugins/ dir
    if any(marker in combined for marker in [
        "bukkit", "spigot", "paper", "purpur", "plugin.yml",
        "/data/plugins/", "/server/plugins/", "minecraft server",
        "[server thread/", "loading libraries",
    ]):
        return "minecraft_java"

    # Minecraft Bedrock — look for bedrock-specific markers
    if any(marker in combined for marker in [
        "bedrock_server", "behavior_packs", "resource_packs",
        "bedrock dedicated server",
    ]):
        return "minecraft_bedrock"

    # Rust (Oxide/uMod)
    if any(marker in combined for marker in [
        "/oxide/", "umod", "oxide.plugins", "rustdedicated",
        "rust dedicated server", "/oxide/plugins/",
    ]):
        return "rust"

    # Source engine (CS2, Garry's Mod, TF2)
    if any(marker in combined for marker in [
        "sourcemod", "metamod", "/addons/sourcemod/", "srcds",
        "source dedicated server", "/csgo/", "/garrysmod/",
    ]):
        return "source"

    # Valheim
    if any(marker in combined for marker in [
        "valheim", "bepinex", "/bepinex/plugins/", "valheim dedicated",
    ]):
        return "valheim"

    # ARK
    if any(marker in combined for marker in [
        "arkserver", "shootergame", "ark dedicated",
        "/shootergame/", "arksurvival",
    ]):
        return "ark"

    # Terraria
    if any(marker in combined for marker in [
        "tshock", "terraria", "/serverplugins/", "terrariaserver",
    ]):
        return "terraria"

    return None


# ---------------------------------------------------------------------------
# Conflict checking per game type
# ---------------------------------------------------------------------------

def _normalize_plugin_names(file_listing: str, extensions: set[str]) -> list[str]:
    """Extract normalized plugin names from a directory listing.

    Strips version numbers, file extensions, and normalizes casing so that
    'EssentialsX-2.20.1.jar' becomes 'essentialsx'.
    """
    plugins: list[str] = []
    for line in file_listing.splitlines():
        # Grab the last token on each line (the filename in ls -la output)
        parts = line.split()
        if not parts:
            continue
        filename = parts[-1]

        # Check if the file has a relevant extension
        if not any(filename.lower().endswith(ext) for ext in extensions):
            continue

        # Strip extension
        name = filename
        for ext in extensions:
            if name.lower().endswith(ext):
                name = name[: -len(ext)]
                break

        # Strip version numbers: common patterns like -2.20.1, _v3.1, -SNAPSHOT
        name = re.sub(r"[-_]v?[\d][\d.]*[-_]?(?:snapshot|release|beta|alpha|dev|build\d*)?$", "", name, flags=re.IGNORECASE)
        name = re.sub(r"[-_](?:snapshot|release|beta|alpha|dev|build\d*)$", "", name, flags=re.IGNORECASE)

        if name:
            plugins.append(name.lower())

    return plugins


def _find_conflicts(
    plugins: list[str],
    conflict_db: list[tuple[set[str], str, str, str]],
) -> list[dict[str, str]]:
    """Check a list of plugin names against a conflict database.

    Returns a list of conflict dicts with keys: severity, description,
    recommendation, conflicting_plugins.
    """
    conflicts: list[dict[str, str]] = []

    for pattern_set, severity, description, recommendation in conflict_db:
        matched = [p for p in plugins if p in pattern_set]
        if len(matched) >= 2 or (len(pattern_set) == 1 and len(matched) == 1):
            # For single-entry sets (advisory warnings), only flag if the plugin exists
            # For multi-entry sets, flag when 2+ conflicting plugins are present
            if len(pattern_set) == 1 or len(matched) >= 2:
                conflicts.append({
                    "severity": severity,
                    "conflicting_plugins": ", ".join(sorted(set(matched))),
                    "description": description,
                    "recommendation": recommendation,
                })

    return conflicts


def _check_minecraft_conflicts(
    plugin_listing: str,
    log_text: str,
) -> list[dict[str, str]]:
    """Analyze Minecraft Java plugins for known conflicts and crash patterns.

    Returns a list of conflict/issue dicts.
    """
    plugins = _normalize_plugin_names(plugin_listing, {".jar"})
    conflicts = _find_conflicts(plugins, _MINECRAFT_CONFLICTS)

    # Check for duplicate plugin JARs (same plugin, different versions)
    seen: dict[str, list[str]] = {}
    for line in plugin_listing.splitlines():
        parts = line.split()
        if not parts:
            continue
        filename = parts[-1]
        if not filename.lower().endswith(".jar"):
            continue
        base = re.sub(r"[-_]v?[\d][\d.]*.*\.jar$", "", filename, flags=re.IGNORECASE)
        base = base.lower()
        if base:
            seen.setdefault(base, []).append(filename)

    for base_name, filenames in seen.items():
        if len(filenames) > 1:
            conflicts.append({
                "severity": "critical",
                "conflicting_plugins": ", ".join(filenames),
                "description": f"Duplicate plugin JARs detected for '{base_name}': {', '.join(filenames)}. "
                    "Multiple versions of the same plugin will cause class loading conflicts.",
                "recommendation": "Remove all but the latest version. Restart the server after cleanup.",
            })

    # Parse crash patterns from logs
    crash_issues = _parse_crash_log(log_text, "minecraft_java")
    conflicts.extend(crash_issues)

    return conflicts


def _check_rust_conflicts(
    plugin_listing: str,
    log_text: str,
) -> list[dict[str, str]]:
    """Analyze Rust/Oxide plugins for known conflicts and crash patterns.

    Returns a list of conflict/issue dicts.
    """
    plugins = _normalize_plugin_names(plugin_listing, {".cs", ".dll"})
    conflicts = _find_conflicts(plugins, _RUST_CONFLICTS)

    # Parse crash patterns from logs
    crash_issues = _parse_crash_log(log_text, "rust")
    conflicts.extend(crash_issues)

    return conflicts


def _check_source_conflicts(
    plugin_listing: str,
    log_text: str,
) -> list[dict[str, str]]:
    """Analyze Source engine plugins for known conflicts and crash patterns."""
    plugins = _normalize_plugin_names(plugin_listing, {".smx", ".so", ".dll", ".vdf"})
    conflicts = _find_conflicts(plugins, _SOURCE_CONFLICTS)

    crash_issues = _parse_crash_log(log_text, "source")
    conflicts.extend(crash_issues)

    return conflicts


# ---------------------------------------------------------------------------
# Crash log parsing
# ---------------------------------------------------------------------------

def _parse_crash_log(log_text: str, game_type: str) -> list[dict[str, str]]:
    """Extract crash causes and error patterns from log text.

    Returns a list of issue dicts with severity, description, and
    recommendation keys. Deduplicates by description to avoid flooding
    the report with repeated stack traces.
    """
    if not log_text.strip():
        return []

    pattern_sets: list[list[tuple[str, str, str]]] = [_GENERIC_CRASH_PATTERNS]

    if game_type == "minecraft_java":
        pattern_sets.insert(0, _MINECRAFT_CRASH_PATTERNS)
    elif game_type == "rust":
        pattern_sets.insert(0, _RUST_CRASH_PATTERNS)
    elif game_type == "source":
        pattern_sets.insert(0, _SOURCE_CRASH_PATTERNS)

    issues: list[dict[str, str]] = []
    seen_descriptions: set[str] = set()

    for patterns in pattern_sets:
        for pattern, severity, base_description in patterns:
            matches = re.findall(pattern, log_text, re.IGNORECASE | re.DOTALL)
            if matches:
                # Include the first matched group for context if available
                match_context = matches[0] if isinstance(matches[0], str) else str(matches[0])
                description = f"{base_description} (Found: {match_context[:120]})"

                if description not in seen_descriptions:
                    seen_descriptions.add(description)
                    issues.append({
                        "severity": severity,
                        "description": description,
                        "recommendation": "Review the full stack trace above this error. "
                            "Identify the plugin/mod referenced and check for updates.",
                    })

    return issues


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

def _build_mod_report(
    server: str,
    container: str,
    game_type: str,
    conflicts: list[dict[str, str]],
    crashes: list[dict[str, str]],
) -> str:
    """Build a human-readable mod conflict and crash report.

    Standalone function for testability — no side effects, pure string
    assembly from structured data.

    Args:
        server: Server name from inventory.
        container: Docker container name.
        game_type: Detected or specified game type.
        conflicts: List of conflict dicts from conflict checking.
        crashes: List of crash issue dicts from log parsing.

    Returns:
        Formatted report string.
    """
    all_issues = conflicts + crashes

    # Sort by severity: critical first, then warning, then info
    severity_order = {"critical": 0, "warning": 1, "info": 2}
    all_issues.sort(key=lambda x: severity_order.get(x.get("severity", "info"), 3))

    lines: list[str] = []
    lines.append(f"=== MOD/PLUGIN CONFLICT REPORT ===")
    lines.append(f"Server: {server}")
    lines.append(f"Container: {container}")
    lines.append(f"Game Type: {game_type}")
    lines.append("")

    critical_count = sum(1 for i in all_issues if i.get("severity") == "critical")
    warning_count = sum(1 for i in all_issues if i.get("severity") == "warning")
    info_count = sum(1 for i in all_issues if i.get("severity") == "info")

    lines.append(f"Issues found: {len(all_issues)} "
                 f"({critical_count} critical, {warning_count} warnings, {info_count} info)")
    lines.append("")

    if not all_issues:
        lines.append("No conflicts or crash patterns detected. Server looks clean.")
        return "\n".join(lines)

    for i, issue in enumerate(all_issues, 1):
        severity = issue.get("severity", "info").upper()
        lines.append(f"--- Issue #{i} [{severity}] ---")

        if issue.get("conflicting_plugins"):
            lines.append(f"Plugins: {issue['conflicting_plugins']}")

        lines.append(f"Problem: {issue.get('description', 'Unknown')}")
        lines.append(f"Fix: {issue.get('recommendation', 'No recommendation available.')}")
        lines.append("")

    if critical_count > 0:
        lines.append("ACTION REQUIRED: Critical issues detected that will cause server "
                     "instability or crashes. Address these before restarting.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Plugin directory paths per game type
# ---------------------------------------------------------------------------

_PLUGIN_PATHS: dict[str, list[str]] = {
    "minecraft_java": [
        "/data/plugins/",
        "/server/plugins/",
        "/plugins/",
    ],
    "minecraft_bedrock": [
        "/data/behavior_packs/",
        "/data/resource_packs/",
        "/server/behavior_packs/",
        "/server/resource_packs/",
    ],
    "rust": [
        "/oxide/plugins/",
        "/server/oxide/plugins/",
        "/rustserver/oxide/plugins/",
    ],
    "source": [
        "/home/container/game/csgo/addons/sourcemod/plugins/",
        "/home/container/game/garrysmod/addons/",
        "/server/game/csgo/addons/sourcemod/plugins/",
        "/addons/sourcemod/plugins/",
    ],
    "valheim": [
        "/bepinex/plugins/",
        "/BepInEx/plugins/",
        "/server/BepInEx/plugins/",
    ],
    "ark": [
        "/ShooterGame/Binaries/Linux/Mods/",
        "/server/ShooterGame/Binaries/Linux/Mods/",
    ],
    "terraria": [
        "/server/ServerPlugins/",
        "/tshock/ServerPlugins/",
    ],
}

_LOG_PATHS: dict[str, list[str]] = {
    "minecraft_java": [
        "/data/logs/latest.log",
        "/server/logs/latest.log",
        "/logs/latest.log",
    ],
    "minecraft_bedrock": [
        "/data/logs/latest.log",
    ],
    "rust": [
        "/oxide/logs/",
        "/server/oxide/logs/",
    ],
    "source": [
        "/home/container/game/csgo/addons/sourcemod/logs/",
        "/addons/sourcemod/logs/",
    ],
    "valheim": [
        "/BepInEx/LogOutput.log",
        "/bepinex/LogOutput.log",
    ],
    "ark": [
        "/ShooterGame/Saved/Logs/ShooterGame.log",
    ],
    "terraria": [
        "/tshock/logs/",
    ],
}


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------

class ModConflictCheck(BaseTool):
    """Detect game server plugin/mod conflicts and crash causes.

    Inspects a Docker container's plugin directory and recent logs to
    identify known conflicting plugin combinations, version mismatches,
    and crash patterns across major game servers: Minecraft Java/Bedrock,
    Rust (Oxide), Source engine (CS2, GMod), Valheim, ARK, and Terraria.
    """

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "mod_conflict_check"

    @property
    def description(self) -> str:
        return (
            "Detect game server plugin/mod conflicts and crash causes inside a Docker "
            "container. Checks for known conflicting plugin pairs, version mismatches, "
            "and parses crash logs. Supports Minecraft Java/Bedrock, Rust (Oxide/uMod), "
            "Source engine (CS2, GMod), Valheim, ARK, and Terraria. Auto-detects game "
            "type if not specified."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server name from the inventory.",
                },
                "container": {
                    "type": "string",
                    "description": "Docker container name or ID running the game server.",
                },
                "game_type": {
                    "type": "string",
                    "description": (
                        "Game type: 'minecraft_java', 'minecraft_bedrock', 'rust', "
                        "'source', 'valheim', 'ark', 'terraria'. Auto-detected if omitted."
                    ),
                    "enum": [
                        "minecraft_java",
                        "minecraft_bedrock",
                        "rust",
                        "source",
                        "valheim",
                        "ark",
                        "terraria",
                    ],
                },
            },
            "required": ["server", "container"],
        }

    async def execute(
        self,
        *,
        server: str,
        container: str,
        game_type: str | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        """Run conflict detection on a game server container.

        Gathers plugin listings and log data in parallel, detects the game
        type if not provided, then runs the appropriate conflict checker.
        """
        # Phase 1: Gather discovery data in parallel to detect game type
        # and find the right plugin/log paths.
        discovery_commands = [
            # Broad file discovery to detect game type
            f"docker exec {container} ls /data/ 2>/dev/null",
            f"docker exec {container} ls /server/ 2>/dev/null",
            f"docker exec {container} ls / 2>/dev/null",
            # Get recent docker logs for crash pattern detection
            f"docker logs --tail 200 {container} 2>&1",
        ]

        discovery_results = await asyncio.gather(
            *[_run_on_server(self._inventory, server, cmd) for cmd in discovery_commands],
            return_exceptions=True,
        )

        # Combine file listing output for game type detection
        file_listing = ""
        for result in discovery_results[:3]:
            if isinstance(result, ToolResult) and result.output:
                file_listing += result.output + "\n"

        docker_log = ""
        if isinstance(discovery_results[3], ToolResult):
            docker_log = discovery_results[3].output or ""

        # Detect game type if not specified
        detected_type = game_type
        if not detected_type:
            detected_type = _detect_game_type(file_listing, docker_log)

        if not detected_type:
            return ToolResult(
                output=_build_mod_report(
                    server, container, "unknown", [],
                    [{"severity": "warning",
                      "description": "Could not auto-detect game type from container files and logs.",
                      "recommendation": "Specify game_type parameter explicitly: 'minecraft_java', "
                                       "'minecraft_bedrock', 'rust', 'source', 'valheim', 'ark', 'terraria'."}],
                ),
                exit_code=0,
            )

        # Phase 2: Gather plugin listings and game-specific logs in parallel
        plugin_paths = _PLUGIN_PATHS.get(detected_type, [])
        log_paths = _LOG_PATHS.get(detected_type, [])

        gather_commands: list[str] = []
        plugin_cmd_count = len(plugin_paths)

        for path in plugin_paths:
            gather_commands.append(
                f"docker exec {container} ls -la {path} 2>/dev/null"
            )
        for path in log_paths:
            # For directories, list files; for files, tail content
            if path.endswith("/"):
                # Find the most recent log file and tail it
                gather_commands.append(
                    f"docker exec {container} sh -c 'find {path} -name \"*.log\" -o -name \"*.txt\" 2>/dev/null"
                    f" | head -5 | xargs tail -200 2>/dev/null'"
                )
            else:
                gather_commands.append(
                    f"docker exec {container} tail -200 {path} 2>/dev/null"
                )

        gather_results = await asyncio.gather(
            *[_run_on_server(self._inventory, server, cmd) for cmd in gather_commands],
            return_exceptions=True,
        )

        # Combine plugin listings
        plugin_output = ""
        for result in gather_results[:plugin_cmd_count]:
            if isinstance(result, ToolResult) and result.output:
                plugin_output += result.output + "\n"

        # Combine log output with docker logs
        log_output = docker_log
        for result in gather_results[plugin_cmd_count:]:
            if isinstance(result, ToolResult) and result.output:
                log_output += "\n" + result.output

        # Phase 3: Run the appropriate conflict checker
        conflicts: list[dict[str, str]] = []
        crashes: list[dict[str, str]] = []

        if detected_type == "minecraft_java":
            issues = _check_minecraft_conflicts(plugin_output, log_output)
            # Separate conflicts (those with conflicting_plugins) from crash-only issues
            for issue in issues:
                if issue.get("conflicting_plugins"):
                    conflicts.append(issue)
                else:
                    crashes.append(issue)

        elif detected_type == "rust":
            issues = _check_rust_conflicts(plugin_output, log_output)
            for issue in issues:
                if issue.get("conflicting_plugins"):
                    conflicts.append(issue)
                else:
                    crashes.append(issue)

        elif detected_type == "source":
            issues = _check_source_conflicts(plugin_output, log_output)
            for issue in issues:
                if issue.get("conflicting_plugins"):
                    conflicts.append(issue)
                else:
                    crashes.append(issue)

        elif detected_type in ("minecraft_bedrock", "valheim", "ark", "terraria"):
            # These game types use generic crash pattern detection
            # (no curated conflict database yet — log analysis only)
            crashes = _parse_crash_log(log_output, detected_type)

        report = _build_mod_report(server, container, detected_type, conflicts, crashes)
        return ToolResult(output=report, exit_code=0)
