"""SDK ABI and parsing tests. All constructed sample data is TEST ONLY."""
import ctypes as c
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    import bridge
except ImportError:
    bridge = None


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(bridge, 'Native Bertec SDK bridge must exist')

    def test_abi_matches_pack8_header(self):
        self.assertEqual(c.sizeof(bridge.AdditionalData), 32)
        self.assertEqual(c.sizeof(bridge.DeviceData), 168)
        self.assertEqual(bridge.Frame.device.offset, 8)
        self.assertEqual(c.sizeof(bridge.StreamControl), 24)
        self.assertEqual(c.sizeof(bridge.DeviceInfo), 616)

    def test_parse_real_channel_names_no_invented_split(self):
        d = bridge.DeviceData()
        d.channelData.count = 3
        d.channelData.data[0:3] = [1, 2, 700]
        d.additionalData.timestamp = 8000
        d.additionalData.frameCounter = 42
        result = bridge.decode_device(d, ['FX', 'FY', 'FZ'])
        self.assertEqual(result['t'], 1.0)
        self.assertEqual(result['fz_n'], 700)
        self.assertIsNone(result['left_n'])
        self.assertIsNone(result['right_n'])
        self.assertEqual(result['frame_counter'], 42)
        self.assertEqual(result['channels']['FY'], 2)

    def test_legacy_timestamp_uses_millisecond_ticks(self):
        d = bridge.DeviceData()
        d.channelData.count = 1
        d.additionalData.timestamp = 4033
        result = bridge.decode_device(d, ['FZ'], timestamp_hz=1000)
        self.assertEqual(result['t'], 4.033)
        self.assertEqual(result['timestamp_ticks'], 4033)

    def test_no_fz_is_rejected(self):
        d = bridge.DeviceData()
        d.channelData.count = 2
        with self.assertRaisesRegex(ValueError, 'FZ'):
            bridge.decode_device(d, ['FX', 'FY'])

    def test_nonfinite_force_rejected(self):
        d = bridge.DeviceData()
        d.channelData.count = 1
        d.channelData.data[0] = float('nan')
        with self.assertRaises(ValueError):
            bridge.decode_device(d, ['FZ'])

    def test_mismatched_channels_rejected(self):
        d = bridge.DeviceData()
        d.channelData.count = 2
        with self.assertRaises(ValueError):
            bridge.decode_device(d, ['FZ'])

    def test_signed_force_not_absolutized(self):
        d = bridge.DeviceData()
        d.channelData.count = 1
        d.channelData.data[0] = -50
        self.assertEqual(bridge.decode_device(d, ['FZ'])['fz_n'], -50)

    def test_real_vendor_sdk_initializes_and_stops(self):
        b = bridge.NativeBridge()
        try:
            b.open()
            self.assertEqual(b.version, '2.57')
            self.assertTrue(b.handle)
            self.assertEqual(b.state, 'searching')
        finally:
            b.close()
        self.assertIsNone(b.handle)

    def test_stream_success_status_is_not_an_error(self):
        from types import SimpleNamespace
        for result in (0, -551):  # vendor BERTEC_STREAM_SUCCESSFUL, not generic failure
            with self.subTest(result=result):
                b = bridge.NativeBridge()
                b.handle = 1
                storage = c.create_string_buffer(c.sizeof(bridge.Frame))
                def info(handle, index, ptr, size):
                    device = c.cast(ptr, c.POINTER(bridge.DeviceInfo)).contents
                    device.channelCount = 1
                    device.channelNames[0].value = b'FZ'
                    device.samplingFreq = 1000
                    device.serial = b'TEST ONLY'
                    return 0
                def allocate(handle, size):
                    c.cast(size, c.POINTER(c.c_size_t)).contents.value = c.sizeof(storage)
                    return c.addressof(storage)
                b.dll = SimpleNamespace(bertec_GetDeviceCount=lambda h:1,
                    bertec_GetDeviceInfo=info, bertec_CanStartDataStream=lambda h,p:0,
                    bertec_StartDataStream=lambda h,p:result,
                    bertec_AllocateReadBufferedData=allocate)
                b._start_stream()
                self.assertTrue(b.streaming)
                self.assertEqual(b.devices[0]['serial'],'TEST ONLY')
                self.assertEqual(b.devices[0]['timestamp_hz'], 1000)
                self.assertFalse(b.devices[0]['has_hardware_timestamp'])
                self.assertTrue(b.buffer)

    def test_stream_failure_is_still_rejected(self):
        from types import SimpleNamespace
        b = bridge.NativeBridge(); b.handle = 1
        def info(handle, index, ptr, size):
            device = c.cast(ptr, c.POINTER(bridge.DeviceInfo)).contents
            device.channelCount = 1; device.channelNames[0].value = b'FZ'
            return 0
        b.dll = SimpleNamespace(bertec_GetDeviceCount=lambda h:1,
            bertec_GetDeviceInfo=info,bertec_CanStartDataStream=lambda h,p:0,
            bertec_StartDataStream=lambda h,p:-552)
        with self.assertRaisesRegex(RuntimeError,'-552'): b._start_stream()
        self.assertFalse(b.streaming)

    def test_missing_dll_fails_honestly(self):
        b = bridge.NativeBridge(Path('not-installed'))
        with self.assertRaisesRegex(RuntimeError, 'SDK'):
            b.open()
        b.close()


if __name__ == '__main__':
    unittest.main()
