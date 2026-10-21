import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('wizard', ROOT/'scripts/proxy-cheap-setup.py')
w = importlib.util.module_from_spec(spec); spec.loader.exec_module(w)


def proxy(i=1, protocol='SOCKS5'):
    return dict(id=i, status='ACTIVE', networkType='RESIDENTIAL_STATIC', proxyType=protocol,
                countryCode='US', connection=dict(connectIp='192.0.2.1', publicIp='192.0.2.2', socks5Port=1080),
                authentication=dict(username='fixture-user', password='fixture-password'))


class APIContracts(unittest.TestCase):
    def test_envelopes(self):
        for payload in [[proxy()], {'proxies':[proxy()]}]:
            api=w.API('key','secret')
            with mock.patch.object(api,'request',return_value=payload):
                self.assertEqual(api.proxies()[0]['id'],1)

    def test_bad_envelope_duplicate_pagination(self):
        for payload in [{}, {'proxies':None}, {'proxies':[proxy(),proxy()]},
                        {'proxies':[proxy()], 'next':'page2'}, [dict(id='../bad')]]:
            api=w.API('key','secret')
            with mock.patch.object(api,'request',return_value=payload), self.assertRaises(w.Error):api.proxies()

    def test_credentials_stdin_not_argv_no_redirect_no_config_file(self):
        api=w.API('key','secret')
        with mock.patch.object(w.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='{"proxies":[]}\n200')) as run:
            self.assertEqual(api.proxies(),[])
        args,kw=run.call_args
        self.assertNotIn('secret',str(args));self.assertNotIn('key',str(args))
        self.assertIn('X-Api-Secret: secret',kw['input'])
        self.assertEqual(args[0][:2],['curl','--disable'])
        self.assertNotIn('--location',args[0]);self.assertNotIn('shell',kw)

    def test_headers_preserve_unicode_and_escape_quotes_and_backslashes(self):
        key='clé"\\key';secret='密钥\\secret"'
        with mock.patch.object(w.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='{"proxies":[]}\n200')) as run:
            w.API(key,secret).proxies()
        config=run.call_args.kwargs['input']
        for header in ('X-Api-Key: '+key,'X-Api-Secret: '+secret):
            self.assertIn('header = '+json.dumps(header,ensure_ascii=False),config)
        self.assertNotIn(key,str(run.call_args.args))
        self.assertNotIn(secret,str(run.call_args.args))

    def test_no_purchase_or_unknown_write(self):
        api=w.API('key','secret')
        for path,payload in [('/order/execute',{}),('/proxies/1/change-protocol',{'newType':'HTTP'}),('/proxies',{})]:
            with self.assertRaises(w.Error):api.request(path,payload)

    def test_error_redaction(self):
        for result in [SimpleNamespace(returncode=0,stdout='fixture-secret\n403'),
                       SimpleNamespace(returncode=0,stdout='fixture-secret\n302'),
                       SimpleNamespace(returncode=0,stdout='fixture-secret\n200'),
                       SimpleNamespace(returncode=7,stdout='fixture-secret')]:
            with mock.patch.object(w.subprocess,'run',return_value=result):
                with self.assertRaises(w.Error) as caught:w.API('key','secret').proxies()
                self.assertNotIn('fixture-secret',str(caught.exception))

    def test_timeout_redaction_and_header_rejection(self):
        with mock.patch.object(w.subprocess,'run',side_effect=subprocess.TimeoutExpired('fixture-secret',35)):
            with self.assertRaises(w.Error) as c:w.API('key','secret').proxies()
            self.assertNotIn('fixture-secret',str(c.exception))
        for key in ['', 'key\nInjected: yes']:
            with self.assertRaises(w.Error):w.API(key,'secret')

    def test_node_mapping_and_validation(self):
        n=w.to_node(proxy());self.assertEqual(n['server'],'192.0.2.1');self.assertEqual(n['expected_ip'],'192.0.2.2')
        self.assertEqual(n['protocol'],'socks5');self.assertEqual(n['name'],'PC-ISP-1')
        for path,value in [('status','PENDING'),('proxyType','HTTP'),('networkType','RESIDENTIAL')]:
            p=proxy();p[path]=value
            with self.assertRaises(w.Error):w.to_node(p)
        for section,key,value in [('connection','socks5Port',0),('authentication','password','')]:
            p=proxy();p[section][key]=value
            with self.assertRaises(w.Error):w.to_node(p)

    def test_existing_socks5_no_post(self):
        api=mock.Mock();api.proxies.return_value=[proxy()]
        self.assertEqual(len(w.ensure_socks5(api,[proxy()])),1);api.request.assert_not_called()

    def test_switch_authorized_then_refetch(self):
        api=mock.Mock();api.proxies.return_value=[proxy()]
        api.request.side_effect=[{'currentType':'HTTP','availableTypes':['SOCKS5']},{},{'currentType':'SOCKS5'}]
        with mock.patch.object(w,'confirm') as confirm:w.ensure_socks5(api,[proxy(protocol='HTTP')])
        confirm.assert_called_once();self.assertEqual(api.request.call_args_list[1].args,('/proxies/1/change-protocol',{'newType':'SOCKS5'}))

    def test_ambiguous_write_reconciles_without_repeat(self):
        api=mock.Mock();api.proxies.return_value=[proxy()]
        api.request.side_effect=[{'currentType':'HTTP','availableTypes':['SOCKS5']},w.Error('timeout'),{'currentType':'SOCKS5'}]
        with mock.patch.object(w,'confirm'),redirect_stdout(io.StringIO()):w.ensure_socks5(api,[proxy(protocol='HTTP')])
        self.assertEqual(len([c for c in api.request.call_args_list if len(c.args)==2]),1)

    def test_cancel_and_unsupported_no_write(self):
        for info in [{'availableTypes':['HTTP']},{'currentType':'HTTP','availableTypes':['SOCKS5']}]:
            api=mock.Mock();api.request.return_value=info
            with mock.patch.object(w,'confirm',side_effect=w.Error('cancel')),self.assertRaises(w.Error):
                w.ensure_socks5(api,[proxy(protocol='HTTP')])
            self.assertEqual(api.request.call_count,1);api.proxies.assert_not_called()

    def test_multiselect_validation(self):
        with mock.patch('builtins.input',side_effect=['0','x','1,2,1']),redirect_stdout(io.StringIO()):
            self.assertEqual(w.choose('pick',['a','b'],True),[0,1])
        with mock.patch('builtins.input',return_value='q'),self.assertRaises(w.Error),redirect_stdout(io.StringIO()):
            w.choose('pick',['a'])


