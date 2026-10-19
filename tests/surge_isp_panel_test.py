#!/usr/bin/env python3
"""Offline ISP panel installer contracts: no real Surge or network access."""
import importlib.util
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
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
[Rule]
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
'''


def settings():
    return {
        "version": 1, "default": "ISP-Example", "entry_regex": "^US",
        "direct_ssids": [], "domains": ["claude.ai"],
        "nodes": [{"name": "ISP-Example", "protocol": "socks5", "server": "192.0.2.50",
                   "port": 1080, "username": "fixture-user", "password": "fixture-password",
                   "expected_ip": "192.0.2.50"}],
    }


class FakePanelSurge:
    def __init__(self, profile=None, fail_enable=False, enabled=True, runtime_script=True):
        self.profile = str(profile) if profile is not None else ""
        self.fail_enable = fail_enable
        self.runtime_script = runtime_script
        self.checked = []
        self.enabled_names = [isp.PANEL_MODULE_NAME] if enabled else []
        self.available_names = [isp.PANEL_MODULE_NAME]
        self.reloads = 0
        self.selections = {}
        self.script_path = ""
        self.script_argument = {}
        self.disabled_names = []
    def status(self):
        return {"profile-path": self.profile, "mode": "rule", "core-version": 6008000}
    def check(self, path):
        self.checked.append(Path(path))
    def data(self, *args):
        if args == ("module", "list"):
            return {"enabled": list(self.enabled_names), "available": list(self.available_names)}
        if args == ("script", "list"):
            scripts = []
            if self.runtime_script and self.script_path:
                scripts.append({"name": isp.PANEL_SCRIPT_NAME, "enabled": True,
                                "parameters": {"type": "generic", "script-path": self.script_path,
                                                "timeout": "30", "argument": self.script_argument}})
            return {"scripts": scripts}
        if args == ("policy-group", "get", "Claude"):
            return {"group": {"selected": isp.GROUP, "options": [{"name": isp.GROUP, "isGroup": True}]}}
        if args == ("policy-group", "get", isp.GROUP):
            return {"group": {"selected": "ISP-Example", "options": [{"name": "ISP-Example", "isGroup": False}]}}
        raise AssertionError(args)
    def module_enable(self, name):
        if self.fail_enable:
            raise isp.Error("enable failed")
        if name not in self.enabled_names:
            self.enabled_names.append(name)
    def module_disable(self, name):
        self.disabled_names.append(name)
        self.enabled_names = [x for x in self.enabled_names if x != name]
    def enabled_modules(self):
        value = self.data("module", "list")
        enabled = value.get("enabled") if isinstance(value, dict) else None
        if not isinstance(enabled, list) or not all(isinstance(x, str) for x in enabled):
            raise isp.Error("Unsupported Surge module-list response format")
        return enabled
    def module_enabled(self, name):
        return name in self.enabled_modules()
    def restore_module_enabled(self, name, was_enabled):
        if was_enabled:
            self.module_enable(name)
        else:
            self.module_disable(name)
    def script_list(self):
        value = self.data("script", "list")
        scripts = value.get("scripts") if isinstance(value, dict) else None
        if not isinstance(scripts, list):
            raise isp.Error("Unsupported Surge script-list response format")
        return scripts
    def verify_panel(self, script_path, argument):
        if not self.script_path:
            self.script_path = str(script_path)
        if not self.script_argument:
            self.script_argument = argument
        return isp.Surge.verify_panel(self, script_path, argument)
    def environment(self):
        return {"ProxyGroupSelection": dict(self.selections)}
    def reload(self):
        self.reloads += 1
    def verify(self, binding, cfg):
        pass
    def select(self, group, value):
        self.selections[group] = value
    def restore(self, env):
        self.selections = dict(env.get("ProxyGroupSelection", {}))


class BadShapeSurge(FakePanelSurge):
    def __init__(self, shape):
        super().__init__(enabled=False)
        self.shape = shape
    def data(self, *args):
        if args == ("module", "list"):
            return self.shape
        return super().data(*args)


class PanelRenderTests(unittest.TestCase):
    def test_existing_module_preserves_unknown_rows_and_replaces_owned_rows(self):
        existing = '''#!name=Unlock & IP Risk Panel
