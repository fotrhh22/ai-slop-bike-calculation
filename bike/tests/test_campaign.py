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
    async def test_confirmed_printer_send_automatically_prepares_next_participant(self):
        store=Store(':memory:');self.addCleanup(store.close);clock=Clock();station=Station(store,clock)
        station.mode='usb';station.connected=True;station.ingest(Reading(5,.11))
        entry={'id':str(uuid4()),'participant_code':'PRINT001'}
        session=station.start('PRINT001',.3463,campaign_entry=entry);station.finish()
        store.acknowledge(session['id'])
        from unittest.mock import Mock
        cloud=Mock();cloud.call.return_value={'session_id':session['id'],'job':{
            'bike_session_id':session['id'],'status':'SENT','cups_job_id':'PT210-1'}}
        sync=CampaignSync(store,station,cloud);sync.selected=entry
        await sync.check_print_completion()
        self.assertIsNone(station.session);self.assertIsNone(sync.selected)
        self.assertEqual(sync.last_printed,'PRINT001')
        self.assertEqual(len(store.history()),1)

    async def test_failed_or_unknown_print_does_not_clear_participant(self):
        store=Store(':memory:');self.addCleanup(store.close);clock=Clock();station=Station(store,clock)
        station.mode='usb';station.connected=True;station.ingest(Reading(5,.11))
        session=station.start('KEEPPRINT',.3463,campaign_entry={'id':str(uuid4())});station.finish()
        store.acknowledge(session['id'])
        from unittest.mock import Mock
        cloud=Mock();sync=CampaignSync(store,station,cloud)
        for status in ('PENDING','SENDING','FAILED','UNKNOWN'):
            cloud.call.return_value={'session_id':session['id'],'job':{
                'bike_session_id':session['id'],'status':status,'cups_job_id':None}}
            await sync.check_print_completion()
            self.assertIs(station.session,session)

    async def test_delete_removes_cloud_entry_selection_cache_and_start_intent(self):
        store=Store(':memory:');self.addCleanup(store.close);station=Station(store)
        entry={'id':str(uuid4()),'participant_code':'DELETE01','state':'waiting'}
        class Cloud:
            def call(self,path,body=None):
                if path.endswith('/delete'):return {'deleted':True}
                return {'entries':[]}
        sync=CampaignSync(store,station,Cloud())
        store.cache('entries',[entry]);old_intent=store.intent(entry['id']);sync.selected=entry
        self.assertTrue((await sync.delete(entry['id']))['deleted'])
        self.assertEqual(sync.snapshot()['entries'],[]);self.assertIsNone(sync.selected)
        self.assertNotEqual(store.intent(entry['id']),old_intent)

    async def test_delete_failure_preserves_local_data_and_measuring_entry_is_protected(self):
        store=Store(':memory:');self.addCleanup(store.close);station=Station(store)
        entry={'id':str(uuid4()),'participant_code':'KEEP0001','state':'waiting'}
        from unittest.mock import Mock
        cloud=Mock();cloud.call.side_effect=OSError('offline')
        sync=CampaignSync(store,station,cloud);store.cache('entries',[entry]);sync.selected=entry
        from bike.measurement import StationError
        with self.assertRaises(StationError):await sync.delete(entry['id'])
        self.assertEqual(sync.snapshot()['entries'],[entry]);self.assertEqual(sync.selected,entry)
        store.cache('entries',[{**entry,'state':'measuring'}]);cloud.reset_mock()
        with self.assertRaises(StationError):await sync.delete(entry['id'])
        cloud.call.assert_not_called()

    async def test_refresh_cannot_restore_stale_entry_after_delete(self):
        store=Store(':memory:');self.addCleanup(store.close);station=Station(store)
        entry={'id':str(uuid4()),'participant_code':'RACE0001','state':'waiting'}
        from threading import Event
        requested,release=Event(),Event()
        class Cloud:
            def call(self,path,body=None):
                if path=='/entries':
                    requested.set();release.wait(2);return {'entries':[entry]}
                return {'deleted':True}
        sync=CampaignSync(store,station,Cloud());store.cache('entries',[entry])
        refresh=asyncio.create_task(sync.sync_once())
        self.assertTrue(await asyncio.to_thread(requested.wait,1))
        delete=asyncio.create_task(sync.delete(entry['id']))
        try:
            await asyncio.wait_for(delete,1)
        finally:
            release.set()
        await refresh
        self.assertEqual(sync.snapshot()['entries'],[])

    async def test_lost_completion_response_reuses_same_id_and_survives_reconnect(self):
        store=Store(':memory:');self.addCleanup(store.close);clock=Clock();station=Station(store,clock)
        station.mode='usb';station.connected=True;station.ingest(Reading(5,0.11))
        entry={'id':str(uuid4()),'participant_code':'GEMINI01','target_wh':None,'energy_basis':'tokens_only','reference_target_wh':0.3463}
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
        self.assertEqual(station.session['target_wh'],0.3463)
        clock.now=.1;station.ingest(Reading(5,.11));clock.now=1.1;station.ingest(Reading(5,.11));station.finish()
        await sync.sync_once();self.assertEqual(len(store.pending()),1)
        await sync.sync_once();self.assertEqual(len(store.pending()),0)
        self.assertEqual(len(cloud.print_ids),1)
        self.assertEqual(sync.snapshot()['entries'],[])

    async def test_gemini_fixed_reference_automatically_stops_at_100_percent(self):
        store=Store(':memory:');self.addCleanup(store.close);clock=Clock();station=Station(store,clock)
        station.mode='usb';station.connected=True;station.ingest(Reading(5,.11))
        entry={'id':str(uuid4()),'participant_code':'FIXED001','energy_basis':'tokens_only',
            'target_wh':None,'reference_target_wh':0.3463}
        class Cloud:
            def call(self,path,body=None):return {'entry':{**entry,'bike_session_id':body['session_id']}}
        sync=CampaignSync(store,station,Cloud());store.cache('entries',[entry]);await sync.select(entry['id'])
        await sync.start()
        for i in range(25):
            clock.now=.1+i;station.ingest(Reading(5,.11))
        self.assertEqual(station.session['end_reason'],'target_reached')
        self.assertGreaterEqual(station.snapshot()['session']['progress_pct'],100)
        self.assertEqual(len(store.pending()),1)

    async def test_waiting_list_orders_newest_first(self):
        store=Store(':memory:');self.addCleanup(store.close);station=Station(store)
        old={'id':'old','created_at':'2026-11-04T00:00:00Z'}
        new={'id':'new','created_at':'2026-11-04T01:00:00Z'}
        store.cache('entries',[old,new])
        self.assertEqual(CampaignSync(store,station).snapshot()['entries'],[new,old])


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
