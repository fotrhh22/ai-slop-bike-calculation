import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch
from fastapi.testclient import TestClient
from bike.app import create_app
from bike.measurement import Station,Store
from bike.protocol import Reading
from bike.campaign import CampaignSync


class Clock:
    def __init__(self):self.now=0
    def __call__(self):return self.now


class MeasurementTests(unittest.TestCase):
    def setUp(self):
        self.store=Store(':memory:');self.addCleanup(self.store.close)
        self.clock=Clock();self.station=Station(self.store,self.clock)
        self.station.mode='usb';self.station.connected=True
        self.station.ingest(Reading(5,0.11))
        self.entry={'id':str(uuid4()),'participant_code':'CODE1234'}

    def sample(self,time):
        self.clock.now=time;self.station.ingest(Reading(5,0.11))

    def test_100_percent_ends_and_freezes_with_one_outbox_event(self):
        self.station.start('CODE1234',0.02,campaign_entry=self.entry)
        self.sample(0.1);self.sample(1.1);self.assertEqual(self.station.session['status'],'running')
        self.sample(2.1);self.assertEqual(self.station.session['end_reason'],'target_reached')
        value=self.station.session['energy_wh'];self.sample(3.1)
        self.assertEqual(self.station.session['energy_wh'],value)
        self.assertEqual(len(self.store.pending()),1)
        self.assertGreaterEqual(self.station.snapshot()['session']['progress_pct'],100)

    def test_partial_manual_stop_also_enqueues_and_gemini_has_no_target(self):
        self.station.start('CODE1234',0.5,campaign_entry=self.entry)
        self.sample(0.1);self.sample(1.1);self.station.finish()
        self.assertEqual(self.store.pending()[0][1]['end_reason'],'manual_stop')
        self.assertLess(self.station.snapshot()['session']['progress_pct'],100)
        self.station.reset();self.station.start('GEMINI',None,campaign_entry={'id':str(uuid4())})
        self.sample(1.2);self.sample(2.2)
        self.assertEqual(self.station.session['status'],'running')
        self.station.finish();self.assertIsNone(self.station.snapshot()['session']['progress_pct'])
        self.assertEqual(len(self.store.pending()),2)

    def test_restart_recovers_the_same_session_as_interrupted(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'station.sqlite3';store=Store(path);station=Station(store,self.clock)
            station.mode='usb';station.connected=True;station.ingest(Reading(5,0.11))
            session=station.start('CODE',None,campaign_entry=self.entry);store.close()
            restored=Store(path)
            self.assertEqual(restored.pending()[0][0],session['id'])
            self.assertEqual(restored.pending()[0][1]['end_reason'],'interrupted')
            restored.close();again=Store(path)
            self.assertEqual(len(again.pending()),1);again.close()


class SyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_lost_completion_response_reuses_same_id_and_survives_reconnect(self):
        store=Store(':memory:');self.addCleanup(store.close);clock=Clock();station=Station(store,clock)
        station.mode='usb';station.connected=True;station.ingest(Reading(5,0.11))
        entry={'id':str(uuid4()),'participant_code':'GEMINI01','target_wh':None,'energy_basis':'tokens_only'}
        class Cloud:
            def __init__(self):self.print_ids=set();self.ends=0
            def call(self,path,body=None):
                if path.endswith('/claim'):return {'entry':{**entry,'bike_session_id':body['session_id']}}
                if path=='/entries':return {'entries':[entry]}
                self.ends+=1;self.print_ids.add(body['session_id'])
                if self.ends==1:raise OSError('lost response after commit')
                return {'result':{'id':body['session_id']},'print_queued':True}
        cloud=Cloud();sync=CampaignSync(store,station,cloud)
        await sync.sync_once();await sync.select(entry['id']);await sync.start()
        clock.now=.1;station.ingest(Reading(5,.11));clock.now=1.1;station.ingest(Reading(5,.11));station.finish()
        await sync.sync_once();self.assertEqual(len(store.pending()),1)
        await sync.sync_once();self.assertEqual(len(store.pending()),0)
        self.assertEqual(len(cloud.print_ids),1)
        self.assertEqual(sync.snapshot()['entries'],[])


class EnvironmentTests(unittest.TestCase):
    def test_env_file_db_path_is_loaded_before_opening_store(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'custom.sqlite3'
            env=Path(temp)/'.env.local'
            env.write_text('BIKE_DB_PATH='+str(path)+'\n')
            with patch.dict(os.environ,{'BIKE_ENV_FILE':str(env),'BIKE_API_BASE':''}):
                os.environ.pop('BIKE_DB_PATH',None)
                with TestClient(create_app()):
                    self.assertTrue(path.is_file())


if __name__=='__main__':unittest.main()
