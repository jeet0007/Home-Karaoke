import io
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import midi  # noqa: E402


def _read_chunk(buf):
    tag = buf.read(4)
    (length,) = struct.unpack(">I", buf.read(4))
    return tag, buf.read(length)


def _parse_events(track_body):
    """Minimal SMF track parser -> list of (delta, status, data...) for the
    channel-voice events, enough to assert note on/off structure."""
    buf = io.BytesIO(track_body)
    events = []

    def read_vlq():
        value = 0
        while True:
            (byte,) = buf.read(1)
            value = (value << 7) | (byte & 0x7F)
            if not byte & 0x80:
                return value

    while buf.tell() < len(track_body):
        delta = read_vlq()
        status = buf.read(1)[0]
        if status == 0xFF:  # meta
            buf.read(1)  # type
            length = read_vlq()
            buf.read(length)
            events.append((delta, "meta"))
        elif status in (0x80, 0x90):
            note = buf.read(1)[0]
            vel = buf.read(1)[0]
            events.append((delta, "on" if status == 0x90 else "off", note, vel))
    return events


class MidiWriterTestCase(unittest.TestCase):
    def test_header_is_format0_single_track(self):
        data = midi.notes_to_midi_bytes([{"start_ms": 0, "end_ms": 500, "midi": 60}])
        buf = io.BytesIO(data)
        tag, header = _read_chunk(buf)
        self.assertEqual(tag, b"MThd")
        fmt, ntrks, division = struct.unpack(">HHH", header)
        self.assertEqual(fmt, 0)
        self.assertEqual(ntrks, 1)
        self.assertEqual(division, midi.DEFAULT_TICKS_PER_BEAT)

    def test_note_produces_on_then_off(self):
        data = midi.notes_to_midi_bytes([{"start_ms": 0, "end_ms": 500, "midi": 60}])
        buf = io.BytesIO(data)
        _read_chunk(buf)  # header
        _tag, track = _read_chunk(buf)
        events = _parse_events(track)
        note_events = [e for e in events if e[1] in ("on", "off")]
        self.assertEqual(len(note_events), 2)
        self.assertEqual(note_events[0][1], "on")
        self.assertEqual(note_events[0][2], 60)
        self.assertEqual(note_events[1][1], "off")
        self.assertEqual(note_events[1][2], 60)

    def test_note_duration_maps_to_ticks(self):
        # 500ms at 120bpm (500ms/beat) = 1 beat = ticks_per_beat.
        data = midi.notes_to_midi_bytes([{"start_ms": 0, "end_ms": 500, "midi": 60}])
        buf = io.BytesIO(data)
        _read_chunk(buf)
        _tag, track = _read_chunk(buf)
        events = _parse_events(track)
        off = next(e for e in events if e[1] == "off")
        # The note-off delta (from the note-on at t=0) should be one beat.
        self.assertEqual(off[0], midi.DEFAULT_TICKS_PER_BEAT)

    def test_empty_melody_is_valid_file(self):
        data = midi.notes_to_midi_bytes([])
        buf = io.BytesIO(data)
        tag, _ = _read_chunk(buf)
        self.assertEqual(tag, b"MThd")
        tag2, track = _read_chunk(buf)
        self.assertEqual(tag2, b"MTrk")
        self.assertTrue(track.endswith(b"\xff\x2f\x00"))  # end of track

    def test_degenerate_notes_skipped(self):
        data = midi.notes_to_midi_bytes([{"start_ms": 100, "end_ms": 100, "midi": 60}])
        buf = io.BytesIO(data)
        _read_chunk(buf)
        _tag, track = _read_chunk(buf)
        events = _parse_events(track)
        self.assertEqual([e for e in events if e[1] in ("on", "off")], [])

    def test_pitch_is_clamped_to_valid_range(self):
        data = midi.notes_to_midi_bytes([{"start_ms": 0, "end_ms": 500, "midi": 200}])
        buf = io.BytesIO(data)
        _read_chunk(buf)
        _tag, track = _read_chunk(buf)
        on = next(e for e in _parse_events(track) if e[1] == "on")
        self.assertEqual(on[2], 127)

    def test_write_midi_to_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.mid")
            midi.write_midi([{"start_ms": 0, "end_ms": 500, "midi": 62}], path)
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(4), b"MThd")


if __name__ == "__main__":
    unittest.main()