SOURCE='''#!MANAGED-CONFIG https://subscription.example/config
[Proxy]
US1 = socks5, 192.0.2.3, 1080
[Proxy Group]
Claude = select, US1
[Rule]
FINAL,Claude
'''


class WizardContracts(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.config=self.root/'config/isp.json';self.state=self.root/'state'
        self.binding=dict(profile=str(self.root/'Provider-ISP.conf'),source=str(self.root/'Provider.conf'),target='Claude')

    def run_wizard(self,dry=False,confirm_effect=None,protocol="SOCKS5"):
        api=mock.Mock();api.proxies.return_value=[proxy(protocol=protocol)]
        surge=mock.Mock();surge.status.return_value={'core-version':6008000}
        with mock.patch.object(w.sys,'platform','darwin'),mock.patch.object(w.sys.stdin,'isatty',return_value=True),\
             mock.patch.object(w.shutil,'which',return_value='/usr/bin/curl'),mock.patch.object(w.isp,'Surge',return_value=surge),\
             mock.patch.object(w,'profile_choices',return_value=[(Path('Provider.conf'),self.binding,SOURCE,['Claude'])]),\
             mock.patch.object(w.getpass,'getpass',side_effect=['key','secret']),mock.patch.object(w,'API',return_value=api),\
             mock.patch.object(w,'choose',side_effect=[[0],0,0,0,[0]]),mock.patch.object(w,'confirm',side_effect=confirm_effect),\
             mock.patch.object(w.isp,'Manager'),mock.patch.object(w,'apply_local') as apply,redirect_stdout(io.StringIO()):
            w.wizard(self.config,self.state,dry)
        return apply,api

    def test_full_wizard_wiring(self):
        apply,api=self.run_wizard()
        apply.assert_called_once();cfg=apply.call_args.args[3]
        self.assertEqual(cfg['default'],'PC-ISP-1');self.assertEqual(cfg['entry_regex'],'^(?:US1)$')
        self.assertEqual(cfg['nodes'][0]['protocol'],'socks5')
        self.assertNotIn('secret',json.dumps(cfg))

    def test_preview_no_write(self):
        apply,api=self.run_wizard(True);apply.assert_not_called();api.request.assert_not_called()
        self.assertFalse(self.config.exists());self.assertFalse(self.state.exists())

    def test_declining_replacement_does_not_touch_remote_protocol(self):
        cfg=w.isp.defaults();cfg.update(nodes=[w.to_node(proxy())],default='PC-ISP-1')
        w.isp.save_json(self.config,cfg)
        before=self.config.read_bytes()
        with mock.patch.object(w,'ensure_socks5',return_value=[w.to_node(proxy())]) as ensure:
            with self.assertRaisesRegex(w.Error,'replacement declined'):
                self.run_wizard(confirm_effect=w.Error('replacement declined'),protocol='HTTP')
            ensure.assert_not_called()
        self.assertEqual(self.config.read_bytes(),before)
        self.assertFalse(self.state.exists())

    def test_new_profile_preview_with_real_manager(self):
        source=self.root/'Provider.conf';source.write_text(SOURCE)
        binding=w.isp.new_binding(source,'Claude')
        cfg=w.isp.defaults();cfg.update(nodes=[w.to_node(proxy())],default='PC-ISP-1',entry_regex='^US1$')
        with redirect_stdout(io.StringIO()):
            w.isp.Manager(self.state,mock.Mock()).apply(binding,cfg,initialize=True,dry_run=True)
        self.assertFalse(Path(binding['profile']).exists())
        self.assertFalse(self.state.exists())

    def test_apply_activation_panel_watcher_check(self):
        cfg=w.isp.defaults();cfg.update(nodes=[w.to_node(proxy())],default='PC-ISP-1')
        surge=mock.Mock();surge.status.return_value={'profile-path':self.binding['profile']}
        with mock.patch.object(w.isp,'Manager') as manager,mock.patch.object(w.isp,'install_isp_panel') as panel,\
             mock.patch.object(w.isp,'install_watcher') as watcher,mock.patch.object(w.isp,'check_claude_connectivity') as check,\
             redirect_stdout(io.StringIO()):
            w.apply_local(self.config,self.state,None,cfg,self.binding,surge)
        panel.assert_called_once();watcher.assert_called_once();check.assert_called_once()
        self.assertEqual(self.config.stat().st_mode & 0o777,0o600)
        self.assertEqual(manager.return_value.apply.call_count,3)

    def test_real_manager_end_to_end_with_fake_surge(self):
        fixture_spec=importlib.util.spec_from_file_location('isp_test_fixture',ROOT/'tests/surge_isp_test.py')
        fixture=importlib.util.module_from_spec(fixture_spec);fixture_spec.loader.exec_module(fixture)
        source=self.root/'Provider.conf';source.write_text(SOURCE)
        binding=w.isp.new_binding(source,'Claude')
        cfg=w.isp.defaults();cfg.update(nodes=[w.to_node(proxy())],default='PC-ISP-1',entry_regex='^US1$')
        surge=fixture.FakeSurge(source)
        def run(*args):
            self.assertEqual(args[:2],('profile','switch'))
            surge.profile=str(self.root/(args[2]+'.conf'))
        surge.run=run
        output=io.StringIO()
        with mock.patch.object(w.isp,'install_isp_panel') as panel, mock.patch.object(w.isp,'install_watcher') as watcher, \
             mock.patch.object(w.isp,'check_claude_connectivity') as check,redirect_stdout(output):
            w.apply_local(self.config,self.state,None,cfg,binding,surge)
        self.assertEqual(source.read_text(),SOURCE)
        self.assertEqual(surge.profile,binding['profile'])
        self.assertEqual(surge.selections['Claude'],'Custom-ISP')
        self.assertEqual(surge.selections['Custom-ISP'],'PC-ISP-1')
        self.assertTrue(Path(binding['generated']).exists())
        self.assertTrue(binding['runtime_initialized'])
        self.assertFalse(binding['pending_activation'])
        self.assertIn('username="fixture-user"',Path(binding['generated']).read_text())
        self.assertNotIn('fixture-password',output.getvalue())
        panel.assert_called_once();watcher.assert_called_once();check.assert_called_once()

    def test_apply_failure_restores_config(self):
        cfg=w.isp.defaults();cfg.update(nodes=[w.to_node(proxy())],default='PC-ISP-1')
        with mock.patch.object(w.isp,'Manager') as manager,redirect_stdout(io.StringIO()):
            manager.return_value.apply.side_effect=[None,w.Error('failed')]
            with self.assertRaises(w.Error):w.apply_local(self.config,self.state,None,cfg,self.binding,mock.Mock())
        self.assertFalse(self.config.exists())

    def test_concurrent_config_change_rejected(self):
        self.config.parent.mkdir();self.config.write_text('{}')
        with self.assertRaises(w.Error):w.apply_local(self.config,self.state,None,{},self.binding,mock.Mock())
        self.assertEqual(self.config.read_text(),'{}')

    def test_verification_failure_reports_applied(self):
        cfg=w.isp.defaults();cfg.update(nodes=[w.to_node(proxy())],default='PC-ISP-1')
        surge=mock.Mock();surge.status.return_value={'profile-path':self.binding['profile']}
        with mock.patch.object(w.isp,'Manager'),mock.patch.object(w.isp,'install_isp_panel'),\
             mock.patch.object(w.isp,'install_watcher'),mock.patch.object(w.isp,'check_claude_connectivity',side_effect=w.Error('bad exit')),\
             redirect_stdout(io.StringIO()),self.assertRaisesRegex(w.Error,'Settings applied'):
            w.apply_local(self.config,self.state,None,cfg,self.binding,surge)
        self.assertTrue(self.config.exists())


if __name__=='__main__':unittest.main()