#!desc=custom
#!category=Panel

[Script]
custom_check = type=generic, script-path=https://example.invalid/custom.js
isp_check = type=generic, script-path=https://old.invalid/isp.js, argument=title=Old

[Panel]
custom_check = script-name=custom_check, update-interval=60
isp_check = script-name=isp_check, update-interval=60
'''
        rendered = isp.render_panel_module(existing, Path('/Users/me/Library/Application Support/Surge/Profiles/surge-isp-isp-check.js'), settings())
        self.assertIn('custom_check = type=generic, script-path=https://example.invalid/custom.js', rendered)
        self.assertIn('custom_check = script-name=custom_check, update-interval=60', rendered)
        self.assertEqual(rendered.count('isp_check = type=generic'), 1)
        self.assertEqual(rendered.count('isp_check = script-name=isp_check'), 1)
        self.assertIn('script-path="/Users/me/Library/Application Support/Surge/Profiles/surge-isp-isp-check.js"', rendered)
        compact = existing.replace('isp_check =', 'isp_check=')
        rendered_compact = isp.render_panel_module(compact, Path('/tmp/isp.js'), settings())
        self.assertEqual(rendered_compact.count('isp_check = type=generic'), 1)
        self.assertNotIn('isp_check= type=generic', rendered_compact)

    def test_cli_shape_module_and_script_verification_is_strict(self):
        good = FakePanelSurge(enabled=True)
        good.script_path = '/tmp/isp.js'
        good.script_argument = 'title=ISP&policy=Custom-ISP&expected-ip=192.0.2.50'
        self.assertTrue(good.module_enabled(isp.PANEL_MODULE_NAME))
        good.verify_panel(Path('/tmp/isp.js'), 'title=ISP&policy=Custom-ISP&expected-ip=192.0.2.50')
        with self.assertRaises(isp.Error):
            BadShapeSurge({'enabled': [{'name': isp.PANEL_MODULE_NAME}], 'available': []}).module_enabled(isp.PANEL_MODULE_NAME)
        missing = FakePanelSurge(enabled=True, runtime_script=False)
        with self.assertRaisesRegex(isp.Error, 'missing'):
            missing.verify_panel(Path('/tmp/isp.js'), 'title=ISP&policy=Custom-ISP&expected-ip=192.0.2.50')
        wrong_path = FakePanelSurge(enabled=True)
        wrong_path.script_path = '/tmp/other.js'
        wrong_path.script_argument = 'title=ISP&policy=Custom-ISP&expected-ip=192.0.2.50'
        with self.assertRaisesRegex(isp.Error, 'path'):
            wrong_path.verify_panel(Path('/tmp/isp.js'), 'title=ISP&policy=Custom-ISP&expected-ip=192.0.2.50')

    def test_bundled_assets_do_not_contain_real_credentials_or_expected_ip(self):
        asset_dir = ROOT / 'assets/surge-isp-panel'
        text = ''.join(p.read_text() for p in asset_dir.iterdir() if p.is_file())
        self.assertNotIn('fixture-password', text)
        self.assertNotRegex(text, r'expected-ip=(?!__ISP_ARGUMENT__)\d+\.\d+\.\d+\.\d+')


class PanelInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.modules = self.root / 'Installed Modules'
        self.profiles = self.root / 'Surge Profiles With Spaces'

    def test_absent_module_installs_bundled_copy_and_enables_by_name(self):
        surge = FakePanelSurge(enabled=False)
        self.assertTrue(isp.install_isp_panel(surge, settings(), self.state,
                                              modules_dir=self.modules, profiles_dir=self.profiles))
        module = self.modules / isp.PANEL_MODULE_FILE
        script = self.profiles / isp.PANEL_SCRIPT_FILE
        self.assertTrue(module.exists())
        self.assertTrue(script.exists())
        self.assertEqual(script.stat().st_mode & 0o777, 0o600)
        text = module.read_text()
        self.assertIn('isp_check = type=generic, timeout=30', text)
        self.assertIn('script-path=' + json.dumps(str(script)), text)
        self.assertEqual(surge.enabled_names, [isp.PANEL_MODULE_NAME])
        self.assertEqual(len(surge.checked), 1)
        self.assertEqual(surge.checked[0].parent, self.state)
        self.assertFalse(surge.checked[0].exists())

    def test_existing_hashed_module_is_updated_in_place_not_duplicated(self):
        self.modules.mkdir(parents=True)
        hashed = self.modules / '384092bf6c99110e715ad6b26100b6ae.sgmodule'
        hashed.write_text('''#!name=Unlock & IP Risk Panel
