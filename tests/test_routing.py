import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import routing
from mailbox import Mailbox, read_json, write_json
from routing import Router


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='hermes-routing 测试 ')
        self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)
        self.root=self.base/'邮箱';self.project=self.base/'工作 项目';self.project.mkdir()
        self.scope=self.project/'Research09';self.scope.mkdir()
        self.config=self.base/'config.json'
        write_json(self.config,{'mailbox_root':str(self.root),'listener':{'thread_id':'router','host_id':'local','cwd':str(self.root),'project_id':'mail-project'}})
        self.mail=Mailbox(self.root);self.mail.initialize();self.mail.mode('auto')
        self.router=Router(self.config);self.router.enable(True,'router',self.root)
        self.snapshot={'projects':[{'projectId':'app-project','projectKind':'local','path':str(self.project),'hostId':'local'}],
                       'threads':[{'id':'executor','cwd':str(self.project),'projectId':'app-project','title':'Research09','status':'idle','updatedAt':100},
                                  {'id':'wrong-topic','cwd':str(self.project),'projectId':'app-project','title':'Study01','status':'idle','updatedAt':300}]}
        self.target={'thread_id':'executor','host_id':'local','cwd':str(self.project),'project_root':str(self.project),
                     'app_project_id':'app-project','scope':'Research09','topic':'Research09'}
        self.router.bind_route(self.target,self.snapshot,aliases=['Research09'])
        self.mail.send({'task_id':'one','title':'Research09只读验收','project':str(self.scope),'original_request':'读取当前进度',
                        'instructions':'只读','acceptance':'报告','reply_to':''})
        self.mail.claim('owner')

    def prepare(self):
        return self.router.prepare('one','owner',self.target,'router',self.root)

    def result(self,text='任务完成'):
        p=self.base/'结果.md';p.write_text(text,encoding='utf-8');return p

    def test_routes_choose_latest_matching_not_unrelated_topic(self):
        self.snapshot['threads'].append({'id':'newer','cwd':str(self.project),'projectId':'app-project','title':'Research09新进度',
                                         'status':'idle','updatedAt':2000000000000})
        self.assertEqual(self.router.resolve('one',self.snapshot)['thread_id'],'newer')
        self.assertEqual(self.router.resolve('one',self.snapshot,'新主题')['event'],'create_required')
        first=self.router.resolve('one',self.snapshot,'新主题')
        self.assertEqual(first['creation_nonce'],Router(self.config).resolve('one',self.snapshot,'新主题')['creation_nonce'])
        self.router.bind_route(self.target,self.snapshot,selection='manual')
        self.assertEqual(self.router.resolve('one',self.snapshot)['thread_id'],'executor')
        self.snapshot['threads'][0]['archived']=True
        self.assertEqual(self.router.resolve('one',self.snapshot)['reason'],'manual_override_invalid')

    def test_path_boundaries_wrong_identity_and_unregistered_project(self):
        wrong={**self.target,'cwd':str(self.base)}
        with self.assertRaises(ValueError):self.router.bind_route(wrong,{**self.snapshot,'threads':[{**self.snapshot['threads'][0],'cwd':str(self.base)}]})
        escaped={**self.target,'scope':'../outside'}
        with self.assertRaises(ValueError):self.router.bind_route(escaped,self.snapshot)
        self.assertEqual(self.router.resolve('one',{'projects':[],'threads':[]})['reason'],'project_not_registered')
        record=self.prepare()
        with self.assertRaises(ValueError):self.router.accept('one',record['dispatch_id'],'other',self.project)
        with self.assertRaises(ValueError):self.router.prepare('one','another-worker',self.target,'router',self.root)
        with self.assertRaises(ValueError):self.router.prepare('one','owner',self.target,'someone',self.root)

    def test_executor_finishes_with_original_claim_owner(self):
        d=self.prepare();token=d['dispatch_id']
        self.assertEqual(self.router.accept('one',token,'executor',self.project)['event'],'accepted')
        self.assertEqual(self.router.accept('one',token,'executor',self.project)['event'],'duplicate')
        self.assertEqual(self.router.finish('one',token,'executor',self.project,'done',self.result())['event'],'done')
        self.assertEqual(self.router.finish('one',token,'executor',self.project,'done',self.result())['event'],'duplicate')
        self.assertEqual(read_json(self.mail.find('one')[1]/'execution.json')['worker'],'owner')
        self.assertEqual(set(read_json(self.mail.find('one')[1]/'execution.json')),{'worker','attempt','claimed_at'})

    def test_delivery_failure_fallback_executes_and_late_packet_is_fenced(self):
        original=self.prepare()
        fallback=self.router.fallback('one',original['dispatch_id'],'controlled desktop delivery failure','router',self.root)
        with self.assertRaises(ValueError):self.router.accept('one',original['dispatch_id'],'executor',self.project)
        self.assertEqual(self.router.accept('one',fallback['dispatch_id'],'router',self.root)['event'],'accepted')
        marker=self.base/'fallback-executed.txt';marker.write_text('real fallback branch ran',encoding='utf-8')
        completed=self.router.finish('one',fallback['dispatch_id'],'router',self.root,'done',self.result(marker.read_text()))
        self.assertEqual(completed['task']['state'],'done')
        self.assertEqual(completed['dispatch']['generation'],2)
        self.assertEqual(completed['dispatch']['fallback_reason'],'controlled desktop delivery failure')

    def test_accepted_timeout_cannot_fall_back_without_stopped_proof(self):
        d=self.prepare();self.router.accept('one',d['dispatch_id'],'executor',self.project)
        with self.assertRaises(ValueError):self.router.fallback('one',d['dispatch_id'],'timeout','router',self.root)
        evidence={'thread_id':'executor','status':'idle','external_actions_checked':True,'checkpoint':'verified stopped, no external action'}
        newer=self.router.fallback('one',d['dispatch_id'],'verified stopped','router',self.root,evidence)
        self.assertEqual(newer['history'][-1]['stopped_evidence'],evidence)
        with self.assertRaises(ValueError):self.router.check('one',d['dispatch_id'],'executor',self.project)

    def test_pause_stop_cancel_and_disabled_do_not_start_executor(self):
        d=self.prepare()
        for mode in ['paused','stopped']:
            self.mail.mode(mode)
            with self.assertRaises(ValueError):self.router.accept('one',d['dispatch_id'],'executor',self.project)
        self.mail.mode('auto');self.mail.cancel('one','用户撤回')
        with self.assertRaises(ValueError):self.router.accept('one',d['dispatch_id'],'executor',self.project)
        self.assertEqual(self.router.abort('one',d['dispatch_id'],'router',self.root,self.result('撤回前尚未执行'))['event'],'failed')
        self.router.enable(False,'router',self.root)
        with self.assertRaises(ValueError):self.prepare()

    def test_goal_change_requires_acknowledgement_and_terminal_checkpoint(self):
        d=self.prepare();self.router.accept('one',d['dispatch_id'],'executor',self.project)
        folder=self.mail.find('one')[1];write_json(folder/'goal.json',{'version':'g2'})
        self.assertEqual(self.router.check('one',d['dispatch_id'],'executor',self.project)['event'],'goal_changed')
        with self.assertRaises(ValueError):self.router.finish('one',d['dispatch_id'],'executor',self.project,'done',self.result())
        self.assertEqual(self.router.check('one',d['dispatch_id'],'executor',self.project,'read g2','g2')['event'],'continue')

    def test_post_finish_crash_reconciles_without_running_again(self):
        d=self.prepare();self.router.accept('one',d['dispatch_id'],'executor',self.project)
        actual_write=routing.write_json
        def fail_terminal(path,value):
            if value.get('phase')=='finished':raise OSError('controlled crash after mailbox finish')
            return actual_write(path,value)
        with patch.object(routing,'write_json',side_effect=fail_terminal):
            with self.assertRaises(OSError):self.router.finish('one',d['dispatch_id'],'executor',self.project,'done',self.result())
        self.assertEqual(self.mail.find('one')[0],'done')
        self.assertEqual(self.router.status('one')['dispatch']['phase'],'finished')

    def test_two_process_accept_revoke_race_has_one_winner(self):
        d=self.prepare()
        def launch(action,actor,cwd,extra):
            env=os.environ.copy();env['CODEX_THREAD_ID']=actor;env['PYTHONIOENCODING']='utf-8'
            return subprocess.Popen([sys.executable,'-B',str(ROOT/'scripts/routing.py'),'--config',str(self.config),action,
                                     '--task-id','one','--token',d['dispatch_id'],*extra],cwd=cwd,env=env,
                                    stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
        accept=launch('accept','executor',self.project,[])
        fallback=launch('fallback','router',self.root,['--reason','controlled race'])
        outputs=[]
        for process in [accept,fallback]:
            out,err=process.communicate(timeout=20);outputs.append((process.returncode,out,err))
        self.assertEqual(sum(code==0 for code,_,_ in outputs),1,outputs)
        record=read_json(self.router.path('one',1))
        self.assertTrue(record['phase']=='accepted' or record['target'].get('fallback'))


if __name__=='__main__':unittest.main()
