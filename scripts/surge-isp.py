#!/usr/bin/env python3
"""Local ISP overlays for Surge Mac; stdlib only. Never print proxy credentials."""
from __future__ import annotations

import argparse
import contextlib
import copy
import fcntl
import getpass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import unquote, urlsplit

GROUP = "Custom-ISP"
TRANSIT = "Custom-ISP-Transit"
ENTRY = "Custom-ISP-Entry"
DOMAINS = ["claude.ai", "claude.com", "anthropic.com", "claudeusercontent.com"]
LABEL = "local.surge-isp.sync"
PROFILES = Path.home() / "Library/Application Support/Surge/Profiles"
CONFIG = Path.home() / ".config/surge-isp/isp.json"
STATE = Path.home() / ".local/state/surge-isp/isp"
CLI = "/Applications/Surge.app/Contents/Applications/surge-cli"
SURGE_APP_SUPPORT = Path.home() / "Library/Application Support/com.nssurge.surge-mac"
INSTALLED_MODULES = SURGE_APP_SUPPORT / "Installed Modules"
PANEL_MODULE_NAME = "Unlock & IP Risk Panel"
PANEL_MODULE_FILE = "surge-isp-unlock-ip-risk-panel.sgmodule"
PANEL_SCRIPT_FILE = "surge-isp-isp-check.js"
PANEL_SCRIPT_NAME = "isp_check"
PANEL_ASSET_DIR = Path(__file__).resolve().parents[1] / "assets/surge-isp-panel"
ROUTING = ("Proxy", "Proxy Group", "Rule")


class Error(Exception):
    """A deliberately credential-free user-facing error."""


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def private_dir(path):
    if path.is_symlink():
        raise Error("Refusing a symlinked state directory")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise Error("Use an owner-only 0700 config/state directory; shared directory permissions will not be changed")


