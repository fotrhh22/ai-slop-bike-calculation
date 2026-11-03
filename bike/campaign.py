"""Outbound-only cloud queue sync. Receiver sampling never waits for HTTP."""
import asyncio
import json
import os
from pathlib import Path
import urllib.error
import urllib.request

from .measurement import StationError


class CampaignClient:
    def __init__(self,base,token):
        if not (base.startswith('https://') or base.startswith('http://127.0.0.1:') or base.startswith('http://localhost:')):
            raise ValueError('BIKE_API_BASE_REQUIRES_HTTPS')
        if len(token)<32: raise ValueError('BIKE_WORKER_TOKEN_REQUIRES_32_CHARACTERS')
        self.base,self.token=base.rstrip('/'),token

    def call(self,path,body=None):
        request=urllib.request.Request(self.base+'/api/campaign'+path,
            data=None if body is None else json.dumps(body).encode(),
            headers={'Content-Type':'application/json','X-Bike-Worker-Token':self.token})
        try:
            with urllib.request.urlopen(request,timeout=12) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise StationError('클라우드 연결 오류 HTTP '+str(exc.code)) from None


def client_from_env():
    env=Path(os.environ.get('BIKE_ENV_FILE',str(Path(__file__).resolve().parents[1]/'.env.local')))
    if env.is_file():
        for line in env.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                key,value=line.split('=',1);os.environ.setdefault(key.strip(),value.strip().strip('"').strip("'"))
    base=os.environ.get('BIKE_API_BASE','').rstrip('/')
    return CampaignClient(base,os.environ.get('BIKE_WORKER_TOKEN','')) if base else None


class CampaignSync:
    def __init__(self,store,station,client=None):
        self.store,self.station,self.client=store,station,client
        self.selected=None
        self.error=''
        self.last_sync=None
        self.starting=False
        self.lock=asyncio.Lock()

    def snapshot(self):
        entries=[e for e in self.store.cached('entries',[]) if not self.store.entry_ended(e['id'])]
        return {'enabled':self.client is not None,'entries':entries,'selected':self.selected,
                'error':self.error,'pending_ends':len(self.store.pending()),'last_sync':self.last_sync,'starting':self.starting}

    async def sync_once(self):
        if self.client is None: return
        try:
            for session_id,payload in self.store.pending():
                result=await asyncio.to_thread(self.client.call,'/bike-ends',payload)
                if result.get('result',{}).get('id') != session_id or not result.get('print_queued'):
                    raise StationError('종료 기록 확인 실패. 같은 기록으로 다시 동기화합니다.')
                self.store.acknowledge(session_id)
            result=await asyncio.to_thread(self.client.call,'/entries')
            self.store.cache('entries',result['entries'])
            from .measurement import utc_now
            self.last_sync=utc_now();self.error=''
        except Exception as exc:
            # Do not expose URLs, headers or credentials in the iPad UI.
            self.error=str(exc) if isinstance(exc,StationError) else '클라우드 연결 대기 · 결과는 Mac에 보관됩니다.'

    async def run(self):
        while True:
            await self.sync_once()
            await asyncio.sleep(3)

    async def select(self,entry_id):
        async with self.lock:
            if self.station.session or self.starting:
                raise StationError('현재 기록을 확인하고 새 참가자를 눌러 주세요.')
            entry=next((e for e in self.snapshot()['entries'] if e['id']==entry_id),None)
            if not entry: raise StationError('대기 참가자를 찾지 못했습니다. 목록을 새로고침해 주세요.')
            if entry['energy_basis']=='unavailable': raise StationError('AI 계측 확인이 필요해 목표를 확정할 수 없습니다.')
            self.selected=entry
            return entry

    async def start(self):
        async with self.lock:
            if self.client is None or self.selected is None: raise StationError('대기 목록에서 참가자를 먼저 선택하세요.')
            if self.station.session: raise StationError('현재 기록을 확인하고 새 참가자를 눌러 주세요.')
            if self.station.mode=='demo': raise StationError('공식 참가자는 USB 또는 Bluetooth 실측으로 시작하세요.')
            if not self.station.fresh(): raise StationError('새 실측값이 수신되는 상태에서 시작하세요.')
            entry_id=self.selected['id']
            session_id=self.store.intent(entry_id)  # retained if the HTTP response is lost
            self.starting=True
            try:
                response=await asyncio.to_thread(self.client.call,'/entries/'+entry_id+'/claim',{'session_id':session_id})
                entry=response['entry']
                if entry['id']!=entry_id or entry['bike_session_id']!=session_id or entry['energy_basis']=='unavailable':
                    raise StationError('참가자 기록 확인 실패')
                target=entry['target_wh'] if entry['energy_basis']=='measured_gpu' else None
                self.selected=entry
                return self.station.start(entry['participant_code'],target,session_id,entry)
            except StationError: raise
            except Exception: raise StationError('시작 연결 확인 실패. 같은 참가자로 다시 시작해 주세요.') from None
            finally:
                self.starting=False
