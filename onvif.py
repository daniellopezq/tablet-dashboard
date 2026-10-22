"""Minimal ONVIF client for local cameras: pan/tilt steps and the day/night (IR-cut filter) mode.

Only what the Cameras tab needs, plus WS-Discovery to find a camera again after its address changed. Plain SOAP 1.2 over HTTP to the camera's device service, which tells
us where its PTZ, media and imaging services are. The V380 answers without a password; cameras that
need WS-Security credentials aren't supported yet. Python 3 standard library only.
"""
import re
import socket
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

ENVELOPE = ('<?xml version="1.0" encoding="UTF-8"?>'
            '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
            ' xmlns:tt="http://www.onvif.org/ver10/schema"><s:Body>%s</s:Body></s:Envelope>')
DEVICE_NS = "http://www.onvif.org/ver10/device/wsdl"
MEDIA_NS = "http://www.onvif.org/ver10/media/wsdl"
PTZ_NS = "http://www.onvif.org/ver20/ptz/wsdl"
IMAGING_NS = "http://www.onvif.org/ver20/imaging/wsdl"
MAX_REPLY = 256 * 1024

# Dashboard words <-> ONVIF IrCutFilter: the filter in (ON) is the colour day picture, out (OFF) is
# infrared night vision.
MODES = {"day": "ON", "night": "OFF", "auto": "AUTO"}
DIRECTIONS = {"left": (-1, 0), "right": (1, 0), "up": (0, 1), "down": (0, -1)}


PROBE = ('<?xml version="1.0" encoding="UTF-8"?><e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"'
         ' xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"'
         ' xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
         '<e:Header><w:MessageID>uuid:%s</w:MessageID>'
         '<w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
         '<w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>'
         '</e:Header><e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>')
DISCOVERY_ADDR = ("239.255.255.250", 3702)
# Only a MAC written with separators: a UUID's last group is 12 hex digits too.
MAC_RE = re.compile(r"([0-9a-f]{2}([:-])[0-9a-f]{2}(?:\2[0-9a-f]{2}){4})", re.I)


def normalize_mac(value):
    """'00-00-5E-00-53-01' or '00005e005301' -> '00:00:5e:00:53:01' (None if it isn't a MAC)."""
    digits = re.sub(r"[^0-9a-f]", "", (value or "").lower())
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2)) if len(digits) == 12 else None


