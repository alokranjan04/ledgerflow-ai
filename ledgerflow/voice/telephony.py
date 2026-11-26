"""Telephony provider adapters: answer XML + media-stream message formats.

Both providers send/expect mu-law 8 kHz mono audio, base64-encoded in JSON
text frames. They differ in the answer XML, event names and stream IDs.
"""
import base64
from xml.sax.saxutils import escape, quoteattr

from ledgerflow import config


class Vobiz:
    name = "vobiz"
    needs_start = False  # media frames are accepted without a stream id

    def answer_xml(self, ws_url: str, caller_id: str) -> str:
        # Do not add <Record> here: in production a Vobiz <Record recordSession>
        # alongside the stream coincided with calls that carried no audio either
        # way. Record on our side instead (agent/recorder.py).
        return (
            '<?xml version="1.0" encoding="UTF-8"?><Response>'
            '<Stream bidirectional="true" keepCallAlive="true" '
            'contentType="audio/x-mulaw;rate=8000">'
            f"{escape(ws_url)}?caller_id={escape(caller_id)}"
            f"{'&amp;token=' + escape(config.STREAM_TOKEN) if config.STREAM_TOKEN else ''}"
            "</Stream></Response>"
        )

    def parse(self, msg: dict):
        event = msg.get("event")
        if event == "media":
            return "media", base64.b64decode(msg["media"]["payload"])
        if event == "start":
            return "start", msg.get("start", {})
        if event == "stop":
            return "stop", None
        return None, None

    def play(self, mulaw: bytes, stream_id: str) -> dict:
        return {"event": "playAudio", "media": {
            "contentType": "audio/x-mulaw", "sampleRate": 8000,
            "payload": base64.b64encode(mulaw).decode()}}

    def clear(self, stream_id: str) -> dict:
        return {"event": "clear"}


class Twilio:
    name = "twilio"
    needs_start = True  # outbound frames must carry the streamSid from "start"

    def answer_xml(self, ws_url: str, caller_id: str) -> str:
        # Twilio strips query strings from stream URLs: pass data as <Parameter>,
        # which arrives in start.customParameters.
        params = f"<Parameter name=\"caller_id\" value={quoteattr(caller_id)}/>"
        if config.STREAM_TOKEN:
            params += f"<Parameter name=\"token\" value={quoteattr(config.STREAM_TOKEN)}/>"
        return (
            '<?xml version="1.0" encoding="UTF-8"?><Response><Connect>'
            f"<Stream url={quoteattr(ws_url)}>{params}</Stream>"
            "</Connect></Response>"
        )

    def parse(self, msg: dict):
        event = msg.get("event")
        if event == "media":
            if msg["media"].get("track", "inbound") != "inbound":
                return None, None
            return "media", base64.b64decode(msg["media"]["payload"])
        if event == "start":
            start = msg.get("start", {})
            start.setdefault("streamId", start.get("streamSid") or msg.get("streamSid"))
            return "start", start
        if event == "stop":
            return "stop", None
        return None, None

    def play(self, mulaw: bytes, stream_id: str) -> dict:
        return {"event": "media", "streamSid": stream_id,
                "media": {"payload": base64.b64encode(mulaw).decode()}}

    def clear(self, stream_id: str) -> dict:
        return {"event": "clear", "streamSid": stream_id}


def get_provider():
    providers = {"vobiz": Vobiz, "twilio": Twilio}
    if config.TELEPHONY_PROVIDER not in providers:
        raise ValueError(f"TELEPHONY_PROVIDER must be one of {sorted(providers)}")
    return providers[config.TELEPHONY_PROVIDER]()
