"""Tests for onvif.py against a local mock ONVIF camera (replies shaped like the V380's).
Run: python3 -m unittest discover -s tests -v. Nothing leaves this machine.
"""
import os
import re
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import onvif  # noqa: E402

REPLY = ('<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
         ' xmlns:tt="http://www.onvif.org/ver10/schema" xmlns:tds="http://www.onvif.org/ver10/device/wsdl"'
         ' xmlns:trt="http://www.onvif.org/ver10/media/wsdl" xmlns:timg="http://www.onvif.org/ver20/imaging/wsdl"'
         ' xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"><s:Body>%s</s:Body></s:Envelope>')
FAULT = ('<s:Fault><s:Code><s:Value>s:Receiver</s:Value></s:Code><s:Reason>'
         '<s:Text xml:lang="en">Action Not Implemented</s:Text></s:Reason></s:Fault>')


class MockCamera(BaseHTTPRequestHandler):
    calls = []        # (path, action)
    ir = "AUTO"
    no_ptz = False

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        action = re.search(r"<s:Body><(\w+)", body).group(1)
        MockCamera.calls.append((self.path, action))
        host = "http://10.9.9.9:8899"  # the camera may name another address; the client must ignore it
        if action == "GetCapabilities":
            ptz = "" if MockCamera.no_ptz else "<tt:PTZ><tt:XAddr>%s/onvif/ptz_service</tt:XAddr></tt:PTZ>" % host
            out = ("<tds:GetCapabilitiesResponse><tds:Capabilities>"
                   "<tt:Imaging><tt:XAddr>%s/onvif/image_service</tt:XAddr></tt:Imaging>"
                   "<tt:Media><tt:XAddr>%s/onvif/media_service</tt:XAddr></tt:Media>%s"
                   "</tds:Capabilities></tds:GetCapabilitiesResponse>" % (host, host, ptz))
        elif action == "GetProfiles":
            out = ('<trt:GetProfilesResponse><trt:Profiles token="PROFILE_000"><tt:Name>PROFILE_000</tt:Name>'
                   '<tt:VideoSourceConfiguration token="V_SRC_CFG_000"><tt:SourceToken>V_SRC_000</tt:SourceToken>'
                   '</tt:VideoSourceConfiguration></trt:Profiles><trt:Profiles token="PROFILE_001"/>'
                   '</trt:GetProfilesResponse>')
        elif action == "GetImagingSettings":
            assert "<VideoSourceToken>V_SRC_000<" in body
            out = ("<timg:GetImagingSettingsResponse><timg:ImagingSettings><tt:Brightness>50</tt:Brightness>"
                   "<tt:IrCutFilter>%s</tt:IrCutFilter></timg:ImagingSettings>"
                   "</timg:GetImagingSettingsResponse>" % MockCamera.ir)
        elif action == "SetImagingSettings":
            MockCamera.ir = re.search(r"<tt:IrCutFilter>(\w+)<", body).group(1)
            out = "<timg:SetImagingSettingsResponse/>"
        elif action in ("ContinuousMove", "Stop"):
            assert "<ProfileToken>PROFILE_000<" in body
            if action == "ContinuousMove":
                MockCamera.calls[-1] = (self.path, action, re.search(r'x="([-\d.]+)" y="([-\d.]+)"', body).groups())
            out = "<tptz:%sResponse/>" % action
        else:
            return self._send(500, REPLY % FAULT)
        self._send(200, REPLY % out)

    def _send(self, status, text):
        data = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/soap+xml")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class OnvifTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockCamera)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d/onvif/device_service" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        MockCamera.calls = []
        MockCamera.ir = "AUTO"
        MockCamera.no_ptz = False
        self.slept = []
        self.cam = onvif.Camera(self.url, timeout=3, sleep=self.slept.append)

    def test_mode_read_and_set(self):
        self.assertEqual(self.cam.mode(), "auto")
        self.cam.set_mode("night")
        self.assertEqual(MockCamera.ir, "OFF")
        self.assertEqual(self.cam.mode(), "night")
        self.cam.set_mode("day")
        self.assertEqual(MockCamera.ir, "ON")
        # Discovery happens once; every service is called on the configured host.
        self.assertEqual([c[1] for c in MockCamera.calls].count("GetCapabilities"), 1)
        self.assertIn(("/onvif/image_service", "SetImagingSettings"), MockCamera.calls)

    def test_step_moves_then_stops(self):
        self.cam.step("left", speed=0.5, seconds=0.5)
        moves = [c for c in MockCamera.calls if c[0] == "/onvif/ptz_service"]
        self.assertEqual(moves, [("/onvif/ptz_service", "ContinuousMove", ("-0.50", "0.00")),
                                 ("/onvif/ptz_service", "Stop")])
        self.assertEqual(self.slept, [0.5])
        MockCamera.calls = []
        self.cam.step("up", speed=0.3, seconds=0.2)
        self.assertEqual(MockCamera.calls[0][2], ("0.00", "0.30"))

    def test_missing_service_and_faults(self):
        MockCamera.no_ptz = True
        with self.assertRaisesRegex(onvif.OnvifError, "no ONVIF ptz service"):
            self.cam.step("right")
        with self.assertRaisesRegex(onvif.OnvifError, "not answering"):
            onvif.Camera("http://127.0.0.1:1/onvif/device_service", timeout=1).mode()

    def test_fault_is_reported(self):
        cam = onvif.Camera(self.url, timeout=3)
        with self.assertRaisesRegex(onvif.OnvifError, "Action Not Implemented"):
            cam._call(self.url, "<GetRelayOutputs/>")



PROBE_MATCH = ('<?xml version="1.0" encoding="UTF-8"?><SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"'
               ' xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">'
               '<SOAP-ENV:Body><d:ProbeMatches><d:ProbeMatch><wsa:EndpointReference>'
               '<wsa:Address>urn:uuid:11111111-2222-3333-4444-00:00:5e:00:53:01</wsa:Address></wsa:EndpointReference>'
               '<d:XAddrs>http://192.168.1.20:8899/onvif/device_service </d:XAddrs></d:ProbeMatch></d:ProbeMatches>'
               '</SOAP-ENV:Body></SOAP-ENV:Envelope>')


class DiscoveryTests(unittest.TestCase):
    def test_probe_match_like_the_v380(self):
        self.assertEqual(onvif.parse_probe_match(PROBE_MATCH.encode()),
                         (["http://192.168.1.20:8899/onvif/device_service"], "00:00:5e:00:53:01"))

    def test_probe_match_without_mac_or_garbage(self):
        plain = PROBE_MATCH.replace("11111111-2222-3333-4444-00:00:5e:00:53:01", "1234abcd-0000-1111-2222-333344445555")
        self.assertEqual(onvif.parse_probe_match(plain.encode())[1], None)
        self.assertEqual(onvif.parse_probe_match(b"not xml"), ([], None))

    def test_normalize_mac(self):
        for v in ("00:00:5E:00:53:01", "00-00-5e-00-53-01", "00005e005301"):
            self.assertEqual(onvif.normalize_mac(v), "00:00:5e:00:53:01")
        self.assertIsNone(onvif.normalize_mac("00:00:5e"))


if __name__ == "__main__":
    unittest.main()