def parse_probe_match(data):
    """(device service URLs, MAC from the endpoint address or None) from one WS-Discovery reply."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return [], None
    xaddrs = (_text(root, "XAddrs") or "").split()
    endpoint = _text(_first(root, "EndpointReference") or root, "Address") or ""
    m = MAC_RE.search(endpoint)
    return [x for x in xaddrs if x.startswith("http://")], normalize_mac(m.group(1)) if m else None


def discover(seconds=3.0):
    """WS-Discovery probe on the LAN: {camera ip: (device service URLs, MAC or None)}."""
    found = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        sock.sendto((PROBE % uuid.uuid4()).encode(), DISCOVERY_ADDR)
        end = time.monotonic() + seconds
        while True:
            left = end - time.monotonic()
            if left <= 0:
                break
            sock.settimeout(left)
            try:
                data, (ip, _) = sock.recvfrom(65535)
            except socket.timeout:
                break
            xaddrs, mac = parse_probe_match(data)
            if xaddrs:
                found[ip] = (xaddrs, mac)
    except OSError:
        pass
    finally:
        sock.close()
    return found


class OnvifError(Exception):
    pass


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _first(root, name):
    return next((e for e in root.iter() if _local(e.tag) == name), None)


def _text(root, name):
    e = _first(root, name)
    return (e.text or "").strip() if e is not None else None


class Camera:
    """One ONVIF camera. `url` is its device service, e.g. http://192.168.1.20:8899/onvif/device_service."""

    def __init__(self, url, timeout=4.0, opener=None, sleep=time.sleep):
        self.url = url
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen
        self.sleep = sleep
        self._lock = threading.Lock()       # discovery
        self._move_lock = threading.Lock()  # one pan/tilt step at a time
        self._services = None               # {"ptz": url, "media": url, "imaging": url}
        self._profile = None                # (profile token, video source token)

    def _call(self, url, body):
        req = urllib.request.Request(url, data=(ENVELOPE % body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/soap+xml; charset=utf-8"})
        try:
            with self._open(req, timeout=self.timeout) as resp:
                raw = resp.read(MAX_REPLY)
        except urllib.error.HTTPError as exc:  # SOAP faults come back as HTTP 400/500
            raw = exc.read(MAX_REPLY)
        except (OSError, ValueError) as exc:
            raise OnvifError("camera not answering (%s)" % getattr(exc, "reason", exc))
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            raise OnvifError("camera sent an unreadable reply")
        if _first(root, "Fault") is not None:
            raise OnvifError("camera refused: %s" % (_text(root, "Text") or "unknown error")[:120])
        return root

    def _same_host(self, xaddr):
        """A service URL from the camera, kept on the configured host (only its path is used)."""
        base = urllib.parse.urlsplit(self.url)
        path = urllib.parse.urlsplit(xaddr or "").path
        return urllib.parse.urlunsplit((base.scheme, base.netloc, path or base.path, "", ""))

    def _discover(self):
        with self._lock:
            if self._services and self._profile:
                return self._services, self._profile
            root = self._call(self.url, '<GetCapabilities xmlns="%s"><Category>All</Category>'
                                        '</GetCapabilities>' % DEVICE_NS)
            services = {}
            for name, key in (("PTZ", "ptz"), ("Media", "media"), ("Imaging", "imaging")):
                section = _first(root, name)
                xaddr = _text(section, "XAddr") if section is not None else None
                if xaddr:
                    services[key] = self._same_host(xaddr)
            if "media" not in services:
                raise OnvifError("camera has no ONVIF media service")
            root = self._call(services["media"], '<GetProfiles xmlns="%s"/>' % MEDIA_NS)
            profile = _first(root, "Profiles")
            if profile is None:
                raise OnvifError("camera has no ONVIF profile")
            self._services = services
            self._profile = (escape(profile.get("token") or ""), escape(_text(profile, "SourceToken") or ""))
            return self._services, self._profile

    def _service(self, key):
        services, profile = self._discover()
        if key not in services:
            raise OnvifError("camera has no ONVIF %s service" % key)
        return services[key], profile

    def step(self, direction, speed=0.5, seconds=0.5):
        """Pan or tilt for `seconds`, then stop. Waits for a step already running."""
        dx, dy = DIRECTIONS[direction]
        url, (token, _) = self._service("ptz")
        if not self._move_lock.acquire(timeout=seconds * 4 + self.timeout):
            raise OnvifError("camera is still moving")
        try:
            self._call(url, '<ContinuousMove xmlns="%s"><ProfileToken>%s</ProfileToken><Velocity>'
                            '<tt:PanTilt x="%.2f" y="%.2f"/></Velocity></ContinuousMove>'
                       % (PTZ_NS, token, dx * speed, dy * speed))
            self.sleep(seconds)
        finally:
            try:
                self._call(url, '<Stop xmlns="%s"><ProfileToken>%s</ProfileToken><PanTilt>true</PanTilt>'
                                '<Zoom>true</Zoom></Stop>' % (PTZ_NS, token))
            finally:
                self._move_lock.release()

    def mac(self):
        """MAC address of the camera's first network interface (None if it doesn't say)."""
        root = self._call(self.url, '<GetNetworkInterfaces xmlns="%s"/>' % DEVICE_NS)
        return normalize_mac(_text(root, "HwAddress"))

    def moved_to(self, url):
        """Use a new device service URL (the camera's address changed); services are found again."""
        with self._lock:
            self.url = url
            self._services = None
            self._profile = None

    def mode(self):
        """Current day/night mode: "day", "night" or "auto" (None if the camera reports something else)."""
        url, (_, source) = self._service("imaging")
        root = self._call(url, '<GetImagingSettings xmlns="%s"><VideoSourceToken>%s</VideoSourceToken>'
                               '</GetImagingSettings>' % (IMAGING_NS, source))
        value = (_text(root, "IrCutFilter") or "").upper()
        return next((k for k, v in MODES.items() if v == value), None)

    def set_mode(self, mode):
        url, (_, source) = self._service("imaging")
        self._call(url, '<SetImagingSettings xmlns="%s"><VideoSourceToken>%s</VideoSourceToken>'
                        '<ImagingSettings><tt:IrCutFilter>%s</tt:IrCutFilter></ImagingSettings>'
                        '<ForcePersistence>true</ForcePersistence></SetImagingSettings>'
                   % (IMAGING_NS, source, MODES[mode]))
