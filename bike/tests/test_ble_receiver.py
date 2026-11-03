import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bike.measurement import StationError
from bike.receiver import Receiver


class BLESearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_permission_denial_is_kept_in_device_status_and_scan_can_retry(self):
        sink = SimpleNamespace(mode='ble', message='')
        receiver = Receiver(sink)
        scanner = SimpleNamespace(discover=AsyncMock(side_effect=Exception(
            'BLE is not authorized - check macOS privacy settings')))
        with patch.object(receiver, 'bleak', return_value=(None, scanner)):
            with self.assertRaisesRegex(StationError, 'Mac Bluetooth 권한'):
                await receiver.scan()
            self.assertFalse(receiver.busy)
            self.assertIn('서버를 실행한 앱', sink.message)
            scanner.discover.side_effect = None
            scanner.discover.return_value = [SimpleNamespace(address='test-fnb-id', name='FNB58-099544')]
            devices = await receiver.scan()
            self.assertEqual(devices, [{'id': 'test-fnb-id', 'name': 'FNB58-099544'}])
            self.assertIn('선택', sink.message)
            self.assertFalse(receiver.busy)

    async def test_other_scan_failure_is_not_left_as_searching(self):
        sink = SimpleNamespace(mode='ble', message='')
        receiver = Receiver(sink)
        scanner = SimpleNamespace(discover=AsyncMock(side_effect=Exception('Bluetooth device is turned off')))
        with patch.object(receiver, 'bleak', return_value=(None, scanner)):
            with self.assertRaises(StationError):
                await receiver.scan()
        self.assertIn('BLE 검색 실패', sink.message)
        self.assertFalse(receiver.busy)