#!desc=custom
#!category=Panel

[Script]
unknown = type=generic, script-path=https://example.invalid/u.js

[Panel]
unknown = script-name=unknown, update-interval=60
''')
        surge = FakePanelSurge(enabled=True)
        isp.install_isp_panel(surge, settings(), self.state,
                              modules_dir=self.modules, profiles_dir=self.profiles)
        self.assertTrue(hashed.exists())
        self.assertFalse((self.modules / isp.PANEL_MODULE_FILE).exists())
        text = hashed.read_text()
        self.assertIn('unknown = type=generic', text)
        self.assertIn('isp_check = type=generic', text)
        self.assertEqual(len(surge.checked), 1)
        self.assertEqual(surge.checked[0].parent, self.state)
        self.assertFalse(surge.checked[0].exists())

    def test_dry_run_and_inactive_profile_do_not_write_or_enable(self):
        surge = FakePanelSurge(enabled=False)
        with redirect_stdout(io.StringIO()):
            self.assertFalse(isp.install_isp_panel(surge, settings(), self.state, dry_run=True,
                                                   modules_dir=self.modules, profiles_dir=self.profiles))
            self.assertFalse(isp.install_isp_panel(surge, settings(), self.state, active=False,
                                                   modules_dir=self.modules, profiles_dir=self.profiles))
        self.assertFalse(self.modules.exists())
        self.assertFalse(self.profiles.exists())
        self.assertEqual(surge.enabled_names, [])

    def test_enable_failure_restores_existing_module_and_script(self):
        self.modules.mkdir(parents=True); self.profiles.mkdir(parents=True)
        module = self.modules / isp.PANEL_MODULE_FILE
        script = self.profiles / isp.PANEL_SCRIPT_FILE
        module.write_text('''#!name=Unlock & IP Risk Panel
#!desc=old
#!category=Panel

[Script]
old = type=generic, script-path=https://example.invalid/old.js

[Panel]
old = script-name=old, update-interval=60
''')
        script.write_text('old script')
        surge = FakePanelSurge(fail_enable=True, enabled=False)
        with self.assertRaisesRegex(isp.Error, 'Panel install failed'):
            isp.install_isp_panel(surge, settings(), self.state,
                                  modules_dir=self.modules, profiles_dir=self.profiles)
        self.assertEqual(module.read_text().splitlines()[1], '#!desc=old')
        self.assertEqual(script.read_text(), 'old script')
        self.assertTrue(any((self.state / 'panel-backups').iterdir()))

    def test_idempotent_install_does_not_rewrite_reload_or_backup(self):
        surge = FakePanelSurge(enabled=True)
        self.assertTrue(isp.install_isp_panel(surge, settings(), self.state,
                                              modules_dir=self.modules, profiles_dir=self.profiles))
        module = self.modules / isp.PANEL_MODULE_FILE
        script = self.profiles / isp.PANEL_SCRIPT_FILE
        before = (module.stat().st_mtime_ns, script.stat().st_mtime_ns)
        backups = list((self.state / 'panel-backups').iterdir())
        reloads = surge.reloads
        with redirect_stdout(io.StringIO()):
            self.assertFalse(isp.install_isp_panel(surge, settings(), self.state,
                                                   modules_dir=self.modules, profiles_dir=self.profiles))
        self.assertEqual((module.stat().st_mtime_ns, script.stat().st_mtime_ns), before)
        self.assertEqual(list((self.state / 'panel-backups').iterdir()), backups)
        self.assertEqual(surge.reloads, reloads)

    def test_runtime_failure_restores_files_enabled_state_and_reloads(self):
        self.modules.mkdir(parents=True); self.profiles.mkdir(parents=True)
        module = self.modules / isp.PANEL_MODULE_FILE
        script = self.profiles / isp.PANEL_SCRIPT_FILE
        module.write_text('''#!name=Unlock & IP Risk Panel
