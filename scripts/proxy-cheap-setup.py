#!/usr/bin/env python3
"""Interactive Proxy-Cheap to Surge setup. API credentials remain in memory."""
import argparse
import copy
import getpass
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

spec = importlib.util.spec_from_file_location('surge_isp', Path(__file__).with_name('surge-isp.py'))
isp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(isp)
Error = isp.Error


class API:
    def __init__(self, key, secret):
        for value in (key, secret):
            if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise Error('Both API Key and API Secret are required; control characters are not allowed')
        self.key, self.secret = key, secret

    def request(self, path, payload=None):
        if not (path == '/proxies' and payload is None or
                re.fullmatch(r'/proxies/[0-9]+/change-protocol', path) and
                (payload is None or payload == {'newType': 'SOCKS5'})):
            raise Error('API operation is not allowed')
        # curl config is supplied over stdin, never argv, disk or shell history.
        lines = ['url = ' + json.dumps('https://api.proxy-cheap.com' + path)]
        for header in ('Accept: application/json', 'X-Api-Key: ' + self.key,
                       'X-Api-Secret: ' + self.secret):
            lines.append('header = ' + json.dumps(header))
        if payload is not None:
            lines += ['header = "Content-Type: application/json"',
                      'data = ' + json.dumps(json.dumps(payload))]
        try:
            result = subprocess.run(['curl', '--disable', '--silent', '--show-error',
                '--proto', '=https', '--connect-timeout', '10', '--max-time', '30',
                '--max-filesize', '2000000', '--config', '-', '--write-out', '\n%{http_code}'],
                input='\n'.join(lines) + '\n', text=True, capture_output=True, timeout=35)
        except (OSError, subprocess.TimeoutExpired):
            raise Error('API transport failed; credentials and response withheld') from None
        if result.returncode:
            raise Error('API transport failed; check network/curl; response withheld')
        body, _, status = result.stdout.rpartition('\n')
        if status not in ('200', '201'):
            safe_status = status if re.fullmatch(r'[0-9]{3}', status) else 'unknown'
            raise Error('API HTTP ' + safe_status + '; no automatic retries; response withheld')
        if len(body.encode()) > 2000000:
            raise Error('API response exceeded the size limit')
        try:
            return json.loads(body)
        except ValueError:
            raise Error('API returned invalid JSON; response withheld') from None

    def proxies(self):
        data = self.request('/proxies')
        if isinstance(data, dict):
            # Do not silently import only the first page if pagination appears.
            if data.get('next') or data.get('nextPage') or (isinstance(data.get('links'), dict) and data['links'].get('next')):
                raise Error('Paginated API response is not supported; no import was performed')
            data = data.get('proxies')
        if not isinstance(data, list) or not all(isinstance(p, dict) for p in data):
            raise Error('Unsupported proxy-list response')
        ids = [proxy_id(p) for p in data]
        if len(set(ids)) != len(ids):
            raise Error('Duplicate proxy IDs in API response')
        return data


def proxy_id(proxy):
    value = str(proxy.get('id', ''))
    if not re.fullmatch(r'[0-9]+', value):
        raise Error('Unsupported proxy ID')
    return value


def choose(title, labels, multiple=False):
    if not labels:
        raise Error('No eligible choices: ' + title)
    print('[isp] ' + title)
    for i, label in enumerate(labels, 1):
        # Protect the terminal from control sequences in external display names.
        print('  %d. %s' % (i, ''.join(c if c.isprintable() else '?' for c in str(label))))
    while True:
        answer = input('Numbers separated by commas (q cancels): ' if multiple else 'Number (q cancels): ').strip()
        if answer.lower() == 'q':
            raise Error('Cancelled before the next operation')
        try:
            selected = list(dict.fromkeys(int(x.strip()) - 1 for x in answer.split(',')))
            if not selected or any(i < 0 or i >= len(labels) for i in selected) or (not multiple and len(selected) != 1):
                raise ValueError()
            return selected if multiple else selected[0]
        except ValueError:
            print('[isp] Enter valid menu numbers')


def confirm(message):
    if input(message + ' [y/N]: ').strip().lower() != 'y':
        raise Error('Cancelled; completed remote changes, if any, are not automatically reversed')


