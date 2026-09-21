"""WebRTC audio relay between a Lab Pi node and a student's browser, via the
portal.

This is deliberately separate machinery from pi_relay.py's SocketIO relay:
WebRTC media doesn't travel over Socket.IO, it's negotiated and streamed over
its own ICE/DTLS/SRTP path per RTCPeerConnection. Ported from
remote_lab_admin/audio_relay.py, which runs this same two-hop relay in
production: instead of one peer connection between two endpoints (a browser
talking straight to the node), the portal now runs *two*:

  Node's mic  --(PC #1: portal is the "browser")-->  portal
  Portal      --(PC #2: portal is the "node")-->      student's browser

and pipes the audio track it receives on PC #1 into PC #2 - closer to a small
SFU than a simple proxy.

One asyncio event loop for the process: aiortc needs a persistently-running
loop (media keeps flowing after the HTTP response returns), Flask's routes
are sync, so a single background-thread loop serves every session's offers
via run_coroutine_threadsafe.
"""
import asyncio
import threading

import requests
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaRelay

_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True).start()


def _run(coro, timeout=15):
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout=timeout)


class AudioRelayManager:
    def __init__(self):
        # session_key -> RTCPeerConnection to the node (portal acting as the
        # "browser" side of that connection)
        self._node_pcs = {}
        # session_key -> the audio MediaStreamTrack received from the node
        self._node_tracks = {}
        # session_key -> set of RTCPeerConnection to actual browsers
        self._browser_pcs = {}
        # Fans one real track out to N subscribers without opening a second
        # connection to the node per browser tab/reconnect.
        self._relay = MediaRelay()
        self._lock = threading.Lock()

    async def _connect_to_node(self, session_key, audio_offer_url):
        """Portal negotiates its own WebRTC connection to the node's audio
        server /offer endpoint - the same protocol a browser used to speak
        directly, just one hop earlier now."""
        pc = RTCPeerConnection()
        track_future = asyncio.get_event_loop().create_future()

        @pc.on("track")
        def on_track(track):
            if not track_future.done():
                track_future.set_result(track)

        pc.addTransceiver("audio", direction="recvonly")
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)

        loop = asyncio.get_event_loop()
        # requests is a blocking call - run it off the shared event loop so
        # one slow/unreachable node can't stall every other session's audio
        # negotiation.
        response = await loop.run_in_executor(
            None,
            lambda: requests.post(
                audio_offer_url,
                json={
                    "sdp": pc.localDescription.sdp,
                    "type": pc.localDescription.type,
                    "session_id": session_key,
                },
                timeout=10,
            ),
        )
        response.raise_for_status()
        answer = response.json()

        await pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))
        track = await asyncio.wait_for(track_future, timeout=8)

        self._node_pcs[session_key] = pc
        self._node_tracks[session_key] = track
        return track

    async def _get_node_track(self, session_key, audio_offer_url):
        pc = self._node_pcs.get(session_key)
        track = self._node_tracks.get(session_key)
        if pc is not None and track is not None and pc.connectionState not in ("failed", "closed"):
            return track
        return await self._connect_to_node(session_key, audio_offer_url)

    async def _handle_browser_offer(self, session_key, audio_offer_url, sdp, type_):
        node_track = await self._get_node_track(session_key, audio_offer_url)

        pc = RTCPeerConnection()
        with self._lock:
            self._browser_pcs.setdefault(session_key, set()).add(pc)

        @pc.on("connectionstatechange")
        async def on_connectionstatechange():
            if pc.connectionState == "failed":
                await pc.close()
            if pc.connectionState in ("failed", "closed"):
                with self._lock:
                    self._browser_pcs.get(session_key, set()).discard(pc)

        pc.addTrack(self._relay.subscribe(node_track))

        await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=type_))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        return pc.localDescription.sdp, pc.localDescription.type

    def handle_offer(self, session_key, audio_offer_url, sdp, type_):
        """Sync entry point for the Flask route: returns (answer_sdp, answer_type)."""
        return _run(self._handle_browser_offer(session_key, audio_offer_url, sdp, type_))

    def disconnect(self, session_key):
        """Tear down both legs for this session - call this everywhere a
        session is considered over (mirrors pi_relay.disconnect)."""
        async def _close():
            pc = self._node_pcs.pop(session_key, None)
            self._node_tracks.pop(session_key, None)
            if pc is not None:
                await pc.close()
            with self._lock:
                browser_pcs = self._browser_pcs.pop(session_key, set())
            for bpc in browser_pcs:
                await bpc.close()

        try:
            _run(_close())
        except Exception as e:
            print(f"[AudioRelay] Error closing session {session_key}: {e}")
