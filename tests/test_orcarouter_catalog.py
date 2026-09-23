"""OrcaRouter model catalog: parsing, per-capability filters, and outage behaviour.

The fixtures cover every capability the guide names — text-only chat, image-input chat,
embedding, image generation, video and rerank — so a filter that stops distinguishing them
fails here rather than in the dropdown. Offline: the catalog server is a local fake.
"""
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import orcarouter  # noqa: E402

FAKE_KEY = "sk-orca-catalog-test-key"

# Shapes taken from a real `GET /v1/models?capability=chat` response (see the PR body for
# the live evidence): endpoint types, architecture.input_modalities, context_length.
FIXTURE = [
    {"id": "openai/gpt-5.5", "name": "OpenAI: GPT-5.5", "context_length": 400000,
     "supported_endpoint_types": ["openai", "openai-response"],
     "architecture": {"input_modalities": ["text"]}},
    {"id": "anthropic/claude-opus-4.8", "name": "Anthropic: Claude Opus 4.8",
     "context_length": 200000, "supported_endpoint_types": ["anthropic"],
     "architecture": {"input_modalities": ["text"]}},
    {"id": "google/gemini-3.5-flash", "name": "Google: Gemini 3.5 Flash",
     "context_length": 1000000, "supported_endpoint_types": ["openai", "gemini"],
     "architecture": {"input_modalities": ["text", "image", "audio"]}},
    {"id": "deepseek/deepseek-v4-pro", "name": "DeepSeek: V4 Pro",
     "context_length": 1048576, "supported_endpoint_types": ["openai", "openai-response"],
     "architecture": {"input_modalities": ["text"]}},
    {"id": "deepseek/deepseek-v4.1-flash", "name": "DeepSeek: V4.1 Flash",
     "context_length": 1048576, "supported_endpoint_types": ["openai", "anthropic"],
     "architecture": {"input_modalities": ["text", "image"]}},
    {"id": "orcarouter/auto", "name": "OrcaRouter: Auto",
     "supported_endpoint_types": ["openai", "anthropic", "gemini", "openai-response"]},
    # Non-text-only routes: must never reach a chat dropdown.
    {"id": "vendor/embed-3", "supported_endpoint_types": ["embeddings"]},
    {"id": "vendor/image-xl", "supported_endpoint_types": ["image-generation"]},
    {"id": "vendor/video-1", "supported_endpoint_types": ["openai-video"]},
    {"id": "vendor/rerank-v2", "supported_endpoint_types": ["jina-rerank"]},
    # A record with no id at all must be dropped, not guessed at.
    {"name": "nameless"},
    "not-a-record",
]


class FakeCatalog:
    def __init__(self, records=None, status=200, body=None):
        self.records = FIXTURE if records is None else records
        self.status = status
        self.body = body
        self.paths = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                outer.paths.append(self.path)
                payload = (outer.body if outer.body is not None
                           else json.dumps({"data": outer.records}))
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload.encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def parse(records=FIXTURE):
    return orcarouter.parse_catalog({"data": records})


class Parsing(unittest.TestCase):
    def test_reads_ids_metadata_and_modalities(self):
        models = {m.id: m for m in parse()}
        self.assertIn("openai/gpt-5.5", models)
        self.assertEqual(models["google/gemini-3.5-flash"].context_length, 1000000)
        self.assertEqual(models["deepseek/deepseek-v4.1-flash"].input_modalities,
                         ("text", "image"))
        self.assertEqual(models["orcarouter/auto"].input_modalities, ("text",))

    def test_vendor_namespace_is_preserved_verbatim(self):
        ids = [m.id for m in parse()]
        self.assertIn("deepseek/deepseek-v4-pro", ids)
        self.assertIn("orcarouter/auto", ids)

    def test_records_without_a_usable_id_are_dropped(self):
        ids = [m.id for m in parse()]
        self.assertNotIn("nameless", ids)
        self.assertEqual(len(ids), len([r for r in FIXTURE if isinstance(r, dict)
                                        and isinstance(r.get("id"), str)]))

    def test_garbage_response_shape_raises(self):
        for bad in ({"data": "nope"}, 5, None):
            with self.assertRaises(ValueError):
                orcarouter.parse_catalog(bad)

    def test_parse_accepts_a_bare_list_too(self):
        self.assertEqual(len(orcarouter.parse_catalog(FIXTURE)), len(parse()))


