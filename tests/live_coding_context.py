"""Opt-in real Codex check in disposable files: edit, compact, verify, report."""
import argparse
import http.client
import json
import tempfile
from pathlib import Path
from tests.helpers import RunningRuntime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--model', required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='umbrous-context-live-') as tmp, RunningRuntime(
            codex_enabled=True, ollama_url='http://127.0.0.1:1', context_tokens=4096,
            reply_tokens=768, memory_suggestions=False) as rt:
        root = Path(tmp)
        (root/'value.txt').write_text('old\n')
        (root/'test_value.py').write_text("import unittest\nfrom pathlib import Path\nclass Check(unittest.TestCase):\n def test_value(self):\n  self.assertEqual(Path('value.txt').read_text().strip(), 'new')\n")
        for i in range(4):
            (root/f'notes{i}.txt').write_text(('Historical reference material. No further changes requested. ' * 8 + '\n') * 30)
        rt.runtime.access.attach('prj_general', str(root), 'workspace_code')
        probe, _ = rt.runtime.router.probe(args.model)
        assert probe.passed, probe.detail
        cid = rt.new_chat()
        conn = http.client.HTTPConnection('127.0.0.1', rt.port, timeout=300)
        conn.request('POST', '/api/chat', json.dumps({'chat_id':cid,'model':args.model,'reasoning_effort':'low','use_memory':False,
            'message':'In this disposable fixture: first change value.txt from old to new. Then read notes0.txt through notes3.txt, once each, for reference. Finally run python3 -m unittest test_value.py. Report the confirmed change and test result. Do not edit any other file.'}),
            {'Content-Type':'application/json','Origin':rt.origin,'X-Umbrous-Token':rt.policy.session_token})
        response = conn.getresponse()
        assert response.status == 200
        events = [json.loads(line) for line in response.read().splitlines()]
        conn.close()
        final = events[-1]
        assert final['type'] == 'done', final
        report = final['report']
        assert (root/'value.txt').read_text().strip() == 'new'
        assert len(report['changes']) == 1, report['changes']
        assert any(c['exit'] == 0 and 'unittest' in c['command'] for c in report['commands']), report['commands']
        print(json.dumps({'result':'PASS', 'changes':report['changes'], 'commands':report['commands'],
                          'steps':report['steps'], 'reply':''.join(e['text'] for e in events if e['type']=='delta')}))

if __name__ == '__main__':
    main()