def to_node(proxy):
    if proxy.get('status') != 'ACTIVE' or proxy.get('networkType') != 'RESIDENTIAL_STATIC':
        raise Error('Only active static ISP proxies are supported')
    if proxy.get('proxyType') != 'SOCKS5':
        raise Error('Proxy is not confirmed as SOCKS5')
    c, a = proxy.get('connection'), proxy.get('authentication')
    if not isinstance(c, dict) or not isinstance(a, dict):
        raise Error('Missing connection or authentication fields')
    node = dict(name='PC-ISP-' + proxy_id(proxy), protocol='socks5', server=c.get('connectIp'),
                port=c.get('socks5Port'), username=a.get('username'), password=a.get('password'),
                expected_ip=c.get('publicIp') or '')
    cfg = isp.defaults(); cfg.update(nodes=[node], default=node['name'])
    isp.validate_settings(cfg)
    return node


def ensure_socks5(api, selected):
    for p in selected:
        if p.get('proxyType') == 'SOCKS5':
            continue
        pid = proxy_id(p); path = '/proxies/' + pid + '/change-protocol'
        info = api.request(path)
        if not isinstance(info, dict) or 'SOCKS5' not in info.get('availableTypes', []):
            raise Error('Selected proxy does not support SOCKS5; no local settings saved')
        if info.get('currentType') == 'SOCKS5':
            continue
        confirm('Switch selected proxy ' + pid + ' to SOCKS5? This may disrupt other clients')
        try:
            api.request(path, {'newType': 'SOCKS5'})
        except Error:
            # An ambiguous POST result is not grounds to repeat a write.
            print('[isp] Switch response failed; checking remote state without retrying POST')
        info = api.request(path)
        if not isinstance(info, dict) or info.get('currentType') != 'SOCKS5':
            raise Error('SOCKS5 switch not confirmed; inspect provider console; no local settings saved')
        print('[isp] SOCKS5 confirmed for proxy ' + pid + '; remote change will not be auto-reverted')
    fresh = {proxy_id(p): p for p in api.proxies()}
    if any(proxy_id(p) not in fresh for p in selected):
        raise Error('A selected proxy disappeared; no local settings saved')
    return [to_node(fresh[proxy_id(p)]) for p in selected]


def profile_choices(bindings):
    choices = []
    for p in sorted(isp.PROFILES.glob('*.conf')):
        if p.is_symlink():
            continue
        try:
            b = copy.deepcopy(bindings.get(str(p))) or isp.new_binding(p, 'Claude')
            source = Path(b['source']).read_text()
            groups = isp.definitions(isp.sections(source).get('Proxy Group', ''))
            targets = [k for k, v in groups.items() if re.match(r'\s*select\s*,', v.split('=', 1)[1])]
            if str(p) in bindings:
                targets = [b['target']] if b['target'] in targets else []
            if targets:
                choices.append((p, b, source, targets))
        except (Error, OSError, KeyError):
            continue
    return choices


def apply_local(config, state, before, cfg, binding, surge):
    with isp.lock(state):
        current = config.read_bytes() if config.exists() else None
        if current != before:
            raise Error('Settings changed during the wizard; rerun without overwriting them')
        bf = state / 'bindings.json'
        bindings = isp.load_json(bf) if bf.exists() else {}
        existing = bindings.get(binding['profile'])
        if existing and existing != binding:
            raise Error('Profile binding changed during the wizard; rerun')
        if not existing and 'template' in binding and Path(binding['profile']).exists():
            raise Error('A new profile appeared during the wizard; refusing to overwrite it')
        manager = isp.Manager(state, surge)
        manager.apply(binding, cfg, initialize=not binding.get('fingerprint') or not binding.get('enabled'), dry_run=True)
        isp.private_dir(config.parent)
        isp.save_json(config, cfg)
        print('[isp] Private settings saved; existing attached profiles may now sync these settings')
        try:
            manager.apply(binding, cfg, initialize=not binding.get('fingerprint') or not binding.get('enabled'))
        except BaseException:
            if before is None:
                config.unlink(missing_ok=True)
            else:
                isp.atomic_write(config, before.decode())
            raise
        try:
            bindings[binding['profile']] = binding
            isp.save_json(bf, bindings)
            if surge.status().get('profile-path') != binding['profile']:
                surge.run('profile', 'switch', Path(binding['profile']).stem)
                for _ in range(20):
                    if surge.status().get('profile-path') == binding['profile']:
                        break
                    time.sleep(0.25)
                else:
                    raise Error('Profile activation was not confirmed')
            manager.apply(binding, cfg)
            isp.save_json(bf, bindings)
            isp.install_isp_panel(surge, cfg, state, active=True)
            isp.install_watcher(state, config)
            isp.check_claude_connectivity(surge, cfg, binding)
        except (Error, OSError):
            raise Error('Settings applied, but activation/panel/watcher/verification did not finish. '
                        'Inspect make isp-status and rerun setup/check; saved settings were not rolled back') from None
    print('[isp] Setup and active-route verification completed; unselected routes were not tested')