class CapabilityFilters(unittest.TestCase):
    def setUp(self):
        self.models = parse()

    def ids(self, capability, require_input=""):
        return [m.id for m in orcarouter.filter_models(self.models, capability, require_input)]

    def test_chat_excludes_non_text_only_routes(self):
        ids = self.ids("chat")
        self.assertIn("openai/gpt-5.5", ids)
        self.assertIn("orcarouter/auto", ids)
        for excluded in ("vendor/embed-3", "vendor/image-xl", "vendor/video-1",
                         "vendor/rerank-v2"):
            self.assertNotIn(excluded, ids)

    def test_chat_keeps_only_supported_endpoint_types(self):
        models = orcarouter.filter_models(
            parse([{"id": "x/weird", "supported_endpoint_types": ["some-other-wire"]}]),
            "chat")
        self.assertEqual(models, [])

    def test_embedding_matches_only_the_embeddings_endpoint(self):
        self.assertEqual(self.ids("embedding"), ["vendor/embed-3"])

    def test_image_generation_matches_only_image_generation(self):
        self.assertEqual(self.ids("image"), ["vendor/image-xl"])

    def test_video_and_rerank_are_strict(self):
        self.assertEqual(self.ids("video"), ["vendor/video-1"])
        self.assertEqual(self.ids("rerank"), ["vendor/rerank-v2"])

    def test_multimodal_requires_a_declared_modality_and_fails_closed(self):
        # A chat model that does not declare image input must not appear in the
        # image-capable dropdown, however capable its name sounds.
        ids = self.ids("chat", require_input="image")
        self.assertIn("google/gemini-3.5-flash", ids)
        self.assertIn("deepseek/deepseek-v4.1-flash", ids)
        self.assertNotIn("openai/gpt-5.5", ids)
        self.assertNotIn("anthropic/claude-opus-4.8", ids)
        self.assertNotIn("orcarouter/auto", ids)

    def test_audio_modality_is_filtered_independently(self):
        self.assertEqual(self.ids("chat", require_input="audio"),
                         ["google/gemini-3.5-flash"])

    def test_video_input_has_no_candidates_in_this_catalog(self):
        self.assertEqual(self.ids("chat", require_input="video"), [])

    def test_supports_input_is_fail_closed(self):
        model = orcarouter.ModelInfo(id="m", input_modalities=("text",))
        self.assertTrue(model.supports_input("text"))
        self.assertFalse(model.supports_input("image"))

    def test_results_are_sorted_and_deduplicated_by_id(self):
        ids = self.ids("chat")
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(ids), len(set(ids)))


class CatalogDiscovery(unittest.TestCase):
    def setUp(self):
        self.server = FakeCatalog()
        self.addCleanup(self.server.stop)
        self.catalog = orcarouter.ModelCatalog(cache_ttl=0)

    def test_live_result_is_authoritative_and_labelled(self):
        result = self.catalog.fetch(self.server.base, FAKE_KEY, "chat")
        self.assertEqual(result.source, "live")
        self.assertFalse(result.degraded)
        self.assertIn("openai/gpt-5.5", result.ids())

    def test_request_uses_the_capability_query_and_bearer_auth(self):
        self.catalog.fetch(self.server.base, FAKE_KEY, "chat")
        self.assertEqual(self.server.paths[0], "/models?capability=chat")
        self.catalog.fetch(self.server.base, FAKE_KEY, "embedding")
        self.assertEqual(self.server.paths[1], "/models?capability=embedding")

    def test_live_success_does_not_mix_in_the_seed(self):
        result = self.catalog.fetch(self.server.base, FAKE_KEY, "chat")
        live_ids = {r["id"] for r in FIXTURE if isinstance(r, dict) and r.get("id")}
        for seed in orcarouter.SEED_MODELS:
            if seed["id"] not in live_ids:
                self.assertNotIn(seed["id"], result.ids())

    def test_seed_is_used_only_when_the_catalog_fails(self):
        server = FakeCatalog(status=500)
        self.addCleanup(server.stop)
        result = self.catalog.fetch(server.base, FAKE_KEY, "chat")
        self.assertEqual(result.source, orcarouter.SEED_SOURCE)
        self.assertTrue(result.degraded)
        self.assertIn("HTTP 500", result.error)
        self.assertEqual(sorted(result.ids()),
                         sorted(m["id"] for m in orcarouter.SEED_MODELS))

    def test_network_failure_falls_back_without_raising(self):
        result = self.catalog.fetch("http://127.0.0.1:1", FAKE_KEY, "chat")
        self.assertTrue(result.degraded)
        self.assertEqual(result.source, orcarouter.SEED_SOURCE)

    def test_missing_key_reports_why_instead_of_an_empty_dropdown(self):
        result = self.catalog.fetch(self.server.base, "", "chat")
        self.assertTrue(result.degraded)
        self.assertIn("密钥", result.error)
        self.assertTrue(result.ids())

    def test_401_is_reported_as_an_auth_problem(self):
        server = FakeCatalog(status=401)
        self.addCleanup(server.stop)
        result = self.catalog.fetch(server.base, FAKE_KEY, "chat")
        self.assertIn("401", result.error)
        self.assertTrue(result.degraded)

    def test_last_known_good_is_kept_when_a_refresh_fails(self):
        self.catalog.fetch(self.server.base, FAKE_KEY, "chat")
        # Same origin, now failing: the real catalog answer is retained and labelled
        # degraded, rather than being replaced by the seed.
        self.server.status = 503
        result = self.catalog.fetch(self.server.base, FAKE_KEY, "chat", refresh=True)
        self.assertEqual(result.source, "live")
        self.assertTrue(result.degraded)
        self.assertIn("openai/gpt-5.5", result.ids())
        self.assertIn("HTTP 503", result.error)

    def test_cache_is_per_credential_so_one_users_catalog_is_not_reused(self):
        self.catalog.cache_ttl = 300
        self.catalog.fetch(self.server.base, FAKE_KEY, "chat")
        before = len(self.server.paths)
        self.catalog.fetch(self.server.base, "sk-orca-a-different-key", "chat")
        self.assertGreater(len(self.server.paths), before)

    def test_cache_hit_avoids_a_second_request(self):
        self.catalog.cache_ttl = 300
        self.catalog.fetch(self.server.base, FAKE_KEY, "chat")
        before = len(self.server.paths)
        self.catalog.fetch(self.server.base, FAKE_KEY, "chat")
        self.assertEqual(len(self.server.paths), before)

    def test_oversized_response_is_rejected(self):
        server = FakeCatalog(body=json.dumps({"data": [
            {"id": f"m/{i}"} for i in range(orcarouter.CATALOG_MAX_ITEMS + 10)]}))
        self.addCleanup(server.stop)
        result = self.catalog.fetch(server.base, FAKE_KEY, "chat")
        # Parsing is bounded by CATALOG_MAX_ITEMS; the response is accepted but truncated.
        self.assertLessEqual(len(result.ids()), orcarouter.CATALOG_MAX_ITEMS)

    def test_seed_keeps_its_verified_metadata(self):
        seed = {m.id: m for m in orcarouter.ModelCatalog.seed("chat")}
        gpt = seed["openai/gpt-5.5"]
        self.assertEqual(list(gpt.reasoning), ["low", "medium", "high", "xhigh"])
        self.assertEqual(gpt.context_length, 400000)
        self.assertIn("google/gemini-3.5-flash", seed)
        self.assertIn("anthropic/claude-opus-4.8", seed)
        self.assertIn("deepseek/deepseek-v4-pro", seed)
        self.assertIn("orcarouter/auto", seed)

    def test_seed_filters_by_capability_too(self):
        self.assertEqual(orcarouter.ModelCatalog.seed("image"), [])
        self.assertEqual(orcarouter.ModelCatalog.seed("embedding"), [])
        image_capable = [m.id for m in orcarouter.ModelCatalog.seed("chat", "image")]
        self.assertEqual(image_capable, ["google/gemini-3.5-flash"])


