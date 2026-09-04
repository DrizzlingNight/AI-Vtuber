from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from ai_vtuber.config import (
    ConfigError,
    LoadedAppConfig,
    load_actions_config,
    load_app_config,
    write_actions_config,
)
from ai_vtuber.logging_setup import configure_logging
from ai_vtuber.vts.actions import (
    ActionExecutor,
    ActionMappingError,
    discover_actions,
    run_smoke,
)
from ai_vtuber.vts.client import (
    TokenStore,
    VTSAPIError,
    VTSAuthenticationError,
    VTSClient,
    VTSConnectionError,
    VTSProtocolError,
)
from ai_vtuber.vts.inventory import (
    ModelChangedDuringInventoryError,
    NoModelLoadedError,
    VTSService,
    write_inventory,
)
from ai_vtuber.vts.talk_demo import TalkDemoExecutor

DEFAULT_CONFIG = Path("config/app.yaml")


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _vts_online(url: str, timeout: float = 0.3) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname
    port = parsed.port
    if host is None or port is None:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def health_report(config: LoadedAppConfig) -> dict[str, object]:
    actions_status: dict[str, object] = {
        "path": str(config.actions_path),
        "exists": config.actions_path.exists(),
    }
    if config.actions_path.exists():
        try:
            actions = load_actions_config(config.actions_path)
            actions_status.update(
                {
                    "valid": True,
                    "model_name": actions.model_name,
                    "whitelisted_actions": sorted(actions.actions),
                }
            )
        except ConfigError as error:
            actions_status.update({"valid": False, "error": str(error)})
    return {
        "status": "ready",
        "python": {
            "version": ".".join(str(part) for part in sys.version_info[:3]),
            "is_3_11": sys.version_info[:2] == (3, 11),
        },
        "config": str(config.source),
        "vts": {
            "url": config.data.vts.url,
            "status": "online" if _vts_online(config.data.vts.url) else "offline",
        },
        "local_state": {
            "token_present": config.token_path.exists(),
            "inventory_present": config.inventory_path.exists(),
            "actions": actions_status,
        },
    }


def _authorization_notice(config: LoadedAppConfig) -> None:
    print(
        "\nVTube Studio authorization required:\n"
        "1. Enable 'Allow Plugin API access' in VTube Studio settings.\n"
        f"2. Confirm plugin '{config.data.vts.plugin_name}' by "
        f"'{config.data.vts.plugin_developer}'.\n"
        "3. Press 'Allow' in the VTube Studio popup.\n",
        file=sys.stderr,
        flush=True,
    )


def _build_client(config: LoadedAppConfig) -> VTSClient:
    token_store = TokenStore(
        config.token_path,
        config.data.vts.plugin_name,
        config.data.vts.plugin_developer,
    )
    return VTSClient(
        config.data.vts,
        token_store,
        authorization_notifier=lambda: _authorization_notice(config),
    )


async def _inventory_command(
    config: LoadedAppConfig,
    *,
    overwrite_actions: bool,
) -> int:
    async with _build_client(config) as client:
        service = VTSService(client)
        inventory = await service.refresh_inventory()
        write_inventory(config.inventory_path, inventory)
        generated = overwrite_actions or not config.actions_path.exists()
        missing: list[str] = []
        if generated:
            actions, missing = discover_actions(
                inventory,
                config.data.discovery,
            )
            write_actions_config(config.actions_path, actions)
        else:
            actions = load_actions_config(config.actions_path)
        _print_json(
            {
                "model": {
                    "name": inventory.model.name,
                    "id": inventory.model.model_id,
                },
                "counts": {
                    "hotkeys": len(inventory.hotkeys),
                    "expressions": len(inventory.expressions),
                    "input_parameters": len(inventory.input_parameters),
                    "live2d_parameters": len(inventory.live2d_parameters),
                },
                "inventory_path": str(config.inventory_path),
                "actions_path": str(config.actions_path),
                "actions_generated": generated,
                "whitelisted_actions": sorted(actions.actions),
                "missing_resources": missing,
            }
        )
        return 0


async def _smoke_command(
    config: LoadedAppConfig,
    *,
    only: str | None,
) -> int:
    async with _build_client(config) as client:
        service = VTSService(client)
        inventory = await service.refresh_inventory()
        write_inventory(config.inventory_path, inventory)
        if not config.actions_path.exists():
            generated_actions, _ = discover_actions(
                inventory,
                config.data.discovery,
            )
            write_actions_config(config.actions_path, generated_actions)
        actions = load_actions_config(config.actions_path)
        executor = ActionExecutor(service, actions)
        results = await run_smoke(executor, actions.smoke, only=only)
        _print_json(
            {
                "model": {
                    "name": inventory.model.name,
                    "id": inventory.model.model_id,
                },
                "results": results,
            }
        )
        return 2 if any(item["status"] == "skipped" for item in results) else 0


async def _talk_demo_command(
    config: LoadedAppConfig,
    *,
    duration_seconds: float | None,
) -> int:
    async with _build_client(config) as client:
        service = VTSService(client)
        inventory = await service.refresh_inventory()
        write_inventory(config.inventory_path, inventory)
        actions = load_actions_config(config.actions_path)
        executor = TalkDemoExecutor(service, actions)
        wall_started = time.perf_counter()
        scheduled_elapsed = await executor.run(duration_seconds=duration_seconds)
        _print_json(
            {
                "model": {
                    "name": inventory.model.name,
                    "id": inventory.model.model_id,
                },
                "scheduled_elapsed_seconds": round(scheduled_elapsed, 3),
                "wall_elapsed_seconds": round(time.perf_counter() - wall_started, 3),
                "status": "passed",
            }
        )
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-vtuber",
        description="Local AI VTuber Phase 0/1 tools",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Path to app YAML configuration",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("health", help="Check local configuration and VTS reachability")
    inventory = subparsers.add_parser(
        "inventory",
        help="Authorize VTS and write model resource inventory",
    )
    inventory.add_argument(
        "--overwrite-actions",
        action="store_true",
        help="Regenerate the local action mapping from current resources",
    )
    smoke = subparsers.add_parser(
        "smoke",
        help="Run configured expression, hotkey, parameter, and mouth tests",
    )
    smoke.add_argument(
        "--only",
        choices=("expression", "hotkey", "continuous", "mouth"),
        help="Run only one smoke-test category",
    )
    talk_demo = subparsers.add_parser(
        "talk-demo",
        help="Run a synchronized talking-style VTS choreography",
    )
    talk_demo.add_argument(
        "--duration",
        type=float,
        help="Override the configured duration in seconds",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_app_config(args.config)
    except ConfigError as error:
        parser.error(str(error))
    configure_logging(config.data.logging.level)

    if args.command == "health":
        _print_json(health_report(config))
        return 0

    try:
        if args.command == "inventory":
            return asyncio.run(
                _inventory_command(
                    config,
                    overwrite_actions=args.overwrite_actions,
                )
            )
        if args.command == "talk-demo":
            return asyncio.run(
                _talk_demo_command(
                    config,
                    duration_seconds=args.duration,
                )
            )
        return asyncio.run(_smoke_command(config, only=args.only))
    except (
        ActionMappingError,
        ConfigError,
        ModelChangedDuringInventoryError,
        NoModelLoadedError,
        VTSAPIError,
        VTSAuthenticationError,
        VTSConnectionError,
        VTSProtocolError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