def wizard(config, state, dry_run=False):
    if not sys.stdin.isatty():
        raise Error('Run this wizard in a local interactive terminal')
    if sys.platform != 'darwin' or not shutil.which('curl'):
        raise Error('macOS with curl and Surge Mac 6.8+ is required')
    surge = isp.Surge()
    if int(surge.status().get('core-version', 0)) < 6008000:
        raise Error('Surge Mac 6.8+ is required')
    before = config.read_bytes() if config.exists() else None
    cfg = isp.validate_settings(isp.load_json(config)) if before is not None else isp.defaults()
    bf = state / 'bindings.json'; bindings = isp.load_json(bf) if bf.exists() else {}
    profiles = profile_choices(bindings)
    if not profiles:
        raise Error('Import a supported managed subscription in Surge first; no eligible profiles found')
    print('[isp] Generate BOTH API Key and API Secret at https://app.proxy-cheap.com/api-keys')
    print('[isp] API keys stay in memory. Do not paste them into chat or command arguments.')
    api = API(getpass.getpass('API Key (hidden): '), getpass.getpass('API Secret (hidden): '))
    proxies = [p for p in api.proxies() if p.get('status') == 'ACTIVE' and p.get('networkType') == 'RESIDENTIAL_STATIC']
    labels = ['ID=' + proxy_id(p) + ' | ' + str(p.get('countryCode', '?')) + ' | ' + str(p.get('proxyType', '?')) for p in proxies]
    selected = [proxies[i] for i in choose('Select static ISP proxies', labels, True)]
    default = choose('Choose default exit', ['PC-ISP-' + proxy_id(p) for p in selected])
    _, binding, source, targets = profiles[choose('Select Surge profile', [p[0].name for p in profiles])]
    binding['target'] = targets[choose('Select target policy group', targets)]
    transit = isp.definitions(isp.sections(source).get('Proxy', ''))
    names = list(transit)
    chosen = choose('Select subscription transit nodes (automatic latency selection among these)', names, True)
    cfg['entry_regex'] = '^(?:' + '|'.join(re.escape(names[i]) for i in chosen) + ')$'
    if dry_run and any(p.get('proxyType') != 'SOCKS5' for p in selected):
        raise Error('Read-only preview: selected proxies require protocol changes; no changes made')
    planned = [to_node(dict(p, proxyType='SOCKS5')) for p in selected]
    preview = copy.deepcopy(cfg)
    planned_names = {n['name'] for n in planned}
    preview['nodes'] = [n for n in preview['nodes'] if n['name'] not in planned_names] + planned
    preview['default'] = planned[default]['name']
    isp.Manager(state, surge).apply(copy.deepcopy(binding), preview,
        initialize=not binding.get('fingerprint') or not binding.get('enabled'), dry_run=True)
    nodes = planned if dry_run else ensure_socks5(api, selected)
    imported = {n['name'] for n in nodes}
    if any(n['name'] in imported for n in cfg['nodes']):
        if dry_run:
            print('[isp] Preview would update selected PC-ISP nodes by ID')
        else:
            confirm('Replace existing nodes with matching PC-ISP IDs? Other nodes will be preserved')
    cfg['nodes'] = [n for n in cfg['nodes'] if n['name'] not in imported] + nodes
    cfg['default'] = nodes[default]['name']
    isp.validate_settings(cfg)
    isp.Manager(state, surge).apply(copy.deepcopy(binding), cfg,
        initialize=not binding.get('fingerprint') or not binding.get('enabled'), dry_run=True)
    print('[isp] Selected nodes:', ', '.join(n['name'] for n in nodes))
    print('[isp] Default:', cfg['default'], '| Profile:', Path(binding['profile']).name, '| Target:', binding['target'])
    if dry_run:
        print('[isp] Preview only: no local settings, remote protocols or Surge state changed')
        return
    confirm('Save private settings, apply/activate this profile, install panel/background sync and test exit? '
            'Existing attached profiles share these settings; connections may be interrupted')
    apply_local(config, state, before, cfg, binding, surge)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=isp.CONFIG)
    parser.add_argument('--state-dir', type=Path, default=isp.STATE)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    try:
        wizard(args.config.expanduser().absolute(), args.state_dir.expanduser().absolute(), args.dry_run)
        return 0
    except (Error, OSError, ValueError, KeyError, TypeError, AttributeError, EOFError, KeyboardInterrupt) as exc:
        print('[isp] ERROR: ' + (str(exc) if isinstance(exc, Error) else
              'Operation stopped; sensitive details withheld. Check local/provider state before retrying.'), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
