#!/usr/bin/env python3
"""Offline contracts: no real proxies, credentials or subscription endpoints."""
import ast
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("surge_isp", ROOT / "scripts/surge-isp.py")
isp = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = isp
spec.loader.exec_module(isp)

SOURCE = '''#!MANAGED-CONFIG https://subscription.example/config?token=fixture
[General]
dns-server = 1.1.1.1
[Proxy]
US01 = socks5, 192.0.2.1, 1080
HK01 = socks5, 192.0.2.2, 1080
[Proxy Group]
Provider = select, HK01, US01
Claude = select, Provider, HK01, US01, DIRECT
Apple TV = select, Provider, US01, DIRECT
[Rule]
DOMAIN-SUFFIX,anthropic.com,Claude
FINAL,Provider
'''
MAIN = '''[General]
#!include https://subscription.example/config?token=fixture
[Proxy]
#!include https://subscription.example/config?token=fixture
[Proxy Group]
#!include https://subscription.example/config?token=fixture
[Rule]
#!include https://subscription.example/config?token=fixture
[Host]
internal.example = 192.0.2.10
'''


def settings():
    return {
        "version": 1, "default": "ISP-Example", "entry_regex": "^US",
        "direct_ssids": ["Home Wi-Fi"],
        "domains": ["claude.ai", "anthropic.com"],
        "nodes": [{"name": "ISP-Example", "protocol": "socks5", "server": "192.0.2.50",
                   "port": 1080, "username": "fixture-user", "password": "fixture-password",
                   "expected_ip": "192.0.2.50"}],
    }


