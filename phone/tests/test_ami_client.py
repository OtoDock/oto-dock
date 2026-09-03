"""AMI wire helpers (shared by the origination client + event listener)."""

import asyncio

import pytest

from telephony.ami_client import AMIError, format_action, read_packet


def test_format_action_wire_shape():
    wire = format_action({
        "Action": "Originate",
        "Channel": "Local/1000@from-internal",
        "Variable": "OUTBOUND_UUID=abc",
    })
    assert wire == (
        b"Action: Originate\r\n"
        b"Channel: Local/1000@from-internal\r\n"
        b"Variable: OUTBOUND_UUID=abc\r\n\r\n"
    )


@pytest.mark.parametrize("bad", ["a\rb", "a\nb", "a\x00b"])
def test_format_action_rejects_frame_delimiters(bad):
    with pytest.raises(AMIError):
        format_action({"Action": "Ping", "X": bad})
    with pytest.raises(AMIError):
        format_action({bad: "value"})


@pytest.mark.asyncio
async def test_read_packet_parses_until_blank_line():
    reader = asyncio.StreamReader()
    reader.feed_data(b"Response: Success\r\nActionID: 7\r\n\r\ntrailing")
    packet = await read_packet(reader, line_timeout=1.0)
    assert packet == {"Response": "Success", "ActionID": "7"}


@pytest.mark.asyncio
async def test_read_packet_raises_on_eof():
    reader = asyncio.StreamReader()
    reader.feed_data(b"Response: Success\r\n")  # no terminating blank line
    reader.feed_eof()
    with pytest.raises(AMIError):
        await read_packet(reader, line_timeout=1.0)
