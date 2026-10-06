#!/usr/bin/env python3
"""Local routing and fenced delegation; desktop delivery remains an app-tool operation."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

from mailbox import CONFIG, Mailbox, identifier, now, read_json, unpack, write_json


def canonical(value):
    text = str(value)
    if text.startswith('\\\\?\\UNC\\'):
        text = '\\\\' + text[8:]
    elif text.startswith('\\\\?\\'):
        text = text[4:]
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ValueError('An absolute path is required')
    return path.resolve()


def inside(child, parent):
    return canonical(child).is_relative_to(canonical(parent))


def timestamp(value):
    number = float(value or 0)
    return number / 1000 if number > 100000000000 else number


class Router:
    def __init__(self, config_path):
        self.config_path = Path(config_path).expanduser().resolve()
        self.config = read_json(self.config_path)
        self.mailbox = Mailbox(self.config['mailbox_root'])
        self.routes_path = self.config_path.parent / 'routes.json'
        self.directory = self.mailbox.root / '.routing'
        self.dispatches = self.directory / 'dispatches'

    def routes(self):
        if not self.routes_path.exists():
            return {'schema_version': 1, 'enabled': False, 'routes': []}
        data = read_json(self.routes_path)
        if data.get('schema_version') != 1 or not isinstance(data.get('routes'), list):
            raise ValueError('Invalid routes.json; do not dispatch')
        return data

    def task(self, task_id):
        state, folder = self.mailbox.find(task_id)
        return self.mailbox.describe(state, folder), folder

    def path(self, task_id, attempt):
        identifier(task_id)
        return self.dispatches / f'{task_id}.attempt-{int(attempt)}.json'

    def creation_path(self, target):
        key = json.dumps([str(canonical(target['project_root'])).casefold(),
                          target.get('scope', '').casefold(), target.get('topic', '').casefold()], ensure_ascii=False)
        return self.directory / 'creations' / (hashlib.sha256(key.encode('utf-8')).hexdigest() + '.json')

    def identity(self, thread_id, cwd, target):
        if not thread_id or thread_id != target['thread_id'] or canonical(cwd) != canonical(target['cwd']):
            raise ValueError('Executor thread/cwd does not match verified target')

    def controller(self, thread_id, cwd):
        listener = read_json(self.config_path).get('listener')
        if not listener or canonical(listener['cwd']) != self.mailbox.root:
            raise ValueError('Fixed router binding is missing or inconsistent')
        self.identity(thread_id, cwd, listener)
        return listener

    def resolve(self, task_id, snapshot, topic=None):
        task, folder = self.task(task_id)
        project = canonical(task['project'])
        if not project.is_dir() and task.get('project_mode', 'existing') != 'create':
            raise ValueError('Existing task project is missing')
        projects = [p for p in snapshot['projects'] if p.get('projectKind', 'local') == 'local'
                    and p.get('hostId', 'local') == 'local' and p.get('path') and inside(project, p['path'])]
        if not projects:
            return {'event': 'fallback_required', 'reason': 'project_not_registered', 'task_project': str(project)}
        project_record = max(projects, key=lambda p: len(canonical(p['path']).parts))
        root = canonical(project_record['path'])
        scope = str(project.relative_to(root)) if project != root else ''
        _, body = unpack(folder / 'task.md')
        text = (task['title'] + '\n' + body.split('## 材料')[0]).casefold()
        all_routes = [r for r in self.routes()['routes'] if r.get('enabled', True)
                      and canonical(r['project_root']) == root and r.get('host', 'local') == 'local']
        matching = []
        for route in all_routes:
            route_scope = route.get('scope', '')
            route_topic = route.get('topic', '')
            topic_match = bool(topic and route_topic.casefold() == topic.casefold())
            if not inside(root / route_scope, root):
                raise ValueError('Route scope escapes project root')
            path_match = bool(route_scope and inside(project, root / route_scope) and (not topic or topic_match))
            aliases = route.get('topic_aliases', [])
            alias_match = any(alias.casefold() in text for alias in aliases if alias)
            general = not scope and not topic and not route_scope and not route_topic
            if path_match or topic_match or (not topic and alias_match) or general:
                matching.append(route)
        mapping = max(matching, key=lambda r: len(r.get('scope', ''))) if matching else None
        selected_topic = topic or (mapping or {}).get('topic') or scope or task['title']
        threads = [t for t in snapshot['threads'] if t.get('kind', 'codex') == 'codex'
                   and t.get('hostId', 'local') == 'local' and not t.get('archived', False)
                   and t.get('cwd') and canonical(t['cwd']) == root
                   and t.get('projectId') == project_record['projectId']]
        if mapping and mapping.get('selection') == 'manual':
            target = next((t for t in threads if t['id'] == mapping['thread_id']), None)
            if target is None:
                return {'event': 'fallback_required', 'reason': 'manual_override_invalid', 'task_project': str(project)}
        else:
            aliases = (mapping or {}).get('topic_aliases', []) or [selected_topic]
            eligible = [t for t in threads if t.get('id') == (mapping or {}).get('thread_id')
                        or any(a.casefold() in (t.get('title', '') + '\n' + (t.get('summary') or '')).casefold() for a in aliases if a)]
            target = max(eligible, key=lambda t: timestamp(t.get('updatedAt'))) if eligible else None
        base = {'project_root': str(root), 'app_project_id': project_record['projectId'],
                'scope': (mapping or {}).get('scope', scope), 'topic': selected_topic,
                'task_project': str(project), 'host_id': 'local'}
        if target is None:
            with self.mailbox.lock('routing'):
                path = self.creation_path(base)
                creation = read_json(path) if path.exists() else {}
                if creation.get('phase') != 'pending':
                    creation = {**base, 'phase': 'pending', 'creation_nonce': uuid.uuid4().hex, 'reserved_at': now()}
                    path.parent.mkdir(parents=True, exist_ok=True)
                    write_json(path, creation)
            return {'event': 'create_required', **base, 'creation_nonce': creation['creation_nonce'],
                    'creation_record': str(path)}
        if target.get('status') not in ('idle', 'notLoaded', None):
            return {'event': 'target_busy', **base, 'thread_id': target['id']}
        return {'event': 'resolved', **base, 'thread_id': target['id'], 'cwd': str(root), 'title': target.get('title')}

    def bind_route(self, target, snapshot, selection='auto_latest_matching', aliases=None):
        root = canonical(target['project_root'])
        project = next((p for p in snapshot['projects'] if p.get('projectId') == target['app_project_id']
                        and p.get('path') and canonical(p['path']) == root and p.get('hostId', 'local') == 'local'), None)
        thread = next((t for t in snapshot['threads'] if t['id'] == target['thread_id']), None)
        if not project or not thread or thread.get('archived') or thread.get('hostId', 'local') != 'local':
            raise ValueError('Project/thread must be verified in desktop snapshot')
        if thread.get('projectId') != project['projectId'] or canonical(thread['cwd']) != root:
            raise ValueError('Thread belongs to a different project/cwd')
        if not inside(root / target.get('scope', ''), root):
            raise ValueError('Scope escapes project root')
        with self.mailbox.lock('routing'):
            data = self.routes()
            entry = {'host': 'local', 'project_root': str(root), 'app_project_id': project['projectId'],
                     'scope': target.get('scope', ''), 'topic': target.get('topic', ''),
                     'topic_aliases': aliases or [target.get('topic', '')], 'thread_id': thread['id'],
                     'selection': selection, 'enabled': True, 'last_verified_at': now()}
            data['routes'] = [r for r in data['routes'] if not (canonical(r['project_root']) == root
                             and r.get('scope', '') == entry['scope'] and r.get('topic', '') == entry['topic'])] + [entry]
            write_json(self.routes_path, data)
            creation_path = self.creation_path(entry)
            if creation_path.exists():
                creation = read_json(creation_path)
                creation.update(phase='bound', thread_id=thread['id'], bound_at=now())
                write_json(creation_path, creation)
        return {'event': 'route_bound', 'route': entry}

    def enable(self, value, thread_id, cwd):
        self.controller(thread_id, cwd)
        with self.mailbox.lock('routing'):
            data = self.routes()
            data.update(enabled=value, updated_at=now())
            write_json(self.routes_path, data)
        return {'event': 'routing_enabled' if value else 'routing_disabled'}

    def prepare(self, task_id, worker, target, thread_id, cwd, previous=None, reason=None):
        self.controller(thread_id, cwd)
        with self.mailbox.lock('routing'):
            return self._prepare(task_id, worker, target, previous, reason)

    def _prepare(self, task_id, worker, target, previous=None, reason=None):
        if not self.routes().get('enabled', False):
            raise ValueError('Routing is disabled; do not dispatch')
        task, _ = self.task(task_id)
        if task['state'] != 'running' or task['execution']['worker'] != worker:
            raise ValueError('Task must be running and owned by this claim worker')
        if read_json(self.mailbox.root / 'control.json')['mode'] != 'auto' or task['cancellation']:
            raise ValueError('Mailbox is paused/stopped or task cancelled')
        if not target.get('thread_id') or not target.get('cwd') or target.get('host_id', 'local') != 'local':
            raise ValueError('Verified target identity is required')
        if not target.get('fallback') and not inside(task['project'], target['cwd']):
            raise ValueError('Task project does not belong to target workspace')
        path = self.path(task_id, task['execution']['attempt'])
        if path.exists() and previous is None:
            record = read_json(path)
            if record['target'] != target or record['goal_version'] != task['goal_version']:
                raise ValueError('Existing dispatch differs; inspect/revoke before preparing another')
            return {'event': 'duplicate', **record}
        record = {'task_id': task_id, 'attempt': task['execution']['attempt'], 'goal_version': task['goal_version'],
                  'claim_worker': worker, 'generation': (previous or {}).get('generation', 0) + 1,
                  'dispatch_id': uuid.uuid4().hex, 'phase': 'prepared', 'target': target,
                  'prepared_at': now(), 'fallback_reason': reason,
                  'history': (previous or {}).get('history', []) + ([{k:previous.get(k) for k in ('dispatch_id','generation','phase','target','accepted_at','last_checkpoint','stopped_evidence')}] if previous else [])}
        self.dispatches.mkdir(parents=True, exist_ok=True)
        write_json(path, record)
        return {'event': 'prepared', **record}

    def _load(self, task_id, token):
        task, folder = self.task(task_id)
        path = self.path(task_id, task['execution']['attempt'])
        record = read_json(path)
        if record['dispatch_id'] != token or record['claim_worker'] != task['execution']['worker']:
            raise ValueError('Stale dispatch or different claim worker')
        return task, folder, path, record

    def accept(self, task_id, token, thread_id, cwd):
        with self.mailbox.lock('routing'):
            task, _, path, record = self._load(task_id, token)
            self.identity(thread_id, cwd, record['target'])
            if task['state'] != 'running' or task['goal_version'] != record['goal_version'] or task['cancellation']:
                raise ValueError('Task is terminal, changed or cancelled; do not execute')
            if read_json(self.mailbox.root / 'control.json')['mode'] != 'auto':
                raise ValueError('Mailbox is not auto; do not start execution')
            if record['phase'] != 'prepared':
                return {'event': 'duplicate', **record}
            record.update(phase='accepted', accepted_at=now(), executor_id='codex-project-'+thread_id)
            write_json(path, record)
            return {'event': 'accepted', **record}

    def check(self, task_id, token, thread_id, cwd, checkpoint=None, goal_version=None):
        with self.mailbox.lock('routing'):
            task, _, path, record = self._load(task_id, token)
            self.identity(thread_id, cwd, record['target'])
            if task['state'] != 'running' or record['phase'] != 'accepted':
                raise ValueError('Executor does not own active work')
            if goal_version is not None:
                if goal_version != task['goal_version']:
                    raise ValueError('Goal acknowledgement does not match current task')
                record['goal_version'] = goal_version
            if checkpoint is not None:
                record.update(last_checkpoint=checkpoint, checkpoint_at=now())
                write_json(path, record)
            event = 'cancelled' if task['cancellation'] else read_json(self.mailbox.root / 'control.json')['mode']
            if event == 'auto':
                event = 'goal_changed' if task['goal_version'] != record['goal_version'] else 'continue'
            return {'event': event, 'dispatch': record, 'task': task}

    def fallback(self, task_id, token, reason, thread_id, cwd, stopped_evidence=None):
        listener = self.controller(thread_id, cwd)
        if not reason.strip():
            raise ValueError('Fallback requires a recorded delivery/recovery reason')
        with self.mailbox.lock('routing'):
            task, _, _, record = self._load(task_id, token)
            if record['phase'] not in ('prepared', 'accepted'):
                raise ValueError('Dispatch is finishing/terminal; reconcile, do not rerun')
            if record['phase'] == 'accepted':
                evidence = stopped_evidence or {}
                if evidence.get('thread_id') != record['target']['thread_id'] or evidence.get('status') != 'idle' or not evidence.get('external_actions_checked') or not evidence.get('checkpoint'):
                    raise ValueError('Accepted work cannot fall back without verified stopped-execution evidence')
                record['stopped_evidence'] = evidence
            target = {'thread_id': listener['thread_id'], 'host_id': listener['host_id'], 'cwd': listener['cwd'], 'fallback': True}
            return self._prepare(task_id, task['execution']['worker'], target, record, reason)

    def finish(self, task_id, token, thread_id, cwd, outcome, result):
        with self.mailbox.lock('routing'):
            task, folder, path, record = self._load(task_id, token)
            self.identity(thread_id, cwd, record['target'])
            text = Path(result).read_text(encoding='utf-8-sig')
            digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
            if task['state'] != 'running':
                if task['state'] == record.get('outcome') == outcome and record.get('result_sha256') == digest and (folder/'result.md').read_text(encoding='utf-8') == text:
                    record.update(phase='finished', finished_at=record.get('finished_at', now()), result_path=str(folder/'result.md'))
                    write_json(path, record)
                    return {'event': 'duplicate', 'dispatch': record, 'task': task}
                raise ValueError('Terminal mailbox result differs from dispatch')
            if record['phase'] not in ('accepted', 'finishing') or record['goal_version'] != task['goal_version']:
                raise ValueError('Finish requires current accepted execution and goal')
            if record['phase'] == 'finishing' and (record.get('result_sha256') != digest or record.get('outcome') != outcome):
                raise ValueError('Finish retry differs from recorded result')
            record.update(phase='finishing', outcome=outcome, result_sha256=digest)
            write_json(path, record)
            try:
                result_data = self.mailbox.finish(task_id, record['claim_worker'], outcome, text)
            except (ValueError, OSError):
                # Validation errors are retryable after reading the effective goal/cancellation.
                if self.task(task_id)[0]['state'] == 'running':
                    record.update(phase='accepted')
                    write_json(path, record)
                raise
            record.update(phase='finished', finished_at=now(), result_path=result_data['result_path'])
            write_json(path, record)
            return {'event': outcome, 'dispatch': record, 'task': result_data}

    def abort(self, task_id, token, thread_id, cwd, result):
        listener = self.controller(thread_id, cwd)
        with self.mailbox.lock('routing'):
            task, _, path, record = self._load(task_id, token)
            if task['state'] != 'running' or not task['cancellation'] or record['phase'] != 'prepared':
                raise ValueError('Abort is only for cancelled, not-yet-accepted work')
            record.update(dispatch_id=uuid.uuid4().hex, generation=record['generation']+1,
                          phase='accepted', accepted_at=now(), goal_version=task['goal_version'],
                          target={'thread_id':listener['thread_id'],'host_id':listener['host_id'],
                                  'cwd':listener['cwd'],'fallback':True}, fallback_reason='cancelled_before_accept')
            write_json(path, record)
        return self.finish(task_id, record['dispatch_id'], thread_id, cwd, 'failed', result)

    def status(self, task_id):
        with self.mailbox.lock('routing'):
            task, folder = self.task(task_id)
            path = self.path(task_id, task['execution'].get('attempt', 0))
            record = read_json(path) if path.exists() else None
            if record and record['phase'] == 'finishing' and task['state'] == record.get('outcome') and task['result_path']:
                digest = hashlib.sha256((folder/'result.md').read_text(encoding='utf-8').encode('utf-8')).hexdigest()
                if digest != record['result_sha256']:
                    raise ValueError('Archived result SHA differs; do not mark dispatch finished')
                record.update(phase='finished', finished_at=now(), result_path=task['result_path'])
                write_json(path, record)
            return {'event': 'status', 'task': task, 'dispatch': record}

    def heartbeat(self, phase, thread_id, cwd):
        listener = self.controller(thread_id, cwd)
        self.directory.mkdir(parents=True, exist_ok=True)
        value = {'phase': phase, 'thread_id': listener['thread_id'], 'worker': 'router', 'updated_at': now()}
        with self.mailbox.lock('routing'):
            write_json(self.directory/'router-state.json', value)
        return {'event': 'router_state', **value}


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=os.environ.get('HERMES_EMAIL_CONFIG', str(CONFIG)))
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('resolve','map','enable','prepare','accept','check','checkpoint','fallback','finish','abort','status','heartbeat'):
        p = sub.add_parser(action)
        if action not in ('map','enable','heartbeat'): p.add_argument('--task-id',required=True)
        if action in ('prepare','accept','check','checkpoint','fallback','finish','abort','enable','heartbeat'):
            p.add_argument('--thread-id',default=os.environ.get('CODEX_THREAD_ID'))
            p.add_argument('--cwd',default=str(Path.cwd()))
        if action in ('accept','check','checkpoint','fallback','finish','abort'): p.add_argument('--token',required=True)
        if action in ('resolve','map'): p.add_argument('--snapshot',required=True)
        if action == 'resolve': p.add_argument('--topic')
        if action in ('prepare','map'): p.add_argument('--target',required=True)
        if action == 'prepare': p.add_argument('--worker',required=True)
        if action == 'map':
            p.add_argument('--selection',choices=['manual','auto_latest_matching'],default='auto_latest_matching')
            p.add_argument('--aliases',nargs='*')
        if action == 'enable': p.add_argument('--value',choices=['on','off'],required=True)
        if action == 'checkpoint':
            p.add_argument('--note',required=True);p.add_argument('--goal-version')
        if action == 'fallback':
            p.add_argument('--reason',required=True);p.add_argument('--stopped-evidence')
        if action == 'finish':
            p.add_argument('--outcome',choices=['done','blocked','failed'],required=True);p.add_argument('--result',required=True)
        if action == 'abort':p.add_argument('--result',required=True)
        if action == 'heartbeat': p.add_argument('--phase',required=True)
    args=parser.parse_args()
    try:
        router=Router(args.config)
        if hasattr(args,'thread_id'):
            actual_id=os.environ.get('CODEX_THREAD_ID')
            if actual_id and args.thread_id!=actual_id:
                raise ValueError('Supplied thread ID differs from current Codex environment')
            if canonical(args.cwd)!=Path.cwd().resolve():
                raise ValueError('Run routing commands from the verified chat working directory')
        if args.action=='resolve': result=router.resolve(args.task_id,read_json(args.snapshot),args.topic)
        elif args.action=='map': result=router.bind_route(read_json(args.target),read_json(args.snapshot),args.selection,args.aliases)
        elif args.action=='enable':result=router.enable(args.value=='on',args.thread_id,args.cwd)
        elif args.action=='prepare':result=router.prepare(args.task_id,args.worker,read_json(args.target),args.thread_id,args.cwd)
        elif args.action=='accept':result=router.accept(args.task_id,args.token,args.thread_id,args.cwd)
        elif args.action in ('check','checkpoint'):result=router.check(args.task_id,args.token,args.thread_id,args.cwd,Path(args.note).read_text(encoding='utf-8-sig') if args.action=='checkpoint' else None,args.goal_version if args.action=='checkpoint' else None)
        elif args.action=='fallback':result=router.fallback(args.task_id,args.token,args.reason,args.thread_id,args.cwd,read_json(args.stopped_evidence) if args.stopped_evidence else None)
        elif args.action=='finish':result=router.finish(args.task_id,args.token,args.thread_id,args.cwd,args.outcome,args.result)
        elif args.action=='abort':result=router.abort(args.task_id,args.token,args.thread_id,args.cwd,args.result)
        elif args.action=='status':result=router.status(args.task_id)
        else:result=router.heartbeat(args.phase,args.thread_id,args.cwd)
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except (OSError,ValueError,KeyError,TypeError,TimeoutError) as error:
        print(json.dumps({'event':'error','message':str(error)},ensure_ascii=False),file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
