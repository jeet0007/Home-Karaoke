"""Dependency-free Standard MIDI File writer for melody note segments.

Turns the pipeline's melody - a list of {start_ms, end_ms, midi} segments -
into a real `.mid` file, so the transcribed melody is a reusable, portable
product (openable in any DAW/notation app), not just an internal JSON blob.

Deliberately hand-rolled: a monophonic melody needs only a format-0 file
with tempo + note on/off events, which is a few dozen bytes of well-specified
structure. That is not worth a dependency (mido/pretty_midi and their
transitive deps) in a project whose whole stance is a minimal footprint on a
NAS. The MIDI note number IS the segment's `midi` field, so no pitch
conversion is needed.
"""

DEFAULT_TICKS_PER_BEAT = 480
DEFAULT_BPM = 120
DEFAULT_VELOCITY = 80


def _vlq(value):
    """MIDI variable-length quantity encoding of a non-negative int."""
    if value < 0:
        raise ValueError("VLQ cannot encode a negative value")
    out = bytearray([value & 0x7F])
    value >>= 7
    while value:
        out.insert(0, (value & 0x7F) | 0x80)
        value >>= 7
    return bytes(out)


def _chunk(tag, body):
    return tag + len(body).to_bytes(4, "big") + body


def notes_to_midi_bytes(notes, bpm=DEFAULT_BPM, ticks_per_beat=DEFAULT_TICKS_PER_BEAT, velocity=DEFAULT_VELOCITY):
    """Serialize melody segments to Standard MIDI File (format 0) bytes.

    `notes`: iterable of {start_ms, end_ms, midi}. Notes are placed on
    channel 0; overlapping notes are handled correctly (events are globally
    time-sorted, note-offs before note-ons at the same tick). An empty
    melody yields a valid file with just tempo + end-of-track.
    """
    ms_per_beat = 60000.0 / bpm

    def to_ticks(ms):
        return max(0, int(round(ms / ms_per_beat * ticks_per_beat)))

    # (tick, is_note_on, note) - at equal ticks, note-off (0) sorts before
    # note-on (1) so a note ending exactly when another begins doesn't get
    # its release swallowed.
    events = []
    for note in notes:
        pitch = max(0, min(127, int(note["midi"])))
        start = to_ticks(note["start_ms"])
        end = to_ticks(note["end_ms"])
        if end <= start:
            continue
        events.append((start, 1, pitch))
        events.append((end, 0, pitch))
    events.sort(key=lambda e: (e[0], e[1]))

    body = bytearray()
    # Tempo meta event at t=0: FF 51 03 <microseconds per quarter note>.
    microseconds_per_beat = int(round(60_000_000 / bpm))
    body += _vlq(0) + b"\xff\x51\x03" + microseconds_per_beat.to_bytes(3, "big")

    last_tick = 0
    for tick, is_on, pitch in events:
        body += _vlq(tick - last_tick)
        last_tick = tick
        status = 0x90 if is_on else 0x80
        body += bytes([status, pitch, velocity if is_on else 0])

    # End of track.
    body += _vlq(0) + b"\xff\x2f\x00"

    header = bytes([0, 0]) + bytes([0, 1]) + ticks_per_beat.to_bytes(2, "big")
    return _chunk(b"MThd", header) + _chunk(b"MTrk", bytes(body))


def write_midi(notes, path, **kwargs):
    with open(path, "wb") as handle:
        handle.write(notes_to_midi_bytes(notes, **kwargs))
    return path