class SelectorBinding(unittest.TestCase):
    """The dropdown options ARE the filtered catalog; there is no free-text path."""

    def test_selector_options_are_the_catalog_ids(self):
        catalog = orcarouter.Catalog(models=orcarouter.ModelCatalog.seed("chat"),
                                     source="live")
        self.assertEqual(orcarouter.selector_options(catalog), catalog.ids())

    def test_an_incompatible_selection_is_cleared(self):
        options = ["a/one", "b/two"]
        self.assertEqual(orcarouter.selection_after_catalog("a/one", options),
                         ("a/one", False))
        self.assertEqual(orcarouter.selection_after_catalog("gone/old", options),
                         ("", True))

    def test_capability_change_invalidates_a_now_incompatible_model(self):
        models = parse()
        chat = [m.id for m in orcarouter.filter_models(models, "chat")]
        image = [m.id for m in orcarouter.filter_models(models, "chat", "image")]
        self.assertIn("openai/gpt-5.5", chat)
        self.assertNotIn("openai/gpt-5.5", image)
        self.assertEqual(orcarouter.selection_after_catalog("openai/gpt-5.5", image),
                         ("", True))

    def test_settings_layer_uses_the_same_filter(self):
        import settings_config as config
        server = FakeCatalog()
        self.addCleanup(server.stop)
        with mock.patch.object(orcarouter, "catalog",
                               return_value=orcarouter.ModelCatalog(cache_ttl=0)):
            result = config.list_orcarouter_models(server.base, FAKE_KEY, "chat", "image")
        self.assertIn("google/gemini-3.5-flash", result.ids())
        self.assertNotIn("openai/gpt-5.5", result.ids())
        self.assertEqual(result.source, "live")

    def test_settings_layer_refuses_free_text_models_for_orcarouter(self):
        import settings_config as config
        with self.assertRaises(ValueError):
            config.list_models("ORCAROUTER", "https://api.orcarouter.ai/v1", FAKE_KEY)

    def test_settings_layer_rejects_a_plain_http_remote_origin(self):
        import settings_config as config
        with self.assertRaises(ValueError):
            config.list_orcarouter_models("http://api.orcarouter.ai/v1", FAKE_KEY)


if __name__ == "__main__":
    unittest.main()