#!desc=old
#!category=Panel

[Script]
old = type=generic, script-path=https://example.invalid/old.js

[Panel]
old = script-name=old, update-interval=60
''')
        script.write_text('old script')
        surge = FakePanelSurge(enabled=False, runtime_script=False)
        with self.assertRaisesRegex(isp.Error, 'enabled state were restored'):
            isp.install_isp_panel(surge, settings(), self.state,
                                  modules_dir=self.modules, profiles_dir=self.profiles)
        self.assertEqual(module.read_text().splitlines()[1], '#!desc=old')
        self.assertEqual(script.read_text(), 'old script')
        self.assertIn(isp.PANEL_MODULE_NAME, surge.disabled_names)
        self.assertGreaterEqual(surge.reloads, 2)

    def test_setup_installs_panel_only_after_active_successful_apply(self):
        profile_dir = self.root / 'profiles'; profile_dir.mkdir()
        profile = profile_dir / 'Main.conf'; profile.write_text(MAIN)
        source = profile_dir / 'source.conf'; source.write_text(SOURCE)
        cfg = self.root / 'config.json'; isp.save_json(cfg, settings())
        state = self.state
        binding = {"profile": str(profile), "source": str(source), "generated": str(profile_dir / 'generated.dconf'), "target": "Claude", "enabled": True}
        manager = isp.Manager(state, FakePanelSurge(profile))
        manager.apply(binding, settings(), initialize=True)
        isp.save_json(state / 'bindings.json', {str(profile): binding})
        active_surge = FakePanelSurge(profile, enabled=False)
        with mock.patch.object(isp, 'Surge', return_value=active_surge), \
             mock.patch.object(isp, 'INSTALLED_MODULES', self.modules), \
             mock.patch.object(isp, 'PROFILES', self.profiles), \
             mock.patch.object(isp, 'install_watcher'), \
             mock.patch.object(isp, 'check_claude_connectivity'):
            self.assertEqual(isp.main(['--config', str(cfg), '--state-dir', str(state), 'setup', '--profile', str(profile)]), 0)
        self.assertEqual(active_surge.enabled_names, [isp.PANEL_MODULE_NAME])
        self.assertTrue((self.modules / isp.PANEL_MODULE_FILE).exists())

        inactive_surge = FakePanelSurge(self.root / 'other.conf', enabled=False)
        with mock.patch.object(isp, 'Surge', return_value=inactive_surge), \
             mock.patch.object(isp, 'INSTALLED_MODULES', self.root / 'inactive-modules'), \
             mock.patch.object(isp, 'PROFILES', self.root / 'inactive-profiles'), \
             mock.patch.object(isp, 'install_watcher'), \
             mock.patch.object(isp, 'check_claude_connectivity'):
            self.assertEqual(isp.main(['--config', str(cfg), '--state-dir', str(state), 'setup', '--profile', str(profile)]), 0)
        self.assertEqual(inactive_surge.enabled_names, [])
        self.assertFalse((self.root / 'inactive-modules').exists())


if __name__ == "__main__":
    unittest.main()
