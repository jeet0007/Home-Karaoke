import contextlib
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import artifacts  # noqa: E402
from core import library  # noqa: E402
from core import pipeline  # noqa: E402

LYRICS = {"synced": [{"time_ms": 0, "text": "hi"}], "plain": "hi", "source": "lrclib"}
VIDEO = {"video_id": "vid123", "duration_seconds": 200}
# Basic Pitch note events: (start_s, end_s, pitch_midi, amplitude, pitch_bends).
NOTE_EVENTS = [(0.0, 0.5, 60, 0.9, []), (0.6, 1.0, 62, 0.9, [])]


def _song(**overrides):
    song = {"id": 1, "artist": "Passenger", "title": "Let Her Go", "duration_seconds": 200, "ytmusic_video_id": "ytm1"}
    song.update(overrides)
    return song


@contextlib.contextmanager
def _vocal_stubs(decode=None, separate=None, transcribe_return=NOTE_EVENTS, available=True):
    """Patch the ML vocal stages so the pipeline's vocal path runs without
    torch/ffmpeg. note_events_to_segments stays REAL (pure conversion)."""

    def default_decode(url, out_path, timeout=120):
        with open(out_path, "wb") as handle:
            handle.write(b"MIX")
        return out_path

    def default_separate(mix_path, vocal_path, instrumental_path=None):
        with open(vocal_path, "wb") as handle:
            handle.write(b"VOX")
        if instrumental_path:
            with open(instrumental_path, "wb") as handle:
                handle.write(b"INST")
        return vocal_path

    with mock.patch.object(pipeline.vocal_transcribe, "available", return_value=available), mock.patch.object(
        pipeline.vocal_transcribe, "_decode_to_wav", side_effect=decode or default_decode
    ), mock.patch.object(
        pipeline.vocal_transcribe, "separate_vocals", side_effect=separate or default_separate
    ), mock.patch.object(
        pipeline.vocal_transcribe, "transcribe", return_value=transcribe_return
    ):
        yield


class PipelineTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = artifacts.ArtifactStore(tmp.name)

    def _build(self, **overrides):
        steps = {
            "fetch_lyrics": lambda a, t, d: LYRICS,
            "find_video": lambda a, t, d: VIDEO,
            "resolve_audio_url": lambda ytm: f"https://audio/{ytm}",
        }
        steps.update(overrides)
        return pipeline.build_processor(self.store, **steps)

    def test_missing_synced_lyrics_fails(self):
        process = self._build(fetch_lyrics=lambda a, t, d: {"synced": [], "plain": "x"})
        with self.assertRaises(library.ProcessingError):
            process(_song())

    def test_missing_video_fails(self):
        process = self._build(find_video=lambda a, t, d: None)
        with self.assertRaises(library.ProcessingError):
            process(_song())

    def test_vocal_path_writes_and_records_artifacts(self):
        with _vocal_stubs():
            result = self._build()(_song())

        self.assertEqual(result["video_id"], "vid123")
        self.assertEqual(result["melody"]["source"], "demucs+basic-pitch")
        self.assertEqual([n["midi"] for n in result["melody"]["notes"]], [60, 62])
        kinds = {a["kind"] for a in result["artifacts"]}
        self.assertEqual(kinds, {"lyrics", "mix", "vocals", "instrumental", "melody", "midi"})
        self.assertTrue(self.store.exists(1, artifacts.KIND_MIDI))
        with open(self.store.path(1, artifacts.KIND_MIDI), "rb") as handle:
            self.assertEqual(handle.read(4), b"MThd")

    def test_vocal_path_keeps_stem(self):
        decode_calls, separate_calls = [], []

        def fake_decode(url, out_path, timeout=120):
            decode_calls.append(url)
            with open(out_path, "wb") as handle:
                handle.write(b"MIX")
            return out_path

        def fake_separate(mix_path, vocal_path, instrumental_path=None):
            separate_calls.append(mix_path)
            with open(vocal_path, "wb") as handle:
                handle.write(b"VOX")
            if instrumental_path:
                with open(instrumental_path, "wb") as handle:
                    handle.write(b"INST")
            return vocal_path

        with _vocal_stubs(decode=fake_decode, separate=fake_separate):
            self._build()(_song())

        # The mix + both stems are persisted (singer audio + backing track),
        # from ONE decode and ONE Demucs pass - the instrumental stage reuses
        # what the melody stage's separation already wrote.
        self.assertTrue(self.store.exists(1, artifacts.KIND_VOCALS))
        self.assertTrue(self.store.exists(1, artifacts.KIND_INSTRUMENTAL))
        self.assertTrue(self.store.exists(1, artifacts.KIND_MIX))
        self.assertEqual(len(decode_calls), 1)
        self.assertEqual(len(separate_calls), 1)

    def test_reuses_vocal_stem_without_re_separating(self):
        # Both stems already on disk: transcription reruns, decode + Demucs
        # don't. (A vocal stem WITHOUT an instrumental is the legacy backfill
        # case - covered in InstrumentalStageTestCase - where separation
        # legitimately reruns.)
        self.store.write_bytes(1, artifacts.KIND_VOCALS, b"VOX")
        self.store.write_bytes(1, artifacts.KIND_INSTRUMENTAL, b"INST")
        boom = mock.Mock(side_effect=AssertionError("should not run"))
        with _vocal_stubs(decode=boom, separate=boom):
            result = self._build()(_song())
        self.assertEqual(result["melody"]["source"], "demucs+basic-pitch")

    def test_melody_stage_skips_when_already_present(self):
        # Pre-seed a stored melody: no audio resolve, no transcription.
        # (available=False keeps the instrumental backfill from resolving
        # audio either - its own paths are covered in InstrumentalStageTestCase.)
        self.store.write_json(1, artifacts.KIND_MELODY, {"notes": [{"start_ms": 0, "end_ms": 500, "midi": 60}], "source": "demucs+basic-pitch"})
        resolve = mock.Mock(side_effect=AssertionError("audio should not be resolved"))
        with _vocal_stubs(available=False):
            result = self._build(resolve_audio_url=resolve)(_song())
        resolve.assert_not_called()
        self.assertEqual(result["melody"]["notes"][0]["midi"], 60)
        self.assertTrue(self.store.exists(1, artifacts.KIND_MIDI))  # regenerated

    def test_unavailable_yields_no_melody_but_song_succeeds(self):
        # No torch/Demucs installed -> no guide, deliberately NO full-mix
        # fallback. The song is still fully processed (lyrics + video).
        resolve = mock.Mock(side_effect=AssertionError("no audio resolve without the vocal path"))
        with _vocal_stubs(available=False):
            result = self._build(resolve_audio_url=resolve)(_song())
        self.assertIsNone(result["melody"])
        self.assertEqual(result["video_id"], "vid123")
        self.assertEqual({a["kind"] for a in result["artifacts"]}, {"lyrics"})
        resolve.assert_not_called()

    def test_vocal_failure_yields_no_melody(self):
        with _vocal_stubs(decode=mock.Mock(side_effect=RuntimeError("demucs blew up"))):
            result = self._build()(_song())
        self.assertIsNone(result["melody"])
        self.assertEqual(result["video_id"], "vid123")  # song still succeeds

    def test_no_ytmusic_id_skips_melody(self):
        resolve = mock.Mock()
        with _vocal_stubs():
            result = self._build(resolve_audio_url=resolve)(_song(ytmusic_video_id=None))
        self.assertIsNone(result["melody"])
        resolve.assert_not_called()

    def test_notes_are_gated_to_lyrics(self):
        gated = mock.Mock(return_value=[{"start_ms": 0, "end_ms": 500, "midi": 60}])
        with _vocal_stubs(), mock.patch.object(pipeline, "gate_notes_to_lyrics", gated):
            result = self._build()(_song())
        gated.assert_called_once()
        self.assertEqual(result["melody"]["notes"], [{"start_ms": 0, "end_ms": 500, "midi": 60}])


class InstrumentalStageTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = artifacts.ArtifactStore(tmp.name)

    def _build(self, **overrides):
        steps = {
            "fetch_lyrics": lambda a, t, d: LYRICS,
            "find_video": lambda a, t, d: VIDEO,
            "resolve_audio_url": lambda ytm: f"https://audio/{ytm}",
        }
        steps.update(overrides)
        return pipeline.build_processor(self.store, **steps)

    def _seed_processed_song(self, with_instrumental=False):
        """A song from before the instrumental artifact existed: melody,
        vocal stem, and mix kept on disk."""
        self.store.write_json(
            1, artifacts.KIND_MELODY, {"notes": [{"start_ms": 0, "end_ms": 500, "midi": 60}], "source": "demucs+basic-pitch"}
        )
        self.store.write_bytes(1, artifacts.KIND_VOCALS, b"VOX")
        self.store.write_bytes(1, artifacts.KIND_MIX, b"MIX")
        if with_instrumental:
            self.store.write_bytes(1, artifacts.KIND_INSTRUMENTAL, b"INST")

    def test_fresh_run_produces_instrumental_from_the_same_separation(self):
        with _vocal_stubs():
            result = self._build()(_song())
        self.assertTrue(self.store.exists(1, artifacts.KIND_INSTRUMENTAL))
        self.assertIn("instrumental", {a["kind"] for a in result["artifacts"]})
        self.assertEqual(result["report"]["instrumental"]["status"], "ok")

    def test_backfills_instrumental_for_previously_processed_song(self):
        self._seed_processed_song()
        separate_calls = []

        def fake_separate(mix_path, vocal_path, instrumental_path=None):
            separate_calls.append(instrumental_path)
            with open(vocal_path, "wb") as handle:
                handle.write(b"VOX2")
            with open(instrumental_path, "wb") as handle:
                handle.write(b"INST")
            return vocal_path

        with _vocal_stubs(separate=fake_separate):
            result = self._build()(_song())

        # Melody was reused, but separation reran once, from the KEPT mix
        # (no decode), purely to produce the missing instrumental.
        self.assertEqual(len(separate_calls), 1)
        self.assertTrue(separate_calls[0].endswith("instrumental.wav"))
        self.assertEqual(result["report"]["melody"]["status"], "reused")
        self.assertEqual(result["report"]["instrumental"]["status"], "ok")
        self.assertTrue(self.store.exists(1, artifacts.KIND_INSTRUMENTAL))

    def test_reuses_existing_instrumental(self):
        self._seed_processed_song(with_instrumental=True)
        boom = mock.Mock(side_effect=AssertionError("should not separate again"))
        with _vocal_stubs(decode=boom, separate=boom):
            result = self._build()(_song())
        self.assertEqual(result["report"]["instrumental"]["status"], "reused")
        self.assertIn("instrumental", {a["kind"] for a in result["artifacts"]})

    def test_skipped_without_addon(self):
        with _vocal_stubs(available=False):
            result = self._build()(_song())
        self.assertEqual(result["report"]["instrumental"]["status"], "skipped")
        self.assertIn("add-on", result["report"]["instrumental"]["detail"])

    def test_skipped_without_any_source_recording(self):
        # No kept mix and no ytmusic id: nothing to separate from.
        with _vocal_stubs():
            result = self._build()(_song(ytmusic_video_id=None))
        self.assertEqual(result["report"]["instrumental"]["status"], "skipped")

    def test_separation_failure_is_best_effort(self):
        self._seed_processed_song()
        with _vocal_stubs(separate=mock.Mock(side_effect=RuntimeError("demucs blew up"))):
            result = self._build()(_song())
        self.assertEqual(result["report"]["instrumental"]["status"], "failed")
        self.assertIn("demucs blew up", result["report"]["instrumental"]["detail"])
        self.assertEqual(result["video_id"], "vid123")  # song still succeeds


class ProcessingReportTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = artifacts.ArtifactStore(tmp.name)

    def _build(self, **overrides):
        steps = {
            "fetch_lyrics": lambda a, t, d: LYRICS,
            "find_video": lambda a, t, d: VIDEO,
            "resolve_audio_url": lambda ytm: f"https://audio/{ytm}",
        }
        steps.update(overrides)
        return pipeline.build_processor(self.store, **steps)

    def test_happy_path_reports_every_stage_ok(self):
        with _vocal_stubs():
            report = self._build()(_song())["report"]
        self.assertEqual(report["lyrics"]["status"], "ok")
        self.assertEqual(report["video"]["status"], "ok")
        self.assertEqual(report["melody"]["status"], "ok")
        self.assertEqual(report["instrumental"]["status"], "ok")
        self.assertIn("1 synced lines from lrclib", report["lyrics"]["detail"])
        self.assertIn("notes from the isolated vocal", report["melody"]["detail"])

    def test_reports_melody_skipped_without_addon(self):
        with _vocal_stubs(available=False):
            report = self._build()(_song())["report"]
        self.assertEqual(report["melody"]["status"], "skipped")
        self.assertIn("add-on", report["melody"]["detail"])

    def test_reports_melody_skipped_without_source_id(self):
        with _vocal_stubs():
            report = self._build()(_song(ytmusic_video_id=None))["report"]
        self.assertEqual(report["melody"]["status"], "skipped")
        self.assertIn("no source-recording id", report["melody"]["detail"])

    def test_reports_melody_failed_with_reason(self):
        with _vocal_stubs(decode=mock.Mock(side_effect=RuntimeError("ffmpeg exploded"))):
            report = self._build()(_song())["report"]
        self.assertEqual(report["melody"]["status"], "failed")
        self.assertIn("ffmpeg exploded", report["melody"]["detail"])

    def test_hard_failure_carries_partial_report_on_exception(self):
        process = self._build(find_video=lambda a, t, d: None)
        try:
            process(_song())
            self.fail("expected ProcessingError")
        except library.ProcessingError as exc:
            self.assertEqual(exc.report["lyrics"]["status"], "ok")  # lyrics passed before video failed
            self.assertEqual(exc.report["video"]["status"], "failed")
            # stage_runs carries the same partial lineage - completed stages
            # (lyrics) plus the one that failed (video), in order.
            stages = [row["stage"] for row in exc.stage_runs]
            self.assertEqual(stages, ["lyrics", "video"])
            self.assertEqual(exc.stage_runs[0]["status"], "ok")
            self.assertEqual(exc.stage_runs[1]["status"], "failed")
            self.assertEqual(exc.stage_runs[1]["error"], "no karaoke backing track found")


class TempoTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = artifacts.ArtifactStore(tmp.name)

    def _build(self):
        return pipeline.build_processor(
            self.store,
            fetch_lyrics=lambda a, t, d: LYRICS,
            find_video=lambda a, t, d: VIDEO,
            resolve_audio_url=lambda ytm: f"https://audio/{ytm}",
        )

    def test_bpm_estimated_stored_and_reported(self):
        with _vocal_stubs(), mock.patch.object(pipeline.tempo, "available", return_value=True), mock.patch.object(
            pipeline.tempo, "estimate_bpm", return_value=128.0
        ) as est:
            result = self._build()(_song())

        self.assertEqual(result["melody"]["bpm"], 128.0)
        self.assertEqual(result["report"]["tempo"], {"status": "ok", "detail": "128.0 BPM"})
        # BPM was estimated from the decoded full mix, not the vocal stem.
        self.assertTrue(est.call_args.args[0].endswith("mix.wav"))
        # The stored melody carries the BPM (so reuse/MIDI use it).
        self.assertEqual(self.store.read_json(1, artifacts.KIND_MELODY)["bpm"], 128.0)

    def test_tempo_skipped_without_librosa(self):
        with _vocal_stubs(), mock.patch.object(pipeline.tempo, "available", return_value=False):
            result = self._build()(_song())
        self.assertIsNone(result["melody"]["bpm"])
        self.assertEqual(result["report"]["tempo"]["status"], "skipped")

    def test_tempo_failure_is_best_effort(self):
        with _vocal_stubs(), mock.patch.object(pipeline.tempo, "available", return_value=True), mock.patch.object(
            pipeline.tempo, "estimate_bpm", side_effect=RuntimeError("librosa boom")
        ):
            result = self._build()(_song())
        self.assertIsNone(result["melody"]["bpm"])
        self.assertEqual(result["report"]["tempo"]["status"], "failed")

    def test_midi_uses_estimated_tempo(self):
        from core import midi

        with _vocal_stubs(), mock.patch.object(pipeline.tempo, "available", return_value=True), mock.patch.object(
            pipeline.tempo, "estimate_bpm", return_value=90.0
        ):
            self._build()(_song())

        # A note-off one beat long should land at ticks_per_beat regardless of
        # BPM, but the tempo META event encodes the BPM - decode it to confirm
        # 90 BPM (666667 microseconds/beat) was written, not the 120 default.
        with open(self.store.path(1, artifacts.KIND_MIDI), "rb") as handle:
            data = handle.read()
        expected = (round(60_000_000 / 90.0)).to_bytes(3, "big")
        self.assertIn(b"\xff\x51\x03" + expected, data)


class StageRunsTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = artifacts.ArtifactStore(tmp.name)

    def _build(self, **overrides):
        steps = {
            "fetch_lyrics": lambda a, t, d: LYRICS,
            "find_video": lambda a, t, d: VIDEO,
            "resolve_audio_url": lambda ytm: f"https://audio/{ytm}",
        }
        steps.update(overrides)
        return pipeline.build_processor(self.store, **steps)

    def test_stage_runs_carry_timing_for_every_stage(self):
        with _vocal_stubs():
            result = self._build()(_song())
        by_stage = {row["stage"]: row for row in result["stage_runs"]}
        self.assertEqual(
            set(by_stage),
            {"lyrics", "video", "decode", "separate", "transcribe", "tempo", "melody", "instrumental"},
        )
        for row in by_stage.values():
            self.assertGreaterEqual(row["finished_at"], row["started_at"])
            self.assertGreaterEqual(row["duration_ms"], 0)

    def test_ok_stages_carry_output_hash_video_does_not(self):
        with _vocal_stubs():
            result = self._build()(_song())
        by_stage = {row["stage"]: row for row in result["stage_runs"]}
        self.assertIsNotNone(by_stage["lyrics"]["output_hash"])
        self.assertIsNotNone(by_stage["melody"]["output_hash"])
        self.assertIsNone(by_stage["video"]["output_hash"])  # no file I/O for this stage

    def test_reused_melody_carries_output_hash(self):
        self.store.write_json(
            1,
            artifacts.KIND_MELODY,
            {"notes": [{"start_ms": 0, "end_ms": 500, "midi": 60}], "source": "demucs+basic-pitch"},
        )
        with _vocal_stubs(available=False):
            result = self._build()(_song())
        by_stage = {row["stage"]: row for row in result["stage_runs"]}
        self.assertEqual(by_stage["melody"]["status"], "reused")
        self.assertIsNotNone(by_stage["melody"]["output_hash"])

    def test_failed_melody_carries_best_effort_input_hash(self):
        with _vocal_stubs(decode=mock.Mock(side_effect=RuntimeError("ffmpeg exploded"))):
            result = self._build()(_song())
        by_stage = {row["stage"]: row for row in result["stage_runs"]}
        self.assertEqual(by_stage["melody"]["status"], "failed")
        # No vocal stem was ever written (decode itself failed) - the hash
        # is None rather than an error.
        self.assertIsNone(by_stage["melody"]["input_hashes"][0])

    def test_run_id_defaults_when_not_supplied(self):
        with _vocal_stubs():
            result = self._build()(_song())
        self.assertTrue(result["run_id"])

    def test_run_id_propagates_when_supplied(self):
        with _vocal_stubs():
            result = self._build()(_song(), run_id="explicit-run-id")
        self.assertEqual(result["run_id"], "explicit-run-id")


class GateNotesToLyricsTestCase(unittest.TestCase):
    NOTES = [
        {"start_ms": 0, "end_ms": 2000, "midi": 45},  # instrumental intro
        {"start_ms": 10200, "end_ms": 11000, "midi": 60},  # during line 1
        {"start_ms": 30000, "end_ms": 31000, "midi": 50},  # instrumental break
        {"start_ms": 60500, "end_ms": 61500, "midi": 62},  # long after last line
    ]
    LYRICS = [
        {"time_ms": 10000, "text": "line one"},
        {"time_ms": 12000, "text": "line two ends the vocals"},
    ]

    def test_notes_outside_sung_sections_are_dropped(self):
        gated = pipeline.gate_notes_to_lyrics(self.NOTES, self.LYRICS)
        self.assertEqual([n["start_ms"] for n in gated], [10200])

    def test_lead_in_before_a_line_is_kept(self):
        notes = [{"start_ms": 9500, "end_ms": 9900, "midi": 60}]  # 500ms early
        self.assertEqual(len(pipeline.gate_notes_to_lyrics(notes, self.LYRICS)), 1)

    def test_no_lyrics_passes_notes_through(self):
        self.assertEqual(pipeline.gate_notes_to_lyrics(self.NOTES, []), self.NOTES)
        self.assertEqual(pipeline.gate_notes_to_lyrics(self.NOTES, None), self.NOTES)

    def test_last_line_window_is_capped(self):
        notes = [{"start_ms": 27500, "end_ms": 28500, "midi": 60}]  # 15s after final line
        self.assertEqual(pipeline.gate_notes_to_lyrics(notes, self.LYRICS), [])


if __name__ == "__main__":
    unittest.main()
