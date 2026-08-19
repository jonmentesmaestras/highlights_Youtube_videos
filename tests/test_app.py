import unittest
from types import SimpleNamespace
from unittest.mock import patch

import app as app_module


class YoutubeUrlTests(unittest.TestCase):
    def test_supported_youtube_urls(self) -> None:
        expected = "_m8bqUD3jy4"
        urls = [
            "https://www.youtube.com/watch?v=_m8bqUD3jy4",
            "https://youtu.be/_m8bqUD3jy4?t=20",
            "https://youtube.com/shorts/_m8bqUD3jy4",
            "https://m.youtube.com/live/_m8bqUD3jy4",
            "https://youtube.com/embed/_m8bqUD3jy4",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(app_module.extract_youtube_video_id(url), expected)

    def test_invalid_url_is_rejected(self) -> None:
        for url in ("", "https://example.com/watch?v=_m8bqUD3jy4", "not a url"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                app_module.extract_youtube_video_id(url)


class ResponseTests(unittest.TestCase):
    def test_json_fence_is_tolerated(self) -> None:
        self.assertEqual(
            app_module.parse_json_response('```json\n{"highlights": []}\n```', "test"),
            {"highlights": []},
        )

    def test_selection_is_enriched_with_calculations(self) -> None:
        selection = {
            "highlights": [
                {
                    "id": "h01",
                    "inicio": 10.0,
                    "fin": 130.5,
                    "puntajes": {
                        "densidad": 9,
                        "especificidad": 8,
                        "demanda_busqueda": 7,
                        "autonomia": 10,
                        "apertura": 6,
                    },
                }
            ],
            "descartados": [],
        }
        highlights = app_module.validate_selection(selection)
        self.assertEqual(highlights[0]["duracion_seg"], 120.5)
        self.assertEqual(highlights[0]["ponderado"], 8.2)

    def test_duplicate_highlight_ids_are_rejected(self) -> None:
        highlight = {
            "id": "h01",
            "inicio": 10,
            "fin": 140,
            "puntajes": {name: 8 for name in app_module.SCORE_WEIGHTS},
        }
        selection = {
            "highlights": [highlight, dict(highlight)],
            "descartados": [],
        }
        with self.assertRaises(app_module.ProcessingError):
            app_module.validate_selection(selection)

    def test_transcription_uses_plain_text_response(self) -> None:
        client = SimpleNamespace(
            models=SimpleNamespace(
                generate_content=lambda **kwargs: SimpleNamespace(
                    text="Texto hablado exactamente como aparece en el video."
                )
            )
        )
        highlight = {"id": "h01", "inicio": 102.0, "fin": 400.0}

        text = app_module.transcribe_highlight(
            client,
            "https://www.youtube.com/watch?v=_m8bqUD3jy4",
            highlight,
        )

        self.assertEqual(
            text, "Texto hablado exactamente como aparece en el video."
        )

    def test_empty_plain_text_transcription_is_rejected(self) -> None:
        client = SimpleNamespace(
            models=SimpleNamespace(
                generate_content=lambda **kwargs: SimpleNamespace(text="  ")
            )
        )
        highlight = {"id": "h01", "inicio": 102.0, "fin": 400.0}

        with self.assertRaisesRegex(
            app_module.ProcessingError, "no transcription for h01"
        ):
            app_module.transcribe_highlight(
                client,
                "https://www.youtube.com/watch?v=_m8bqUD3jy4",
                highlight,
            )


class RouteTests(unittest.TestCase):
    def setUp(self) -> None:
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()

    def test_get_renders_plain_html_form(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'name="youtube_url"', response.data)
        self.assertNotIn(b"<script", response.data)

    def test_invalid_url_returns_400(self) -> None:
        response = self.client.post("/", data={"youtube_url": "invalid"})
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"valid YouTube video URL", response.data)

    @patch("app.process_video")
    def test_valid_submission_renders_backend_output(self, process_video) -> None:
        process_video.return_value = {
            "highlights": [{"id": "h01", "texto": "Texto original"}],
            "descartados": [],
        }
        response = self.client.post(
            "/",
            data={
                "youtube_url": "https://www.youtube.com/watch?v=_m8bqUD3jy4"
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Texto original".encode(), response.data)
        rendered = response.get_data(as_text=True)
        self.assertIn("&#34;id&#34;: &#34;h01&#34;", rendered)

    @patch("app.select_highlights")
    @patch("app.create_gemini_client")
    def test_invalid_api_key_has_safe_message(self, create_client, select_highlights) -> None:
        create_client.return_value = object()
        select_highlights.side_effect = app_module.genai_errors.ClientError(
            400,
            {"error": {"message": "API key not valid", "status": "INVALID_ARGUMENT"}},
        )
        with self.assertRaisesRegex(
            app_module.ProcessingError, "GEMINI_API_KEY was rejected"
        ):
            app_module.process_video(
                "https://www.youtube.com/watch?v=_m8bqUD3jy4"
            )


if __name__ == "__main__":
    unittest.main()
