from __future__ import annotations

import struct
import json
import unittest

from app.gemini_live import (
    Pcm16Downsampler48To16,
    PcmChunkBuffer,
    merge_transcript,
    parse_server_content,
    setup_message,
    target_language_code,
)


class GeminiLiveTests(unittest.TestCase):
    def test_target_language_aliases_match_supported_codes(self) -> None:
        self.assertEqual(target_language_code("zh-CN"), "zh-Hans")
        self.assertEqual(target_language_code("zh-TW"), "zh-Hant")
        self.assertEqual(target_language_code("en-US"), "en")
        self.assertEqual(target_language_code("es"), "es")

    def test_setup_message_uses_live_api_transcription_field_locations(self) -> None:
        payload = json.loads(setup_message("en"))
        setup = payload["setup"]
        generation_config = setup["generationConfig"]

        self.assertEqual(setup["inputAudioTranscription"], {})
        self.assertEqual(setup["outputAudioTranscription"], {})
        self.assertNotIn("inputAudioTranscription", generation_config)
        self.assertNotIn("outputAudioTranscription", generation_config)
        self.assertEqual(
            generation_config["translationConfig"]["targetLanguageCode"],
            "en",
        )

    def test_downsampler_preserves_phase_across_browser_chunks(self) -> None:
        samples = list(range(12))
        pcm = struct.pack("<12h", *samples)
        downsampler = Pcm16Downsampler48To16()

        first = downsampler.push(pcm[:10])
        second = downsampler.push(pcm[10:])

        self.assertEqual(struct.unpack("<4h", first + second), (0, 3, 6, 9))

    def test_chunk_buffer_emits_fixed_chunks_and_flushes_tail(self) -> None:
        buffer = PcmChunkBuffer(chunk_bytes=4)
        self.assertEqual(buffer.push(b"abc"), [])
        self.assertEqual(buffer.push(b"defghi"), [b"abcd", b"efgh"])
        self.assertEqual(buffer.flush(), b"i")

    def test_merge_transcript_handles_delta_and_cumulative_events(self) -> None:
        self.assertEqual(merge_transcript("Hello ", "world"), "Hello world")
        self.assertEqual(merge_transcript("Hello", "Hello world"), "Hello world")
        self.assertEqual(merge_transcript("The wor", "world"), "The world")

    def test_parse_server_content_reads_all_audio_parts(self) -> None:
        parsed = parse_server_content(
            {
                "serverContent": {
                    "inputTranscription": {"text": "안녕하세요"},
                    "outputTranscription": {"text": "Hello"},
                    "modelTurn": {
                        "parts": [
                            {"inlineData": {"data": "AAA=", "mimeType": "audio/pcm;rate=24000"}},
                            {"inlineData": {"data": "BBB=", "mimeType": "audio/pcm;rate=24000"}},
                        ]
                    },
                    "turnComplete": True,
                }
            }
        )
        self.assertEqual(parsed.input_transcript, "안녕하세요")
        self.assertEqual(parsed.output_transcript, "Hello")
        self.assertEqual(parsed.audio_chunks, ("AAA=", "BBB="))
        self.assertTrue(parsed.turn_complete)


if __name__ == "__main__":
    unittest.main()