class RenderTests(unittest.TestCase):
    def test_preserves_original_members_and_other_group_exactly(self):
        s = isp.render(SOURCE, settings(), "Claude")
        self.assertIn("Claude = select, Custom-ISP, Provider, HK01, US01, DIRECT", s)
        self.assertIn("Apple TV = select, Provider, US01, DIRECT", s)
        self.assertNotIn("#!MANAGED-CONFIG", s)
        self.assertEqual(s.count("Custom-ISP = select,"), 1)

    def test_repeated_render_uses_original_not_merged_output(self):
        self.assertEqual(isp.render(SOURCE, settings(), "Claude"), isp.render(SOURCE, settings(), "Claude"))
        with self.assertRaises(isp.Error):
            isp.render(isp.render(SOURCE, settings(), "Claude"), settings(), "Claude")

    def test_source_update_preserves_new_order_and_nodes(self):
        new = SOURCE.replace("[Proxy Group]", "US02 = socks5, 192.0.2.3, 1080\n[Proxy Group]")
        new = new.replace("Claude = select, Provider, HK01, US01, DIRECT", "Claude = select, US02, HK01, Provider")
        s = isp.render(new, settings(), "Claude")
        self.assertIn("Claude = select, Custom-ISP, US02, HK01, Provider", s)
        self.assertIn("US02 = socks5", s)

    def test_hidden_helpers_and_custom_ssids(self):
        cfg = settings()
        cfg["direct_ssids"] += ["Home Wi-Fi"]
        s = isp.render(SOURCE, cfg, "Claude")
        self.assertEqual(s.count('"SSID:Home Wi-Fi"=DIRECT'), 1)
        for line in s.splitlines():
            if line.startswith(("Custom-ISP-Transit =", "Custom-ISP-Entry =")):
                self.assertIn("hidden=true", line)
        self.assertNotIn("SUBNET,", s)
        self.assertNotIn("ROUTER:", s)

    def test_no_implicit_direct_networks(self):
        cfg = settings()
        cfg["direct_ssids"] = []
        s = isp.render(SOURCE, cfg, "Claude")
        self.assertIn("Custom-ISP-Entry = subnet, default=Custom-ISP-Transit, hidden=true", s)
        self.assertNotIn("SSID:", s)
        self.assertNotIn("SUBNET,", s)
        self.assertNotIn("ROUTER:", s)
        self.assertLess(s.index("DOMAIN-SUFFIX,claude.ai"), s.index("FINAL,"))

    def test_missing_target_no_transit_and_conflicts_fail(self):
        for source, cfg, target in [
            (SOURCE, settings(), "Missing"),
            (SOURCE, dict(settings(), entry_regex="^NOTHING$"), "Claude"),
            (SOURCE.replace("[Proxy Group]", "Custom-ISP = socks5, 192.0.2.9, 1080\n[Proxy Group]"), settings(), "Claude"),
            (SOURCE.replace("Claude = select", "Claude = smart"), settings(), "Claude"),
        ]:
            with self.subTest(target=target), self.assertRaises(isp.Error):
                isp.render(source, cfg, target)

    def test_managed_header_loss_is_not_silently_accepted(self):
        with self.assertRaises(isp.Error):
            isp.render(SOURCE.split('\n', 1)[1], settings(), 'Claude')

    def test_generated_isp_cannot_enter_transport_pool(self):
        cfg = settings(); cfg["entry_regex"] = ".*"
        with self.assertRaises(isp.Error):
            isp.render(SOURCE, cfg, "Claude")

    def test_broad_subscription_group_cannot_silently_include_isp(self):
        source = SOURCE.replace('Provider = select, HK01, US01', 'Provider = select, include-all-proxies=true')
        with self.assertRaises(isp.Error): isp.render(source, settings(), 'Claude')
        source = source.replace('include-all-proxies=true', 'include-all-proxies=true, policy-regex-filter="^US"')
        self.assertIn('Provider = select, include-all-proxies=true, policy-regex-filter="^US"', isp.render(source, settings(), 'Claude'))

    def test_special_credentials_quoted_and_newlines_rejected(self):
        cfg = settings(); cfg["nodes"][0]["password"] = 'a,b#c"d\\e'
        s = isp.render(SOURCE, cfg, "Claude")
        self.assertIn('password="a,b#c\\"d\\\\e"', s)
        cfg["nodes"][0]["password"] = 'a\n[Rule]'
        with self.assertRaises(isp.Error): isp.validate_settings(cfg)

    def test_duplicate_nodes_and_bad_ports_rejected(self):
        cfg = settings(); cfg["nodes"] *= 2
        with self.assertRaises(isp.Error): isp.validate_settings(cfg)
        for port in [0, 65536, True, "1080"]:
            cfg = settings(); cfg["nodes"][0]["port"] = port
            with self.assertRaises(isp.Error): isp.validate_settings(cfg)

    def test_proxy_url_decodes_credentials_without_executing_commands(self):
        n = isp.parse_proxy_url("socks5h://a%40b:p%3Ass%23x@192.0.2.50:1080")
        self.assertEqual((n["username"], n["password"], n["protocol"]), ("a@b", "p:ss#x", "socks5"))
        for s in ['curl -x socks5://u:p@192.0.2.50:1080', 'ftp://u:p@192.0.2.50:1080', 'socks5://192.0.2.50:1080']:
            with self.assertRaises(isp.Error): isp.parse_proxy_url(s)

    def test_wrapper_retains_source_refresh_and_local_sections(self):
        s = isp.patch_profile(MAIN, "generated.dconf")
        self.assertIn("#!include https://subscription.example/config?token=fixture", s)
        self.assertEqual(s.count("#!include generated.dconf"), 3)
        self.assertIn("internal.example = 192.0.2.10", s)
        with self.assertRaises(isp.Error):
            isp.patch_profile(MAIN.replace("[Proxy]\n", "[Proxy]\nPrivate = direct\n"), "generated.dconf")
        with self.assertRaises(isp.Error):
            isp.patch_profile(MAIN.replace("#!include https://", "#!include mine.dconf, https://"), "generated.dconf")

    def test_managed_source_gets_separate_local_entry(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / 'Provider.conf'; source.write_text(SOURCE)
            b = isp.new_binding(source, 'Claude')
            self.assertEqual(Path(b['profile']).name, 'Provider-ISP.conf')
            self.assertEqual(Path(b['source']), source)
            self.assertIn('#!include Provider.conf', b['template'])
            self.assertEqual(source.read_text(), SOURCE)

    def test_state_file_mode_and_symlink_rejection(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "private.json"
            isp.atomic_write(p, "{}"); self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            link = Path(td) / "link"; link.symlink_to(p)
            with self.assertRaises(isp.Error): isp.atomic_write(link, "oops")

    def test_shared_directory_permissions_are_not_changed(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)/'shared'; p.mkdir(mode=0o755)
            with self.assertRaises(isp.Error): isp.private_dir(p)
            self.assertEqual(p.stat().st_mode & 0o777, 0o755)


class FakeSurge:
    def __init__(self, profile):
        self.profile = str(profile); self.fail_check = False; self.fail_reload = False
        self.reloads = 0; self.selections = {}; self.verified = []
    def status(self): return {"profile-path": self.profile, "mode": "rule", "core-version": 6008000}
    def check(self, path):
        if self.fail_check: raise isp.Error("invalid candidate")
    def environment(self): return {"ProxyGroupSelection": dict(self.selections), "AutoPolicyGroupOverride": {}, "ProxyMode": 2}
    def reload(self):
        self.reloads += 1
        if self.fail_reload:
            self.fail_reload = False
            raise isp.Error("reload failed")
    def select(self, group, value): self.selections[group] = value
    def restore(self, env): self.selections = dict(env["ProxyGroupSelection"])
    def module_enable(self, name): self.selections["module:" + name] = "enabled"
    def module_disable(self, name): self.selections.pop("module:" + name, None)
    def restore_module_enabled(self, name, was_enabled):
        self.module_enable(name) if was_enabled else self.module_disable(name)
    def module_enabled(self, name): return self.selections.get("module:" + name) == "enabled"
    def verify_panel(self, script_path, argument):
        if not self.module_enabled(isp.PANEL_MODULE_NAME):
            raise isp.Error("module disabled")
    def verify(self, binding, cfg): self.verified.append(binding["target"])


class InputAndServiceTests(unittest.TestCase):
    def test_fixed_cli_copy_is_english(self):
        tree = ast.parse((ROOT/'scripts/surge-isp.py').read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                self.assertIsNone(re.search(r'[\u3400-\u9fff\uff01-\uff60]', node.value),
                                  f'Non-English fixed text at line {node.lineno}')

    def test_cli_errors_are_english_and_sent_to_stderr(self):
        with tempfile.TemporaryDirectory() as td:
            result = subprocess.run([sys.executable, str(ROOT/'scripts/surge-isp.py'),
                                     '--config', str(Path(td)/'missing.json'), 'status'],
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        self.assertIn('[isp] ERROR:', result.stderr)
        self.assertIsNone(re.search(r'[\u3400-\u9fff]', result.stderr))

    def test_hidden_url_input_writes_only_private_config(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td)/'private'/'isp.json'; output = io.StringIO()
            url = 'socks5://fixture-user:fixture-password@192.0.2.50:1080'
            with mock.patch.object(sys.stdin, 'isatty', return_value=True), \
                 mock.patch('builtins.input', side_effect=['1', 'ISP-Example', '192.0.2.50']), \
                 mock.patch.object(isp.getpass, 'getpass', return_value=url), redirect_stdout(output):
                isp.configure(cfg)
            value = json.loads(cfg.read_text())
            self.assertEqual(value['nodes'][0]['password'], 'fixture-password')
            self.assertEqual(cfg.stat().st_mode & 0o777, 0o600)
            self.assertNotIn('fixture-password', output.getvalue())
            self.assertNotIn(url, output.getvalue())

    def test_non_tty_never_echoes_password(self):
        with mock.patch.object(sys.stdin, 'isatty', return_value=False), self.assertRaises(isp.Error):
            isp.configure(Path('/unused'))

    def test_launch_agent_uses_private_paths_without_credentials(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            with mock.patch.object(Path, 'home', return_value=home), \
                 mock.patch.object(sys, 'platform', 'darwin'), \
                 mock.patch.object(isp.subprocess, 'run', return_value=SimpleNamespace(returncode=0)):
                isp.install_watcher(home/'state', home/'config.json')
            agent = home/'Library/LaunchAgents'/f'{isp.LABEL}.plist'
            value = isp.plistlib.loads(agent.read_bytes())
            self.assertEqual(value['StartInterval'], 30)
            self.assertEqual(value['ProgramArguments'][-1], 'sync')
            self.assertEqual(agent.stat().st_mode & 0o777, 0o600)
            self.assertEqual((home/'state/sync.log').stat().st_mode & 0o777, 0o600)

    def test_probe_validates_exit_ip_without_switching_policies(self):
        surge = mock.Mock()
        surge.data.return_value = {'group': {'selected': 'ISP-Example'}}
        records = [{'status': 200, 'error': None, 'ip': '192.0.2.50'}, {'status': 401, 'error': None, 'ip': None}]
        surge.run.return_value = 'ISP_RESULT=' + json.dumps(records)
        with redirect_stdout(io.StringIO()): isp.check_connectivity(surge, settings())
        self.assertEqual(surge.run.call_args.args[0:2], ('script', 'evaluate'))
        surge.select.assert_not_called()
        records[0]['ip'] = '192.0.2.51'
        surge.run.return_value = 'ISP_RESULT=' + json.dumps(records)
        with redirect_stdout(io.StringIO()), self.assertRaises(isp.Error): isp.check_connectivity(surge, settings())
        cfg = settings(); cfg['nodes'][0]['expected_ip'] = ''
        records[0]['ip'] = '<html>challenge</html>'
        surge.run.return_value = 'ISP_RESULT=' + json.dumps(records)
        with redirect_stdout(io.StringIO()), self.assertRaises(isp.Error): isp.check_connectivity(surge, cfg)

    def test_claude_probe_respects_an_ordinary_subscription_selection(self):
        surge = mock.Mock()
        surge.data.return_value = {'group': {'selected': 'US01', 'options': [{'name': 'US01', 'isGroup': False}]}}
        records = [{'status': 200, 'error': None, 'ip': '203.0.113.9'}, {'status': 401, 'error': None, 'ip': None}]
        programs = []
        def run(*args, **kwargs):
            programs.append(Path(args[2]).read_text())
            return 'ISP_RESULT=' + json.dumps(records)
        surge.run.side_effect = run
        output = io.StringIO()
        with redirect_stdout(output):
            isp.check_connectivity(surge, settings(), 'Claude', label='Claude policy exit IP')
        self.assertIn('const probePolicy="Claude"', programs[0])
        self.assertIn('Claude policy exit IP: 203.0.113.9', output.getvalue())
        self.assertIn('not pinned to a configured ISP', output.getvalue())
        surge.select.assert_not_called()

    def test_claude_selection_chain_resolves_custom_isp(self):
        surge = mock.Mock()
        surge.data.side_effect = [
            {'group': {'selected': isp.GROUP, 'options': [{'name': isp.GROUP, 'isGroup': True}]}},
            {'group': {'selected': 'ISP-Example'}},
        ]
        self.assertEqual(isp.selected_path(surge, settings(), 'Claude'), ['Claude', isp.GROUP, 'ISP-Example'])

    def test_global_mode_checks_actual_global_route_not_forced_claude_policy(self):
        surge = mock.Mock()
        surge.status.return_value = {'profile-path': '/profile', 'mode': 'proxy', 'global-policy': 'HK01'}
        with mock.patch.object(isp, 'check_connectivity') as probe, redirect_stdout(io.StringIO()):
            isp.check_claude_connectivity(surge, settings(), {'profile': '/profile', 'target': 'Claude'})
        probe.assert_called_once_with(surge, settings(), 'HK01', use_outbound=True,
                                      resolve=False, label='Active outbound exit IP')
        surge.select.assert_not_called()

    def test_inactive_profile_defers_probe(self):
        surge = mock.Mock(); surge.status.return_value = {'profile-path': '/other', 'mode': 'rule'}
        out = io.StringIO()
        with mock.patch.object(isp, 'check_connectivity') as probe, redirect_stdout(out):
            isp.check_claude_connectivity(surge, settings(), {'profile': '/profile', 'target': 'Claude'})
        probe.assert_not_called()
        self.assertIn('verification deferred', out.getvalue())

    def test_outbound_probe_does_not_force_a_policy(self):
        surge = mock.Mock()
        records = [{'status': 200, 'error': None, 'ip': '203.0.113.9'}, {'status': 401, 'error': None, 'ip': None}]
        programs = []
        def run(*args, **kwargs):
            programs.append(Path(args[2]).read_text()); return 'ISP_RESULT=' + json.dumps(records)
        surge.run.side_effect = run
        with redirect_stdout(io.StringIO()):
            isp.check_connectivity(surge, settings(), 'DIRECT', use_outbound=True, resolve=False)
        self.assertIn('const probePolicy=null;', programs[0])
        surge.select.assert_not_called()


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.p = self.root / "profiles"; self.p.mkdir()
        self.modules_patch = mock.patch.object(isp, 'INSTALLED_MODULES', self.root / 'Installed Modules')
        self.profiles_patch = mock.patch.object(isp, 'PROFILES', self.root / 'Private Surge Profiles')
        self.modules_patch.start(); self.profiles_patch.start()
        self.addCleanup(self.modules_patch.stop); self.addCleanup(self.profiles_patch.stop)
        self.main = self.p / "Main.conf"; self.main.write_text(MAIN)
        self.source = self.p / "source.conf"; self.source.write_text(SOURCE)
        self.cli = FakeSurge(self.main); self.manager = isp.Manager(self.root / "state", self.cli)
        self.binding = {"profile": str(self.main), "source": str(self.source), "generated": str(self.p / "generated.dconf"), "target": "Claude", "enabled": True}

    def test_idempotence_and_automatic_native_source_refresh(self):
        self.assertTrue(self.manager.apply(self.binding, settings(), initialize=True))
        self.assertFalse(self.manager.apply(self.binding, settings()))
        self.cli.selections['Claude'] = 'US01'  # User's manual choice must survive updates.
        self.source.write_text(SOURCE.replace("Claude = select,", "Claude = select, HK01,"))
        self.assertTrue(self.manager.apply(self.binding, settings()))
        self.assertEqual(self.cli.selections['Claude'], 'US01')
        self.assertEqual(self.cli.reloads, 2)
        self.assertIn("Claude = select, Custom-ISP, HK01,", Path(self.binding['generated']).read_text())

    def test_check_failure_does_not_touch_live_files(self):
        self.cli.fail_check = True
        with self.assertRaises(isp.Error): self.manager.apply(self.binding, settings(), initialize=True)
        self.assertEqual(self.main.read_text(), MAIN)
        self.assertFalse(Path(self.binding['generated']).exists())

    def test_reload_failure_rolls_back_and_reloads_old_profile(self):
        self.cli.fail_reload = True
        with self.assertRaises(isp.Error): self.manager.apply(self.binding, settings(), initialize=True)
        self.assertEqual(self.main.read_text(), MAIN)
        self.assertFalse(Path(self.binding['generated']).exists())
        self.assertEqual(self.cli.reloads, 2)

    def test_bad_subscription_update_keeps_last_good_generation(self):
        self.manager.apply(self.binding, settings(), initialize=True)
        before = Path(self.binding['generated']).read_bytes()
        self.source.write_text("broken")
        with self.assertRaises(isp.Error): self.manager.apply(self.binding, settings())
        self.assertEqual(Path(self.binding['generated']).read_bytes(), before)

    def test_dry_run_is_read_only(self):
        self.manager.apply(self.binding, settings(), initialize=True, dry_run=True)
        self.assertEqual(self.main.read_text(), MAIN)
        self.assertFalse(Path(self.binding['generated']).exists())
        self.assertEqual(self.cli.reloads, 0)

    def test_dry_run_never_initializes_pending_runtime(self):
        self.cli.profile = 'other'
        self.manager.apply(self.binding, settings(), initialize=True)
        self.cli.profile = str(self.main)
        self.manager.apply(self.binding, settings(), dry_run=True)
        self.assertEqual(self.cli.selections, {})
        self.assertTrue(self.binding['pending_activation'])

    def test_external_edit_is_not_overwritten(self):
        self.manager.apply(self.binding, settings(), initialize=True)
        self.main.write_text(self.main.read_text() + "# user change\n")
        with self.assertRaises(isp.Error): self.manager.apply(self.binding, settings())
        self.assertTrue(self.main.read_text().endswith("# user change\n"))

    def test_settings_update_and_inactive_profile(self):
        self.cli.profile = str(self.p / 'Other.conf')
        self.manager.apply(self.binding, settings(), initialize=True)
        self.assertEqual(self.cli.reloads, 0)
        self.assertEqual(self.cli.selections, {})
        self.cli.profile = str(self.main)
        self.manager.apply(self.binding, settings())
        self.assertEqual(self.cli.selections['Claude'], 'Custom-ISP')
        self.assertEqual(self.cli.selections['Custom-ISP'], 'ISP-Example')
        self.cli.selections['Claude'] = 'HK01'
        self.manager.apply(self.binding, settings())
        self.assertEqual(self.cli.selections['Claude'], 'HK01')

    def test_managed_profile_setup_never_overwrites_original(self):
        original = self.root/'Provider.conf'; original.write_text(SOURCE)
        b = isp.new_binding(original, 'Claude')
        self.manager.apply(b, settings(), initialize=True)
        self.assertEqual(original.read_text(), SOURCE)
        self.assertTrue(Path(b['profile']).exists())

    def test_metadata_only_update_does_not_reload(self):
        self.manager.apply(self.binding, settings(), initialize=True)
        self.source.write_text(SOURCE.replace('token=fixture', 'token=changed'))
        self.assertFalse(self.manager.apply(self.binding, settings()))
        self.assertEqual(self.cli.reloads, 1)

    def test_renderer_upgrade_refreshes_previous_fingerprint(self):
        self.manager.apply(self.binding, settings(), initialize=True)
        generated = Path(self.binding['generated'])
        stale = generated.read_text().replace(
            'DOMAIN-SUFFIX,claude.ai,Claude\n',
            '',
        )
        self.assertNotIn('DOMAIN-SUFFIX,claude.ai,Claude', stale)
        generated.write_text(stale)
        self.binding['generated_hash'] = isp.digest(stale)
        self.binding['fingerprint'] = isp.digest(
            self.source.read_text() + json.dumps(settings(), sort_keys=True, ensure_ascii=False)
        )
        self.assertTrue(self.manager.apply(self.binding, settings()))
        refreshed = generated.read_text()
        self.assertIn('DOMAIN-SUFFIX,claude.ai,Claude', refreshed)
        self.assertEqual(self.cli.reloads, 2)

    def test_sync_command_processes_source_change_without_setup(self):
        self.manager.apply(self.binding, settings(), initialize=True)
        state = self.root/'state'; config = self.root/'config.json'
        isp.save_json(config, settings())
        isp.save_json(state/'bindings.json', {str(self.main): self.binding})
        self.source.write_text(SOURCE.replace('FINAL,Provider', 'DOMAIN,updated.example,DIRECT\nFINAL,Provider'))
        with mock.patch.object(isp, 'Surge', return_value=self.cli):
            self.assertEqual(isp.main(['--config', str(config), '--state-dir', str(state), 'sync']), 0)
        self.assertIn('DOMAIN,updated.example,DIRECT', Path(self.binding['generated']).read_text())
        self.assertEqual(self.cli.reloads, 2)
        stamp = (state/'bindings.json').stat().st_mtime_ns
        with mock.patch.object(isp, 'Surge', return_value=self.cli):
            isp.main(['--config', str(config), '--state-dir', str(state), 'sync'])
        self.assertEqual((state/'bindings.json').stat().st_mtime_ns, stamp)
        self.assertEqual(self.cli.reloads, 2)

    def test_restore_pauses_binding_and_keeps_other_bindings(self):
        self.manager.apply(self.binding, settings(), initialize=True)
        backup = Path(self.binding['last_backup'])
        bindings = {str(self.main): self.binding, 'other': {'enabled': True}}
        isp.restore_backup(backup, self.root/'state', self.cli, bindings)
        self.assertEqual(self.main.read_text(), MAIN)
        self.assertFalse(Path(self.binding['generated']).exists())
        self.assertFalse(bindings[str(self.main)]['enabled'])
        self.assertTrue(bindings['other']['enabled'])
        self.manager.apply(bindings[str(self.main)], settings(), initialize=True)

    def test_keyboard_interrupt_rolls_back(self):
        self.cli.verify = mock.Mock(side_effect=KeyboardInterrupt())
        with self.assertRaises(isp.Error):
            self.manager.apply(self.binding, settings(), initialize=True)
        self.assertEqual(self.main.read_text(), MAIN)
        self.assertFalse(Path(self.binding['generated']).exists())

    def test_explicit_setup_reenables_paused_identical_binding(self):
        self.manager.apply(self.binding, settings(), initialize=True)
        self.binding['enabled'] = False
        self.manager.apply(self.binding, settings(), initialize=True)
        self.assertTrue(self.binding['enabled'])

    def test_first_activation_with_source_update_initializes_selection(self):
        self.cli.profile = 'other'
        self.manager.apply(self.binding, settings(), initialize=True)
        self.cli.profile = str(self.main)
        self.source.write_text(SOURCE.replace('FINAL,Provider', 'DOMAIN,new.example,DIRECT\nFINAL,Provider'))
        self.manager.apply(self.binding, settings())
        self.assertEqual(self.cli.selections['Claude'], 'Custom-ISP')
        self.assertEqual(self.cli.selections['Custom-ISP'], 'ISP-Example')

    def prepare_cli_settings(self):
        self.manager.apply(self.binding, settings(), initialize=True)
        config = self.root/'config.json'; isp.save_json(config, settings())
        isp.save_json(self.root/'state/bindings.json', {str(self.main): self.binding})
        return ['--config', str(config), '--state-dir', str(self.root/'state')]

    def test_setup_runs_exit_check_after_installing_agent(self):
        args = self.prepare_cli_settings(); events = []
        with mock.patch.object(isp, 'Surge', return_value=self.cli), \
             mock.patch.object(isp, 'install_watcher', side_effect=lambda *a: events.append('agent')), \
             mock.patch.object(isp, 'check_claude_connectivity', side_effect=lambda *a: events.append('probe')):
            isp.main(args + ['setup', '--profile', str(self.main)])
        self.assertEqual(events, ['agent', 'probe'])

    def test_dry_run_and_sync_never_run_network_probe(self):
        args = self.prepare_cli_settings()
        with mock.patch.object(isp, 'Surge', return_value=self.cli), \
             mock.patch.object(isp, 'install_watcher') as install, \
             mock.patch.object(isp, 'check_claude_connectivity') as probe:
            isp.main(args + ['setup', '--profile', str(self.main), '--dry-run'])
            isp.main(args + ['sync'])
        install.assert_not_called(); probe.assert_not_called()

    def test_probe_failure_does_not_rollback_valid_setup(self):
        args = self.prepare_cli_settings()
        with mock.patch.object(isp, 'Surge', return_value=self.cli), \
             mock.patch.object(isp, 'check_claude_connectivity', side_effect=isp.Error('probe failed')), \
             self.assertRaisesRegex(isp.Error, 'Setup was applied, but post-setup verification failed'):
            isp.main(args + ['setup', '--profile', str(self.main), '--no-agent'])
        self.assertTrue(Path(self.binding['generated']).exists())
        self.assertIn('generated.dconf', self.main.read_text())
        self.assertTrue(isp.load_json(self.root/'state/bindings.json')[str(self.main)]['enabled'])


if __name__ == "__main__": unittest.main()
