"""Opt-in real-model check: resolve a follow-up and edit a disposable source folder.
Run: python3 -m tests.live_workspace_resolution --live --model codex/MODEL
"""
import argparse
import http.client,json,tempfile
from pathlib import Path
from unittest.mock import patch
from tests.helpers import RunningRuntime
from umbrous.history.store import HistoryStore

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--model', required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='umbrous-resolve-live-') as tmp, RunningRuntime(codex_enabled=True,ollama_url='http://127.0.0.1:1') as rt:
        root=Path(tmp)/'Umbrous';root.mkdir();(root/'status.txt').write_text('Waiting\n')
        model=args.model
        probe,_=rt.runtime.router.probe(model)
        assert probe.passed,probe.detail
        cid=rt.new_chat()
        HistoryStore(rt.home/'umbrous.db').append_exchange(cid,'How would you improve Umbrous in this small fixture?',
            'I would change status.txt from Waiting to Ready. No commands or other files are needed.',model,'complete')
        with patch('umbrous.workspace_resolution.discover',return_value=[{'root':str(root),'name':'Umbrous','umbrous_source':True}]):
            conn=http.client.HTTPConnection('127.0.0.1',rt.port,timeout=300)
            conn.request('POST','/api/chat',json.dumps({'chat_id':cid,'model':model,'reasoning_effort':'low','use_memory':False,'message':'Do that improvement now.'}),{'Content-Type':'application/json','Origin':rt.origin,'X-Umbrous-Token':rt.policy.session_token})
            response=conn.getresponse();assert response.status==200
            events=[json.loads(line) for line in response.read().splitlines()];conn.close()
        assert (root/'status.txt').read_text().strip()=='Ready', events[-1]
        assert next(e for e in events if e['type'] == 'start')['tools']['selected']['inferred']
        assert events[-1]['report']['changes']
        print('PASS: real Codex resolved a follow-up request and edited the selected disposable folder in the same turn')

if __name__ == '__main__':
    main()