def atomic_write(path, text):
    path = Path(path)
    if path.is_symlink():
        raise Error("Refusing to overwrite a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".isp-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def save_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def load_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        raise Error("Cannot read local JSON settings; check the path, permissions and format") from None


def clean(value, field):
    if not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise Error(f"{field} must be non-empty single-line text")
    return value


def name(value):
    clean(value, "Name")
    if any(c in value for c in ',=\"\'[]#;') or value != value.strip():
        raise Error("Names must not contain config delimiters, quotes or surrounding whitespace")
    return value


def q(value):
    return json.dumps(value, ensure_ascii=False)


def defaults():
    return {"version": 1, "nodes": [], "default": "", "entry_regex": "^🇺🇸",
            "direct_ssids": [], "domains": list(DOMAINS)}


def validate_settings(cfg):
    if not isinstance(cfg, dict) or type(cfg.get("version")) is not int or cfg["version"] != 1:
        raise Error("Unsupported settings version")
    nodes = cfg.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise Error("At least one ISP node is required")
    try:
        entry = re.compile(clean(cfg.get("entry_regex"), "Transit filter regex"))
    except re.error:
        raise Error("Invalid transit filter regex") from None
    names = set()
    for n in nodes:
        if not isinstance(n, dict):
            raise Error("Each node must be an object")
        label = name(n.get("name"))
        if label in names or label in {GROUP, ENTRY, TRANSIT}:
            raise Error("Duplicate node name or conflict with a managed group")
        if entry.search(label):
            raise Error("An ISP node matches the transit filter and would create a proxy loop; narrow the regex or rename the node")
        names.add(label)
        if n.get("protocol") not in {"socks5", "http", "https"}:
            raise Error("Supported proxy protocols: socks5/http/https")
        host = clean(n.get("server"), "Proxy server")
        if re.search(r'[\s,/#?@="\[\]]', host):
            raise Error("Server must contain only an IP or hostname, without a scheme, port or path")
        if type(n.get("port")) is not int or not 1 <= n["port"] <= 65535:
            raise Error("Port must be an integer from 1 to 65535")
        clean(n.get("username"), "Username")
        clean(n.get("password"), "Password")
        if not isinstance(n.get("expected_ip", ""), str):
            raise Error("Expected exit IP must be a string; use an empty string to omit it")
        if n.get("expected_ip"):
            try:
                ipaddress.ip_address(n["expected_ip"])
            except ValueError:
                raise Error("Invalid expected exit IP") from None
    if cfg.get("default") not in names:
        raise Error("The default node is not in the node list")
    for key in ["direct_ssids", "domains"]:
        if not isinstance(cfg.get(key), list):
            raise Error(f"{key} must be a list")
        for value in cfg[key]:
            clean(value, key)
    if not cfg["domains"] or any(not re.fullmatch(r"[A-Za-z0-9.-]+", d) for d in cfg["domains"]):
        raise Error("Target domains must be a non-empty hostname list, without URLs or wildcards")
    return cfg


def parse_proxy_url(value):
    clean(value, "Proxy URL")
    try:
        u = urlsplit(value)
        protocol = "socks5" if u.scheme == "socks5h" else u.scheme
        if protocol not in {"socks5", "http", "https"} or not u.hostname or not u.port:
            raise ValueError()
        if u.username is None or u.password is None or u.path not in ("", "/") or u.query or u.fragment:
            raise ValueError()
        return {"protocol": protocol, "server": u.hostname, "port": u.port,
                "username": unquote(u.username), "password": unquote(u.password)}
    except (ValueError, TypeError):
        raise Error("Expected socks5://username:password@server:port; URL-encode special characters. Do not paste a curl command") from None


def sections(text):
    result = {}; current = None
    for line in text.splitlines(keepends=True):
        m = re.fullmatch(r"\[([^\]\r\n]+)\]\s*\n?", line)
        if m:
            current = m[1]
            if current in result:
                raise Error("Duplicate config sections are not supported; refusing to guess how to merge them")
            result[current] = ""
        elif current is not None:
            result[current] += line
    return result


def definitions(body):
    result = {}
    for line in body.splitlines():
        if not line.strip() or line.lstrip().startswith(("#", ";", "//")):
            continue
        if "=" not in line:
            raise Error("Cannot parse a proxy or policy-group definition")
        key = line.split("=", 1)[0].strip().strip('"')
        if key in result:
            raise Error("Duplicate subscription policy names prevent a safe merge")
        result[key] = line
    return result


def guard_dynamic_groups(groups, cfg):
    # New [Proxy] entries must not silently leak into unrelated subscription groups.
    for line in groups.values():
        if not re.search(r"include-all-proxies\s*=\s*true\b", line):
            continue
        m = re.search(r'policy-regex-filter\s*=\s*("(?:\\.|[^"\\])*"|[^,]+)', line)
        if not m:
            raise Error("Unfiltered include-all-proxies would add ISP nodes to other groups; this layout is not supported")
        try:
            value = json.loads(m[1]) if m[1].startswith('"') else m[1].strip()
            pattern = re.compile(value)
        except (ValueError, re.error):
            raise Error("Cannot safely analyze the subscription dynamic-node filter") from None
        if any(pattern.search(n["name"]) for n in cfg["nodes"]):
            raise Error("A subscription dynamic-node filter would include ISP nodes in other groups; refusing to change their options")


def render(source, cfg, target):
    validate_settings(cfg); name(target)
    if not source.lstrip().startswith("#!MANAGED-CONFIG"):
        raise Error("The source lost its managed-profile header; keeping the last valid configuration")
    parts = sections(source)
    if any(k not in parts for k in ROUTING) or re.search(r"^\s*#!include", source, re.M):
        raise Error("A complete original Surge subscription is required (Proxy/Proxy Group/Rule); nested includes are not supported")
    if not re.search(r"^\s*FINAL,", parts["Rule"], re.M):
        raise Error("The subscription has no FINAL rule")
    proxies = definitions(parts["Proxy"]); groups = definitions(parts["Proxy Group"])
    guard_dynamic_groups(groups, cfg)
    owned = {GROUP, TRANSIT, ENTRY} | {n["name"] for n in cfg["nodes"]}
    if owned & (set(proxies) | set(groups)):
        raise Error("Subscription names conflict with custom ISP policies; do not use generated files as subscription sources")
    if target not in groups:
        raise Error("Target group not found in the original subscription; specify its name with --target-group")
    original = groups[target]
    rhs = original.split("=", 1)[1]
    if not re.match(r"\s*select\s*,", rhs):
        raise Error("The target group must be select; refusing to change its semantics implicitly")
    pattern = re.compile(cfg["entry_regex"])
    if not any(pattern.search(n) for n in proxies):
        raise Error("No subscription nodes match the transit filter; refusing a silent DIRECT fallback")
    # Only insert one member. Preserve the original name, options, order and suffix parameters.
    replaced = re.sub(r"^(\s*select\s*,)", r"\1 " + GROUP + ",", rhs, count=1)
    group_lines = parts["Proxy Group"].splitlines()
    group_lines[group_lines.index(original)] = original.split("=", 1)[0] + "=" + replaced
    nodes = sorted(cfg["nodes"], key=lambda n: n["name"] != cfg["default"])
    proxy_lines = []
    for n in nodes:
        proxy_lines.append(f'{n["name"]} = {n["protocol"]}, {q(n["server"])}, {n["port"]}, '
                           f'username={q(n["username"])}, password={q(n["password"])}, underlying-proxy={ENTRY}')
    ssids = list(dict.fromkeys(cfg["direct_ssids"]))
    extra_groups = [
        GROUP + " = select, " + ", ".join(n["name"] for n in nodes),
        f'{TRANSIT} = url-test, include-all-proxies=true, policy-regex-filter={q(cfg["entry_regex"])}, '
        'interval=600, tolerance=100, evaluate-before-use=true, hidden=true',
        f'{ENTRY} = subnet, default={TRANSIT}, '
        + "".join(q("SSID:" + s) + "=DIRECT, " for s in ssids)
        + "hidden=true",
    ]
    parts["Proxy"] = "\n".join(proxy_lines) + "\n" + parts["Proxy"]
    parts["Proxy Group"] = "\n".join(extra_groups + group_lines) + "\n"
    priority_rules = "".join(f"DOMAIN-SUFFIX,{d},{target}\n" for d in dict.fromkeys(cfg["domains"]))
    parts["Rule"] = priority_rules + parts["Rule"]
    # One atomic snapshot keeps updated node definitions and group references consistent.
    return "# Generated by surge-isp; contains credentials. Edit local isp.json, not this file.\n" + "\n".join(
        f"[{key}]\n{body.rstrip()}\n" for key, body in parts.items())


def patch_profile(text, generated, legacy=False, source_name=None):
    if text.lstrip().startswith("#!MANAGED-CONFIG"):
        raise Error("Cannot overwrite an original managed subscription; use a local linked profile")
    parts = sections(text)
    if any(k not in parts for k in ROUTING):
        raise Error("The local entry profile is missing routing sections")
    for key in ROUTING:
        for line in parts[key].splitlines():
            if line.strip() and not line.lstrip().startswith(("#", ";", "//")):
                raise Error("The entry profile contains inline routing customizations; move them to a separate config before setup")
            if line.lstrip().startswith("#!include "):
                for ref in line.split("#!include ", 1)[1].split(","):
                    ref = ref.strip()
                    allowed = {generated, source_name}
                    if legacy:
                        allowed |= {"Claude-ISP.dconf", "SNTP-Proxy-Groups.dconf"}
                    if not ref.startswith("https://") and ref not in allowed:
                        raise Error("The entry references unmanaged local routing files; refusing to discard them. Legacy migration requires --migrate-legacy")
        pattern = r"(?ms)(^\[" + re.escape(key) + r"\]\s*\n).*?(?=^\[|\Z)"
        text, count = re.subn(pattern, lambda m: m[1] + "#!include " + generated + "\n\n", text, count=1)
        if count != 1:
            raise Error("Cannot locate the entry-profile section")
    return text


def check_candidate(surge, profile, generated, main, rendered):
    """Validate without ever writing a file that the active profile references."""
    fd, stage_name = tempfile.mkstemp(prefix=".isp-stage-", suffix=".dconf", dir=profile.parent)
    os.close(fd); stage = Path(stage_name); check = stage.with_suffix(".check.dconf")
    try:
        atomic_write(stage, rendered or "")
        candidate = main
        if rendered is not None:
            candidate = main.replace("#!include " + generated.name + "\n", "#!include " + stage.name + "\n")
        atomic_write(check, candidate)
        surge.check(check)
    finally:
        stage.unlink(missing_ok=True); check.unlink(missing_ok=True)


class Surge:
    def run(self, *args, timeout=30):
        try:
            r = subprocess.run([CLI, *args], capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            raise Error("Surge CLI is unavailable or timed out; no forced restart was attempted") from None
        if r.returncode or re.match(r"\s*(Error:|Failed:)", r.stdout):
            # CLI validation errors can contain complete subscription URLs and password lines.
            raise Error(f"Surge {args[0]} failed (raw error output is withheld to protect credentials)")
        return r.stdout

    def data(self, *args):
        try:
            return json.loads(self.run("--raw", *args))
        except ValueError:
            raise Error("Unsupported Surge CLI response format") from None

    def status(self): return self.data("status")
    def environment(self): return json.loads(self.run("environment"))
    def check(self, path):
        if self.run("--check", str(path)).strip() != "OK":
            raise Error("Surge did not explicitly confirm that the configuration is valid")
    def reload(self): self.run("reload")
    def module_enable(self, name): self.run("module", "enable", name)
    def module_disable(self, name): self.run("module", "disable", name)
    def enabled_modules(self):
        value = self.data("module", "list")
        enabled = value.get("enabled") if isinstance(value, dict) else None
        if not isinstance(enabled, list) or not all(isinstance(x, str) for x in enabled):
            raise Error("Unsupported Surge module-list response format")
        return enabled
    def module_enabled(self, name): return name in self.enabled_modules()
    def restore_module_enabled(self, name, was_enabled):
        if was_enabled:
            self.module_enable(name)
        else:
            self.module_disable(name)
    def script_list(self):
        value = self.data("script", "list")
        scripts = value.get("scripts") if isinstance(value, dict) else None
        if not isinstance(scripts, list):
            raise Error("Unsupported Surge script-list response format")
        return scripts
    def verify_panel(self, script_path, argument):
        last = None
        for _ in range(20):
            try:
                if not self.module_enabled(PANEL_MODULE_NAME):
                    raise Error("The ISP panel module was installed but Surge did not report it enabled")
                scripts = self.script_list()
                match = next((x for x in scripts if isinstance(x, dict) and x.get("name") == PANEL_SCRIPT_NAME), None)
                if not match:
                    raise Error("The ISP panel script is missing from the running Surge scripts")
                if match.get("enabled") is not True:
                    raise Error("The ISP panel script is not enabled in the running Surge scripts")
                params = match.get("parameters") if isinstance(match.get("parameters"), dict) else {}
                reported_path = match.get("path") or params.get("script-path")
                if reported_path != str(script_path):
                    raise Error("The ISP panel script path does not match the deployed local script")
                if params.get("argument") != argument:
                    raise Error("The ISP panel script arguments do not match the local ISP settings")
                return
            except Error as exc:
                last = exc
                time.sleep(0.2)
        raise last
    def select(self, group, value):
        self.run("policy-group", "set", group, value)
        for _ in range(15):
            if self.data("policy-group", "get", group).get("group", {}).get("selected") == value:
                return
            time.sleep(0.2)
        raise Error("Surge did not confirm the policy selection; command delivery alone is not proof of application")
    def restore(self, env):
        self.run("set", "ProxyGroupSelection=" + json.dumps(env.get("ProxyGroupSelection", {}), ensure_ascii=False))

    def verify(self, binding, cfg):
        last = None
        for _ in range(20):
            try:
                g = self.data("policy-group", "get", binding["target"])["group"]
                if g["options"][0]["name"] != GROUP:
                    raise Error("The target group has not loaded the local overlay")
                choices = self.data("policy-group", "get", GROUP)["group"]["options"]
                if {x["name"] for x in choices} != {n["name"] for n in cfg["nodes"]}:
                    raise Error("ISP group options do not match the settings")
                rules = self.run("dump", "rule")
                first = rules.find("DOMAIN-SUFFIX," + cfg["domains"][0] + ",")
                if first < 0:
                    raise Error("Priority rules are missing from the running configuration")
                if any("SUBNET" in l and "DIRECT" in l for l in rules[:first].splitlines()):
                    raise Error("A higher-priority SUBNET/DIRECT module may bypass the ISP; resolve this conflict first")
                return
            except (Error, KeyError, IndexError) as exc:
                last = exc
                time.sleep(0.2)
        raise Error("Runtime configuration verification failed: " + (str(last) if isinstance(last, Error) else "unknown response structure"))


class Manager:
    def __init__(self, state, surge):
        self.state = Path(state); self.surge = surge

    def backup(self, binding, files, env):
        private_dir(self.state)
        private_dir(self.state / "backups")
        folder = self.state / "backups" / (time.strftime("%Y%m%d-%H%M%S") + "-" + str(time.time_ns()))
        private_dir(folder)
        save_json(folder / "snapshot.json", {"binding": binding, "files": files, "environment": env})
        return folder

    def activate_pending(self, binding, cfg):
        if not binding.get("pending_activation") or self.surge.status().get("profile-path") != binding["profile"]:
            return False
        env = self.surge.environment()
        try:
            self.surge.verify(binding, cfg)
            if not binding.get("runtime_initialized"):
                self.surge.select(binding["target"], GROUP)
            self.surge.select(GROUP, cfg["default"])
        except (Error, KeyboardInterrupt):
            self.surge.restore(env)
            raise
        binding.update(pending_activation=False, runtime_initialized=True)
        return True

    def apply(self, binding, cfg, initialize=False, dry_run=False):
        profile = Path(binding["profile"]); source = Path(binding["source"]); generated = Path(binding["generated"])
        text = profile.read_text() if profile.exists() else binding.get("template", "")
        raw = source.read_text()
        if not initialize and binding.get("profile_hash") != digest(text):
            raise Error("The entry profile was changed externally; automatic writes are blocked until reviewed and reattached")
        if generated.exists() and binding.get("generated_hash") and digest(generated.read_text()) != binding["generated_hash"]:
            raise Error("The generated file was modified externally; refusing to overwrite it")
        rendered = render(raw, cfg, binding["target"])
        main = patch_profile(text, generated.name, binding.get("legacy", False), source.name)
        fingerprint = digest(raw + json.dumps(cfg, sort_keys=True, ensure_ascii=False) + rendered + main)
        if dry_run:
            print(f"[isp] [dry-run] {profile.name}: prepend {GROUP} to {binding['target']}; ISP nodes={len(cfg['nodes'])}; no writes or reload")
            return False
        if not initialize and binding.get("fingerprint") == fingerprint and generated.exists() and main == text:
            return self.activate_pending(binding, cfg)
        if generated.exists() and generated.read_text() == rendered and main == text and not initialize:
            binding["fingerprint"] = fingerprint
            return self.activate_pending(binding, cfg)
        check_candidate(self.surge, profile, generated, main, rendered)
        if source.read_text() != raw:
            raise Error("The subscription changed during validation; skipping this write and retrying on the next sync")
        status = self.surge.status()
        active = status.get("profile-path") == str(profile)
        env = self.surge.environment() if active else {}
        old = {str(profile): text if profile.exists() else None, str(generated): generated.read_text() if generated.exists() else None}
        backup = self.backup(copy.deepcopy(binding), old, env)
        default_changed = initialize or binding.get("default") != cfg["default"]
        try:
            atomic_write(generated, rendered)
            if text != main: atomic_write(profile, main)
            if active:
                self.surge.reload()
                self.surge.verify(binding, cfg)
                if initialize or not binding.get("runtime_initialized", False):
                    self.surge.select(binding["target"], GROUP)
                if default_changed or binding.get("pending_activation"):
                    self.surge.select(GROUP, cfg["default"])
            binding.update(profile_hash=digest(main), generated_hash=digest(rendered),
                           fingerprint=fingerprint, default=cfg["default"], last_backup=str(backup), enabled=True,
                           runtime_initialized=active or binding.get("runtime_initialized", False),
                           pending_activation=not active and (default_changed or binding.get("pending_activation", False)))
            binding.pop("template", None)
        except (Exception, KeyboardInterrupt) as exc:
            for path, content in old.items():
                if content is None: Path(path).unlink(missing_ok=True)
                else: atomic_write(Path(path), content)
            if active:
                try:
                    self.surge.reload(); self.surge.restore(env)
                except Error:
                    raise Error("Apply failed and files were restored, but runtime restoration failed; check Surge. Backup: " + str(backup)) from None
            raise Error("Apply failed; previous files and selections were restored. Backup: " + str(backup)) from exc
        print(f"[isp] Merged {profile.name}; default ISP={cfg['default']}; backup={backup.name}")
        return True


@contextlib.contextmanager
def lock(state):
    private_dir(state)
    with open(state / "lock", "a") as f:
        os.chmod(f.name, 0o600)
        try: fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise Error("Another ISP operation is running; retry shortly") from None
        yield


def profile_path(value):
    p = Path(value).expanduser()
    if not p.is_absolute(): p = PROFILES / (value if value.endswith(".conf") else value + ".conf")
    if p.is_symlink() or not p.is_file(): raise Error("Profile does not exist or is a symlink")
    return p


def discover(profile):
    text = profile.read_text()
    urls = list(dict.fromkeys(re.findall(r"^#!include\s+.*?(https://[^\s,]+)", text, re.M)))
    if len(urls) != 1 or "General" not in sections(text) or urls[0] not in sections(text)["General"]:
        raise Error("A single-subscription linked profile is required, retaining its remote include in General")
    # This mapping is verified against Surge Mac 6.8; never guess another cache when absent.
    cache = profile.parent / ".managed" / (hashlib.md5(urls[0].encode()).hexdigest() + ".conf")
    if not cache.is_file(): raise Error("Surge managed cache not found; load and update the original subscription in Surge first")
    if not cache.read_text().lstrip().startswith("#!MANAGED-CONFIG"):
        raise Error("The subscription cache is not a managed profile")
    return cache


def new_binding(profile, target, legacy=False):
    text = profile.read_text()
    template = None
    if text.lstrip().startswith("#!MANAGED-CONFIG"):
        # Keep the original managed file intact and keep referencing it for native updates.
        source = profile
        profile = profile.with_name(profile.stem + "-ISP.conf")
        if profile.exists(): raise Error("The local ISP entry already exists; specify that entry directly")
        template = "# Local ISP entry; original managed subscription stays untouched.\n" + "\n".join(
            f"[{key}]\n#!include {source.name}\n" for key in sections(text))
    else:
        source = discover(profile)
    generated = profile.parent / ("surge-isp-" + digest(str(profile))[:12] + ".dconf")
    if generated.exists(): raise Error("An unregistered generated file already exists; refusing to overwrite it")
    binding = {"profile": str(profile), "source": str(source), "generated": str(generated),
               "target": name(target), "enabled": True, "legacy": legacy}
    if template is not None: binding["template"] = template
    return binding


def import_legacy(profile, config):
    if config.exists(): raise Error("Local settings already exist; use configure instead of overwriting them")
    path = profile.parent / "Claude-ISP.dconf"
    cfg = defaults()
    # Only the previously deployed, explicitly named local fragment is migrated.
    parts = sections(path.read_text())
    for label, line in definitions(parts.get("Proxy", "")).items():
        if not label.startswith("US-ISP-"): continue
        # Legacy writer used unquoted, comma-free credentials. Refuse ambiguous input.
        fields = [x.strip() for x in line.split("=", 1)[1].split(",")]
        if len(fields) != 6 or any('"' in f or "'" in f for f in fields):
            raise Error("Legacy credential syntax is ambiguous; use hidden interactive input instead")
        kv = dict(f.split("=", 1) for f in fields[3:])
        try:
            cfg["nodes"].append({"name": label, "protocol": fields[0], "server": fields[1], "port": int(fields[2]),
                                 "username": kv["username"], "password": kv["password"], "expected_ip": fields[1]})
        except (ValueError, KeyError): raise Error("Invalid legacy node fields") from None
    groups = definitions(parts.get("Proxy Group", ""))
    try:
        cfg["default"] = groups["US-ISP"].split("=", 1)[1].split(",")[1].strip()
        cfg["direct_ssids"] = re.findall(r"SSID:([^,=]+)=DIRECT", groups["Claude-Entry"])
    except (KeyError, IndexError): raise Error("Cannot identify the legacy default ISP group or SSID settings") from None
    validate_settings(cfg); private_dir(config.parent); save_json(config, cfg)
    print(f"[isp] Imported {len(cfg['nodes'])} nodes into private local settings; Surge was not changed")


def prompt(text, default=""):
    value = input(text + (f" [{default}]" if default else "") + ": ").strip()
    return value or default


def configure(config):
    if not sys.stdin.isatty(): raise Error("Interactive input requires a terminal; use a private local --config JSON file for automation")
    cfg = load_json(config) if config.exists() else defaults()
    print("1 Add/update node  2 Remove node  3 Change default exit  4 Network settings")
    action = prompt("Action", "1")
    if action == "1":
        label = name(prompt("Node name/state label (e.g. US-ISP-Delaware)"))
        prior = next((n for n in cfg["nodes"] if n["name"] == label), {})
        url = getpass.getpass("Proxy URL (hidden; press Enter for individual fields): ")
        if url:
            n = parse_proxy_url(url)
        else:
            try: port = int(prompt("Port", str(prior.get("port", 1080))))
            except ValueError: raise Error("Port must be an integer") from None
            n = {"protocol": prompt("Protocol: socks5/http/https", prior.get("protocol", "socks5")),
                 "server": prompt("Server IP/hostname", prior.get("server", "")), "port": port,
                 "username": getpass.getpass("Username (hidden; leave blank to keep the existing value): ") or prior.get("username", ""),
                 "password": getpass.getpass("Password (hidden; leave blank to keep the existing value): ") or prior.get("password", "")}
        n.update(name=label, expected_ip=prompt("Expected exit IP (optional; not inferred from the server address)", prior.get("expected_ip", "")))
        cfg["nodes"] = [x for x in cfg["nodes"] if x["name"] != label] + [n]
        if not cfg["default"] or prompt("Set as default exit? y/n", "y") == "y": cfg["default"] = label
    elif action == "2":
        label = prompt("Node name to remove")
        cfg["nodes"] = [n for n in cfg["nodes"] if n["name"] != label]
        if cfg["default"] == label: cfg["default"] = prompt("New default node name")
    elif action == "3": cfg["default"] = prompt("Default node name", cfg["default"])
    elif action == "4":
        cfg["entry_regex"] = prompt("Transit-node filter regex", cfg["entry_regex"])
        print("Enter direct-entry SSIDs separated by commas; use - to clear them")
        ssids = prompt("Additional SSIDs", ",".join(cfg["direct_ssids"]))
        cfg["direct_ssids"] = [] if ssids == "-" else [s.strip() for s in ssids.split(",") if s.strip()]
        cfg["domains"] = [s.strip() for s in prompt("Target domains", ",".join(cfg["domains"])).split(",") if s.strip()]
    else: raise Error("Unknown action")
    validate_settings(cfg); private_dir(config.parent); save_json(config, cfg)
    print("[isp] Saved local settings (0600, plaintext); attached profiles will be synchronized automatically")


def install_watcher(state, config):
    if sys.platform != "darwin": raise Error("The automatic synchronizer requires macOS launchd")
    script = str(Path(__file__).resolve())
    agent = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
    private_dir(state)
    log = state / "sync.log"
    if not log.exists(): atomic_write(log, "")
    spec = {"Label": LABEL, "ProgramArguments": [sys.executable, script, "--config", str(config), "--state-dir", str(state), "sync"],
            "RunAtLoad": True, "StartInterval": 30, "ProcessType": "Background",
            "StandardOutPath": str(log), "StandardErrorPath": str(log), "Umask": 63}
    # Polling hashes catches atomic cache replacements without depending on WatchPaths event delivery.
    atomic_write(agent, plistlib.dumps(spec).decode())
    domain = "gui/" + str(os.getuid())
    subprocess.run(["/bin/launchctl", "bootout", domain + "/" + LABEL], capture_output=True)
    r = subprocess.run(["/bin/launchctl", "bootstrap", domain, str(agent)], capture_output=True)
    if r.returncode: raise Error("launchd installation failed; config files were retained. Check the current GUI login session")
    print("[isp] Installed automatic sync: checks local subscription files every 30 seconds; no extra provider polling")


def restore_backup(path, state, surge, bindings):
    data = load_json(Path(path) / "snapshot.json")
    original = data["binding"]; key = original["profile"]
    if key not in bindings: raise Error("The backup does not belong to a registered profile")
    allowed = {key, bindings[key]["generated"]}
    if set(data["files"]) != allowed: raise Error("Backup paths do not match the registered files")
    active = surge.status().get("profile-path") == key
    old_main = data["files"][key]
    generated = Path(bindings[key]["generated"])
    if old_main is not None:
        check_candidate(surge, Path(key), generated, old_main, data["files"][str(generated)])
    elif active:
        # A newly created wrapper cannot be removed while Surge is still using it.
        source = Path(original["source"])
        surge.check(source)
        surge.run("profile", "switch", source.stem)
        for _ in range(20):
            if surge.status().get("profile-path") == str(source): break
            time.sleep(0.2)
        else: raise Error("Could not switch to the original managed profile; the current entry was not removed")
    for file, text in data["files"].items():
        if text is None: Path(file).unlink(missing_ok=True)
        else: atomic_write(Path(file), text)
    bindings[key] = dict(original, enabled=False)
    save_json(state / "bindings.json", bindings)
    if active and old_main is not None:
        surge.reload()
        if "ProxyGroupSelection" in data["environment"]:
            surge.restore(data["environment"])
    print("[isp] Restored the backup and paused automatic writes for this profile; run setup to re-enable them")



def panel_expected_ip(cfg):
    node = next((n for n in cfg["nodes"] if n["name"] == cfg["default"]), None)
    return node.get("expected_ip", "") if node else ""


def quote_module_value(value):
    # Surge config values accept JSON-style quoting; quote local paths so spaces in
    # "Application Support" are parsed as one script-path value.
    return q(str(value))


def panel_argument(cfg):
    expected = panel_expected_ip(cfg)
    args = [("title", "ISP"), ("policy", GROUP)]
    if expected:
        args.append(("expected-ip", expected))
    return "&".join(k + "=" + v for k, v in args)


def render_panel_script_line(script_path, cfg):
    return (PANEL_SCRIPT_NAME + " = type=generic, timeout=30, script-path="
            + quote_module_value(script_path) + ", argument=" + panel_argument(cfg))


def render_panel_panel_line():
    return PANEL_SCRIPT_NAME + " = script-name=" + PANEL_SCRIPT_NAME + ", update-interval=1800"


def module_title(text):
    m = re.search(r"^#!name\s*=\s*(.+?)\s*$", text, re.M)
    return m.group(1) if m else ""


def find_existing_panel_module(modules_dir):
    modules_dir = Path(modules_dir)
    if not modules_dir.exists():
        return None
    matches = []
    for path in modules_dir.glob("*.sgmodule"):
        try:
            if module_title(path.read_text()) == PANEL_MODULE_NAME:
                matches.append(path)
        except OSError:
            continue
    if not matches:
        return None
    matches.sort(key=lambda p: (p.name != PANEL_MODULE_FILE, p.name))
    return matches[0]


def replace_or_append_section_line(text, section, key, line):
    pattern = r"(?ms)(^\[" + re.escape(section) + r"\]\s*\n)(.*?)(?=^\[|\Z)"
    m = re.search(pattern, text)
    if not m:
        raise Error("The existing panel module is missing [" + section + "]")
    body = m.group(2)
    lines = body.splitlines()
    replaced = False
    out = []
    key_pattern = re.compile(r"^\s*" + re.escape(key) + r"\s*=")
    for item in lines:
        if key_pattern.match(item):
            if not replaced:
                out.append(line)
                replaced = True
            continue
        out.append(item)
    if not replaced:
        if out and out[-1].strip():
            out.append(line)
        else:
            out.append(line)
    new_body = "\n".join(out).rstrip() + "\n\n"
    return text[:m.start(2)] + new_body + text[m.end(2):]


def render_panel_module(template, script_path, cfg):
    rendered = template.replace("__ISP_SCRIPT_PATH__", quote_module_value(script_path))
    rendered = rendered.replace("__ISP_ARGUMENT__", panel_argument(cfg))
    rendered = replace_or_append_section_line(rendered, "Script", PANEL_SCRIPT_NAME,
                                              render_panel_script_line(script_path, cfg))
    rendered = replace_or_append_section_line(rendered, "Panel", PANEL_SCRIPT_NAME,
                                              render_panel_panel_line())
    if module_title(rendered) != PANEL_MODULE_NAME:
        raise Error("The bundled panel module has an unexpected name")
    return rendered


def validate_panel_candidate(surge, state, rendered):
    private_dir(state)
    fd, name = tempfile.mkstemp(prefix=".isp-panel-check-", suffix=".conf", dir=state)
    path = Path(name)
    os.close(fd)
    try:
        body = rendered.rstrip() + "\n\n[Rule]\nFINAL,DIRECT\n"
        atomic_write(path, body)
        surge.check(path)
    finally:
        path.unlink(missing_ok=True)


def panel_backup(state, files):
    private_dir(state)
    private_dir(state / "panel-backups")
    folder = state / "panel-backups" / (time.strftime("%Y%m%d-%H%M%S") + "-" + str(time.time_ns()))
    private_dir(folder)
    save_json(folder / "snapshot.json", {str(path): content for path, content in files.items()})
    return folder


def install_isp_panel(surge, cfg, state, *, dry_run=False, active=True,
                      modules_dir=None, profiles_dir=None):
    modules_dir = INSTALLED_MODULES if modules_dir is None else modules_dir
    profiles_dir = PROFILES if profiles_dir is None else profiles_dir
    if not active:
        print("[isp] Panel install deferred: this profile is not active; select it, then run setup again")
        return False
    script_src = PANEL_ASSET_DIR / "isp_check.js"
    module_src = PANEL_ASSET_DIR / "Panel.sgmodule"
    try:
        script_text = script_src.read_text()
        module_template = module_src.read_text()
    except OSError:
        raise Error("Bundled ISP panel assets are missing") from None
    script_dest = Path(profiles_dir) / PANEL_SCRIPT_FILE
    modules_dir = Path(modules_dir)
    existing = find_existing_panel_module(modules_dir)
    module_dest = existing or modules_dir / PANEL_MODULE_FILE
    base_text = existing.read_text() if existing else module_template
    rendered = render_panel_module(base_text, script_dest, cfg)
    argument = panel_argument(cfg)
    if dry_run:
        print(f"[isp] [dry-run] panel module {PANEL_MODULE_NAME}: script={script_dest}; no writes or module enable")
        return False
    script_old = script_dest.read_text() if script_dest.exists() else None
    module_old = module_dest.read_text() if module_dest.exists() else None
    script_same = script_old == script_text
    module_same = module_old == rendered
    was_enabled = surge.module_enabled(PANEL_MODULE_NAME)
    if script_same and module_same and was_enabled:
        surge.verify_panel(script_dest, argument)
        return False
    if not script_same or not module_same:
        validate_panel_candidate(surge, Path(state), rendered)
    old = {script_dest: script_old, module_dest: module_old}
    backup = None if (script_same and module_same) else panel_backup(Path(state), old)
    try:
        if not script_same or not module_same:
            script_dest.parent.mkdir(parents=True, exist_ok=True)
            modules_dir.mkdir(parents=True, exist_ok=True)
            if not script_same:
                atomic_write(script_dest, script_text)
            if not module_same:
                atomic_write(module_dest, rendered)
        if not was_enabled:
            surge.module_enable(PANEL_MODULE_NAME)
        surge.reload()
        surge.verify_panel(script_dest, argument)
    except (Exception, KeyboardInterrupt) as exc:
        for path, content in old.items():
            if content is None:
                Path(path).unlink(missing_ok=True)
            else:
                atomic_write(path, content)
        try:
            surge.restore_module_enabled(PANEL_MODULE_NAME, was_enabled)
            surge.reload()
        except Error:
            suffix = (" Backup: " + str(backup)) if backup else ""
            raise Error("Panel install failed; files were restored, but module runtime restoration failed." + suffix) from None
        suffix = (" Backup: " + str(backup)) if backup else ""
        raise Error("Panel install failed; previous panel files and module enabled state were restored." + suffix) from exc
    print(f"[isp] Installed ISP panel module: {module_dest.name}; script={script_dest.name}; backup={(backup.name if backup else 'unchanged')}")
    return True


def selected_path(surge, cfg, policy):
    """Resolve selectable groups without changing them or guessing a failed lookup is DIRECT."""
    nodes = {n["name"] for n in cfg["nodes"]}
    path = [policy]; seen = set()
    while path[-1] not in nodes:
        current = path[-1]
        if current in seen or len(seen) >= 16:
            raise Error("Policy selection contains a loop or exceeds the probe depth limit")
        seen.add(current)
        group = surge.data("policy-group", "get", current).get("group", {})
        selected = group.get("selected")
        if not isinstance(selected, str) or not selected:
            raise Error("Surge did not report a selected policy for the probe")
        path.append(selected)
        if selected in nodes: break
        member = next((m for m in group.get("options", []) if m.get("name") == selected), None)
        if member is None: raise Error("The selected policy is missing from the reported group options")
        if not member.get("isGroup", False): break
    return path


def check_connectivity(surge, cfg, policy=GROUP, *, use_outbound=False, resolve=True, label="ISP exit IP"):
    # Rule mode: explicitly probe the chosen policy. Global/direct mode: let Surge route both
    # destinations normally, rather than falsely claiming a forced ISP probe is the active exit.
    path = selected_path(surge, cfg, policy) if resolve else [policy]
    selected = path[-1]
    node = next((n for n in cfg["nodes"] if n["name"] == selected), None)
    print("[isp] Testing route: " + " -> ".join(path))
    if node is None:
        print("[isp] Selected route is not pinned to a configured ISP exit; reporting the observed IP only")
    elif selected != cfg["default"]:
        print("[isp] WARNING: the runtime selection differs from the configured default (manual override or pending sync); leaving it unchanged")
    forced_policy = None if use_outbound else policy
    program = "const probePolicy=" + json.dumps(forced_policy, ensure_ascii=False) + ";\n" + '''let targets=["https://ipv4.icanhazip.com","https://api.anthropic.com/v1/models"];
let results=[];
function next(){if(!targets.length){console.log("ISP_RESULT="+JSON.stringify(results));$done({});return;}
let url=targets.shift();let start=Date.now();
let options={url:url,timeout:12,"auto-cookie":false};
if(probePolicy!==null)options.policy=probePolicy;
$httpClient.get(options,(error,response,data)=>{
results.push({url:url,error:error||null,status:response?response.status:0,ms:Date.now()-start,
ip:url.indexOf("icanhazip")>=0&&data?data.trim().slice(0,128):null});next();});}next();'''
    with tempfile.TemporaryDirectory(prefix="surge-isp-probe-") as td:
        script = Path(td) / "probe.js"; atomic_write(script, program)
        output = surge.run("script", "evaluate", str(script), "generic", "30", timeout=40)
    match = re.search(r"ISP_RESULT=(\[[^\r\n]*\])", output)
    if not match: raise Error("The probe script returned no result; a latency test is not proof of the exit IP")
    results = json.loads(match[1])
    ok = len(results) == 2
    observed = None
    for r in results:
        displayed = dict(r)
        if isinstance(displayed.get("error"), str):
            for n in cfg["nodes"]:
                for key in ("username", "password"):
                    displayed["error"] = displayed["error"].replace(n[key], "<redacted>")
        print(json.dumps(displayed, ensure_ascii=False))
        ok = ok and not r["error"]
    if len(results) == 2:
        ok = ok and results[0]["status"] == 200 and results[1]["status"] in (200, 401)
        try:
            ipaddress.ip_address(results[0]["ip"])
            if results[0]["status"] == 200 and not results[0]["error"]:
                observed = results[0]["ip"]
        except (ValueError, TypeError): ok = False
        if node and node.get("expected_ip"): ok = ok and results[0]["ip"] == node["expected_ip"]
    print(f"[isp] {label}: {observed or 'unavailable'}")
    if resolve and selected_path(surge, cfg, policy) != path:
        raise Error("Policy selection changed during the probe; retry without changing selections")
    if not ok: raise Error("Connectivity or expected exit-IP verification failed; no exit was switched automatically")
    print("[isp] Exit IP and API connectivity passed (401 is expected without an API key; account access and conversations were not tested)")
    return results


def check_claude_connectivity(surge, cfg, binding):
    status = surge.status()
    if status.get("profile-path") != binding["profile"]:
        print("[isp] Claude exit verification deferred: this profile is not active. Select it, then run make isp-check")
        return
    mode = status.get("mode")
    print("[isp] Outbound mode: " + str(mode))
    if mode == "rule":
        surge.verify(binding, cfg)
        check_connectivity(surge, cfg, binding["target"], label="Claude policy exit IP")
    elif mode in {"proxy", "direct"}:
        policy = "DIRECT" if mode == "direct" else status.get("global-policy")
        if not policy: raise Error("Surge did not report the active global policy")
        print("[isp] WARNING: global/direct mode bypasses the Claude group; probing the active outbound route instead")
        known = {GROUP, binding["target"]} | {n["name"] for n in cfg["nodes"]}
        check_connectivity(surge, cfg, policy, use_outbound=True, resolve=policy in known, label="Active outbound exit IP")
    else:
        raise Error("Unknown outbound mode; refusing to guess the active exit")
    after = surge.status()
    keys = ["profile-path", "mode"] + (["global-policy"] if mode == "proxy" else [])
    if any(status.get(k) != after.get(k) for k in keys):
        raise Error("Active profile or outbound mode changed during verification; retry")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--state-dir", type=Path, default=STATE)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("configure")
    p = sub.add_parser("import-current"); p.add_argument("--profile", default="SNTP")
    p = sub.add_parser("setup"); p.add_argument("--profile", required=True); p.add_argument("--target-group", default="Claude")
    p.add_argument("--dry-run", action="store_true"); p.add_argument("--no-agent", action="store_true")
    p.add_argument("--migrate-legacy", action="store_true", help="Explicitly migrate the previously installed manual ISP/policy-group files")
    sub.add_parser("sync"); sub.add_parser("watch-install"); sub.add_parser("status")
    p = sub.add_parser("check"); p.add_argument("--isp-only", action="store_true", help="Probe Custom-ISP explicitly, independent of the active Claude/global selection")
    p = sub.add_parser("restore"); p.add_argument("--backup", type=Path, required=True)
    args = parser.parse_args(argv); config = args.config.expanduser().absolute(); state = args.state_dir.expanduser().absolute()
    surge = Surge()
    if args.command == "configure":
        with lock(state): configure(config)
        return 0
    if args.command == "import-current":
        with lock(state): import_legacy(profile_path(args.profile), config)
        return 0
    if args.command == "watch-install": install_watcher(state, config); return 0
    if args.command == "check":
        cfg = validate_settings(load_json(config))
        if args.isp_only: check_connectivity(surge, cfg)
        else:
            bindings = load_json(state / "bindings.json")
            binding = bindings.get(surge.status().get("profile-path"))
            if not binding or not binding.get("enabled"):
                raise Error("The active profile is not attached; use setup first or check --isp-only")
            check_claude_connectivity(surge, cfg, binding)
        return 0
    if args.command == "status":
        cfg = validate_settings(load_json(config))
        print(f"[isp] Configured default ISP: {cfg['default']}; nodes: {', '.join(n['name'] for n in cfg['nodes'])}")
        print("[isp] Direct-entry SSIDs:", ", ".join(dict.fromkeys(cfg["direct_ssids"])))
        print("[isp] Background log:", state / "sync.log")
        bindings = load_json(state / "bindings.json") if (state / "bindings.json").exists() else {}
        for b in bindings.values(): print("[isp] Profile:", Path(b["profile"]).name, "- automatic sync" if b["enabled"] else "- paused")
        return 0
    # dry-run must not create even lock/state files.
    guard = contextlib.nullcontext() if getattr(args, "dry_run", False) else lock(state)
    with guard:
        bindings_file = state / "bindings.json"
        bindings = load_json(bindings_file) if bindings_file.exists() else {}
        if args.command == "restore": restore_backup(args.backup, state, surge, bindings); return 0
        cfg = validate_settings(load_json(config)); manager = Manager(state, surge)
        if args.command == "setup":
            profile = profile_path(args.profile)
            if int(surge.status().get("core-version", 0)) < 6008000:
                raise Error("Surge Mac 6.8+ is required for reliable config-value escaping")
            binding = bindings.get(str(profile))
            if binding is None:
                binding = new_binding(profile, args.target_group, args.migrate_legacy)
            elif binding["target"] != args.target_group:
                raise Error("Target group differs from the registered value; restore the old attachment first")
            manager.apply(binding, cfg, initialize=not binding.get("fingerprint") or not binding.get("enabled"), dry_run=args.dry_run)
            if not args.dry_run:
                bindings[binding["profile"]] = binding; save_json(bindings_file, bindings)
                panel_active = surge.status().get("profile-path") == binding["profile"]
                install_isp_panel(surge, cfg, state, active=panel_active)
                if str(profile) != binding["profile"]:
                    print("[isp] Original managed profile preserved; select this local entry in Surge once: " + Path(binding["profile"]).name)
        elif args.command == "sync":
            previous = copy.deepcopy(bindings)
            errors = []
            for binding in bindings.values():
                if not binding.get("enabled"): continue
                try: manager.apply(binding, cfg)
                except (Error, OSError): errors.append(Path(binding["profile"]).name)
            if bindings != previous:
                save_json(bindings_file, bindings)
            if errors: raise Error("Automatic merge was not applied; keeping the last valid configuration for: " + ", ".join(errors) + "; run setup to inspect the error")
    if args.command == "setup" and not args.dry_run:
        if not args.no_agent: install_watcher(state, config)
        print("[isp] Setup applied; verifying the active Claude exit")
        try: check_claude_connectivity(surge, cfg, binding)
        except Error as exc:
            raise Error("Setup was applied, but post-setup verification failed: " + str(exc) + "; retry with make isp-check") from None
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (Error, OSError, ValueError, KeyError, KeyboardInterrupt) as exc:
        # Never format untrusted JSON/Surge exceptions containing credentials.
        print("[isp] ERROR: " + (str(exc) if isinstance(exc, Error) else "Operation did not complete; check local files and input. Sensitive details are withheld"), file=sys.stderr)
        sys.exit(1)
