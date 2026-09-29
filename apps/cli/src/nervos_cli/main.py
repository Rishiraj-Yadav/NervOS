"""NervOS CLI main entrypoint and command dispatcher."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from nervos_cli.client import NervosClient, NervosClientError


def _print_json(data: object) -> None:
    print(json.dumps(data, indent=2))


def handle_auth_login(args: argparse.Namespace, client: NervosClient) -> int:
    username = args.username or input("Username: ").strip()
    password = args.password or getpass.getpass("Password: ")
    try:
        user = client.login(username, password)
        print(f"Logged in as {user.get('username')} (ID: {user.get('id')})")
        return 0
    except NervosClientError as err:
        print(f"Login failed: {err.message}", file=sys.stderr)
        return 1


def handle_auth_logout(args: argparse.Namespace, client: NervosClient) -> int:
    del args
    try:
        client.logout()
        print("Logged out successfully.")
        return 0
    except NervosClientError as err:
        print(f"Logout failed: {err.message}", file=sys.stderr)
        return 1


def handle_auth_status(args: argparse.Namespace, client: NervosClient) -> int:
    del args
    try:
        user = client.me()
        print(f"Authenticated as {user.get('username')} (role: {user.get('role')})")
        return 0
    except NervosClientError:
        print("Not authenticated.", file=sys.stderr)
        return 1


def handle_package_inspect(args: argparse.Namespace, client: NervosClient) -> int:
    file_path = Path(args.file)
    if not file_path.exists():
        print(f"File not found: {file_path}", file=sys.stderr)
        return 1
    try:
        data = client.inspect_package(file_path)
        if args.json:
            _print_json(data)
            return 0
        print(f"Package ID:           {data['package_id']}")
        print(f"Version:              {data['package_version']}")
        print(f"Display Name:         {data['display_name']}")
        print(f"Signer Fingerprint:   {data['signer_fingerprint']}")
        print(f"Content Digest:       {data['content_digest']}")
        print(f"Archive Digest:       {data['archive_digest']}")
        print(f"NervOS Compatible:    {'Yes' if data['is_compatible'] else 'No'}")
        print(f"Entrypoint:           {data['entrypoint_module']}:{data['entrypoint_object']}")
        if data.get("tools_required"):
            print(f"Required Tools:       {', '.join(data['tools_required'])}")
        return 0
    except NervosClientError as err:
        print(f"Inspection failed: {err.message}", file=sys.stderr)
        return 1


def handle_package_install(args: argparse.Namespace, client: NervosClient) -> int:
    file_path = Path(args.file)
    if not file_path.exists():
        print(f"File not found: {file_path}", file=sys.stderr)
        return 1
    try:
        # First inspect to extract verified metadata
        inspected = client.inspect_package(file_path)
        pkg_id = inspected["package_id"]
        pkg_ver = inspected["package_version"]
        content_digest = inspected["content_digest"]
        signer_fp = inspected["signer_fingerprint"]
        archive_digest = inspected["archive_digest"]

        if not inspected["is_compatible"]:
            print(
                f"Error: Package {pkg_id}@{pkg_ver} is not compatible with this NervOS version.",
                file=sys.stderr,
            )
            return 1

        if args.expect_package and args.expect_package != pkg_id:
            print(
                f"Error: Expected package ID {args.expect_package}, found {pkg_id}.",
                file=sys.stderr,
            )
            return 1

        if args.expect_version and args.expect_version != pkg_ver:
            print(
                f"Error: Expected package version {args.expect_version}, found {pkg_ver}.",
                file=sys.stderr,
            )
            return 1

        # Explicit noninteractive mode check: require BOTH approval values if flags used.
        has_approval_flag = bool(args.approve_signer or args.approve_content_digest)
        if args.yes or has_approval_flag:
            if not args.approve_signer or not args.approve_content_digest:
                print(
                    "Error: Non-interactive installation requires both "
                    "--approve-signer and --approve-content-digest.",
                    file=sys.stderr,
                )
                return 1

            if args.approve_signer != signer_fp:
                print(
                    f"Error: Signer fingerprint mismatch.\n"
                    f"Expected: {args.approve_signer}\n"
                    f"Actual:   {signer_fp}",
                    file=sys.stderr,
                )
                return 1

            if args.approve_content_digest != content_digest:
                print(
                    f"Error: Content digest mismatch.\n"
                    f"Expected: {args.approve_content_digest}\n"
                    f"Actual:   {content_digest}",
                    file=sys.stderr,
                )
                return 1
        else:
            print(f"Installing package:   {pkg_id}@{pkg_ver}")
            print(f"Signer Fingerprint:   {signer_fp}")
            print(f"Content Digest:       {content_digest}")
            confirm = (
                input(f"Authorize installation of {pkg_id}@{pkg_ver}? [y/N]: ").strip().lower()
            )
            if confirm not in ("y", "yes"):
                print("Installation aborted by operator.")
                return 1

        result = client.install_package(
            file_path,
            package_id=pkg_id,
            package_version=pkg_ver,
            content_digest=content_digest,
            signer_fingerprint=signer_fp,
            archive_digest=archive_digest,
        )
        if args.json:
            _print_json(result)
        else:
            print(f"Successfully installed {pkg_id}@{pkg_ver} (Status: {result.get('status')})")
        return 0
    except NervosClientError as err:
        print(f"Installation failed: {err.message}", file=sys.stderr)
        return 1


def handle_package_list(args: argparse.Namespace, client: NervosClient) -> int:
    try:
        packages = client.list_packages(status=args.status)
        if args.json:
            _print_json(packages)
            return 0
        if not packages:
            print("No packages installed.")
            return 0
        header = (
            f"{'PACKAGE ID':<20} {'VERSION':<10} {'STATUS':<10} "
            f"{'AGENTS':<6} {'SIGNER FINGERPRINT'}"
        )
        print(header)
        print("-" * len(header))
        for p in packages:
            fp = p.get("signer_fingerprint", "")[:16] + "..."
            b_cnt = p.get("bound_instances_count", 0)
            print(
                f"{p['package_id']:<30} {p['package_version']:<10} "
                f"{p['status']:<12} {b_cnt:<8} {fp}"
            )
        return 0
    except NervosClientError as err:
        print(f"Failed to list packages: {err.message}", file=sys.stderr)
        return 1


def handle_package_show(args: argparse.Namespace, client: NervosClient) -> int:
    target = args.package
    if "@" not in target:
        print("Error: Target must be in format <package_id>@<version>", file=sys.stderr)
        return 1
    pkg_id, pkg_ver = target.split("@", 1)
    try:
        data = client.get_package(pkg_id, pkg_ver)
        if args.json:
            _print_json(data)
            return 0
        print(f"Package:              {data['package_id']}@{data['package_version']}")
        print(f"Display Name:         {data['display_name']}")
        print(f"Status:               {data['status']}")
        print(f"Signer Fingerprint:   {data['signer_fingerprint']}")
        print(f"Content Digest:       {data['content_digest']}")
        print(f"Bound Instances:      {data['bound_instances_count']}")
        print(f"Installed At:         {data.get('installed_at') or 'N/A'}")
        if data.get("config_schema"):
            print("Configuration Schema:")
            print(json.dumps(data["config_schema"], indent=2))
        return 0
    except NervosClientError as err:
        print(f"Failed to get package: {err.message}", file=sys.stderr)
        return 1


def handle_package_uninstall(args: argparse.Namespace, client: NervosClient) -> int:
    target = args.package
    if "@" not in target:
        print("Error: Target must be in format <package_id>@<version>", file=sys.stderr)
        return 1
    pkg_id, pkg_ver = target.split("@", 1)
    try:
        plan = client.get_removal_plan(pkg_id, pkg_ver)
        if not plan.get("can_begin_removal"):
            print(f"Cannot uninstall {target}:", file=sys.stderr)
            for reason in plan.get("blocking_reasons", []):
                print(f"  - {reason}", file=sys.stderr)
            return 1

        if not args.yes:
            print(f"Removal plan for {target}:")
            if plan.get("nonterminal_runs_count", 0) > 0:
                cnt = plan["nonterminal_runs_count"]
                print(
                    f"  Note: {cnt} runs active. "
                    "Version will enter pending_removal until they finish."
                )
            confirm = input(f"Confirm uninstall of {target}? [y/N]: ").strip().lower()
            if confirm not in ("y", "yes"):
                print("Uninstall aborted.")
                return 1

        result = client.uninstall_package(pkg_id, pkg_ver)
        outcome = result.get("outcome")
        if outcome == "removed":
            print(f"Successfully uninstalled {target}.")
        elif outcome == "pending_removal":
            print(f"Package {target} marked pending_removal (draining active runs).")
        return 0
    except NervosClientError as err:
        print(f"Uninstall failed: {err.message}", file=sys.stderr)
        return 1


def handle_agent_create(args: argparse.Namespace, client: NervosClient) -> int:
    target = args.package
    if "@" not in target:
        print("Error: --package must be in format <package_id>@<version>", file=sys.stderr)
        return 1
    pkg_id, pkg_ver = target.split("@", 1)
    config: dict[str, Any] = {}
    if args.config:
        config_path = Path(args.config)
        if not config_path.exists():
            print(f"Config file not found: {config_path}", file=sys.stderr)
            return 1
        parsed_config: object = json.loads(config_path.read_text(encoding="utf-8"))
        if isinstance(parsed_config, dict):
            config = cast("dict[str, Any]", parsed_config)
    try:
        instance = client.create_agent(
            display_name=args.name,
            package_id=pkg_id,
            package_version=pkg_ver,
            model_provider=args.provider,
            model_name=args.model,
            config=config,
        )
        if args.json:
            _print_json(instance)
        else:
            name = instance["display_name"]
            print(f"Created agent instance {instance['id']}: {name} ({pkg_id}@{pkg_ver})")
        return 0
    except NervosClientError as err:
        print(f"Agent creation failed: {err.message}", file=sys.stderr)
        return 1


def handle_agent_config(args: argparse.Namespace, client: NervosClient) -> int:
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Config file not found: {config_path}", file=sys.stderr)
        return 1
    config = json.loads(config_path.read_text(encoding="utf-8"))
    expected_rev = args.expected_revision or 1
    try:
        instance = client.patch_config(args.agent_id, config, expected_rev)
        rev = instance.get("config_revision")
        print(f"Updated configuration for agent {instance['id']} (revision: {rev})")
        return 0
    except NervosClientError as err:
        print(f"Config update failed: {err.message}", file=sys.stderr)
        return 1


def handle_agent_rebind(args: argparse.Namespace, client: NervosClient) -> int:
    config = None
    if args.config:
        config_path = Path(args.config)
        if not config_path.exists():
            print(f"Config file not found: {config_path}", file=sys.stderr)
            return 1
        config = json.loads(config_path.read_text(encoding="utf-8"))
    expected_rev = args.expected_revision or 1
    try:
        instance = client.rebind_agent(args.agent_id, args.to_version, config, expected_rev)
        key = instance["agent_key"]
        ver = instance["agent_definition_version"]
        print(f"Rebound agent {instance['id']} to {key}@{ver}")
        return 0
    except NervosClientError as err:
        print(f"Rebind failed: {err.message}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nervos", description="NervOS management CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Auth commands
    auth_parser = subparsers.add_parser("auth", help="Authentication management")
    auth_subs = auth_parser.add_subparsers(dest="subcommand", required=True)
    login_p = auth_subs.add_parser("login", help="Log in to NervOS")
    login_p.add_argument("--username", "-u", help="Username")
    login_p.add_argument("--password", "-p", help="Password")
    auth_subs.add_parser("logout", help="Log out")
    auth_subs.add_parser("status", help="Show current authentication status")

    # Package commands
    pkg_parser = subparsers.add_parser("package", help="Package management")
    pkg_subs = pkg_parser.add_subparsers(dest="subcommand", required=True)

    inspect_p = pkg_subs.add_parser("inspect", help="Inspect a .nervos package archive")
    inspect_p.add_argument("file", help="Path to .nervos archive")
    inspect_p.add_argument("--json", action="store_true", help="Output JSON")

    install_p = pkg_subs.add_parser("install", help="Install a .nervos package")
    install_p.add_argument("file", help="Path to .nervos archive")
    install_p.add_argument("--approve-signer", help="Approve expected signer fingerprint")
    install_p.add_argument("--approve-content-digest", help="Approve expected content digest")
    install_p.add_argument("--expect-package", help="Assert expected package ID")
    install_p.add_argument("--expect-version", help="Assert expected package version")
    install_p.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")
    install_p.add_argument("--json", action="store_true", help="Output JSON")

    list_p = pkg_subs.add_parser("list", help="List installed packages")
    list_p.add_argument(
        "--status",
        choices=["active", "failed", "pending_removal", "installed"],
        help="Filter by status",
    )
    list_p.add_argument("--json", action="store_true", help="Output JSON")

    show_p = pkg_subs.add_parser("show", help="Show package details")
    show_p.add_argument("package", help="Package identity in format <package_id>@<version>")
    show_p.add_argument("--json", action="store_true", help="Output JSON")

    uninstall_p = pkg_subs.add_parser("uninstall", help="Uninstall a package version")
    uninstall_p.add_argument("package", help="Package identity in format <package_id>@<version>")
    uninstall_p.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")

    # Agent commands
    agent_parser = subparsers.add_parser("agent", help="Agent instance management")
    agent_subs = agent_parser.add_subparsers(dest="subcommand", required=True)

    create_p = agent_subs.add_parser("create", help="Create a package-backed agent instance")
    create_p.add_argument("--name", required=True, help="Display name")
    create_p.add_argument(
        "--package", required=True, help="Package identity in format <package_id>@<version>"
    )
    create_p.add_argument(
        "--provider", required=True, help="Model provider (e.g. anthropic, openai)"
    )
    create_p.add_argument("--model", required=True, help="Model name")
    create_p.add_argument("--config", help="Path to JSON configuration file")
    create_p.add_argument("--json", action="store_true", help="Output JSON")

    config_p = agent_subs.add_parser("config", help="Update agent instance configuration")
    config_p.add_argument("agent_id", type=int, help="Agent instance ID")
    config_p.add_argument("--config", required=True, help="Path to JSON configuration file")
    config_p.add_argument(
        "--expected-revision", type=int, default=1, help="Expected configuration revision"
    )

    rebind_p = agent_subs.add_parser(
        "rebind", help="Rebind agent instance to a new package version"
    )
    rebind_p.add_argument("agent_id", type=int, help="Agent instance ID")
    rebind_p.add_argument("--to-version", required=True, help="Target package version")
    rebind_p.add_argument(
        "--config", help="Path to new JSON configuration file (optional if carry-forward)"
    )
    rebind_p.add_argument(
        "--expected-revision", type=int, default=1, help="Expected configuration revision"
    )

    rollback_p = agent_subs.add_parser(
        "rollback", help="Rollback agent instance to an older package version"
    )
    rollback_p.add_argument("agent_id", type=int, help="Agent instance ID")
    rollback_p.add_argument("--to-version", required=True, help="Target older package version")
    rollback_p.add_argument(
        "--config", help="Path to JSON configuration file (optional if carry-forward)"
    )
    rollback_p.add_argument(
        "--expected-revision", type=int, default=1, help="Expected configuration revision"
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    client = NervosClient()

    if args.command == "auth":
        if args.subcommand == "login":
            return handle_auth_login(args, client)
        if args.subcommand == "logout":
            return handle_auth_logout(args, client)
        if args.subcommand == "status":
            return handle_auth_status(args, client)

    if args.command == "package":
        if args.subcommand == "inspect":
            return handle_package_inspect(args, client)
        if args.subcommand == "install":
            return handle_package_install(args, client)
        if args.subcommand == "list":
            return handle_package_list(args, client)
        if args.subcommand == "show":
            return handle_package_show(args, client)
        if args.subcommand == "uninstall":
            return handle_package_uninstall(args, client)

    if args.command == "agent":
        if args.subcommand == "create":
            return handle_agent_create(args, client)
        if args.subcommand == "config":
            return handle_agent_config(args, client)
        if args.subcommand == "rebind":
            return handle_agent_rebind(args, client)
        if args.subcommand == "rollback":
            return handle_agent_rebind(args, client)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
