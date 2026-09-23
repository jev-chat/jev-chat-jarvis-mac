"""Live OrcaRouter checks through the integration's own code path.

Needs `ORCAROUTER_API_KEY` in the environment. Without it every test here skips, so the
offline suite stays runnable — but the point of the file is the opposite: it proves the
provider this PR adds really talks to OrcaRouter, using the same functions the app uses
(`orcarouter.chat_request`, `orcarouter.ModelCatalog`, `generate.Generator`), not a bare
`curl` beside them.

No credential is printed, and no request is sent to a model the workspace cannot reach:
the model is chosen from the live catalog the same way the settings window chooses it.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import generate          # noqa: E402
import orcarouter        # noqa: E402
import settings_config as config   # noqa: E402
import userconfig        # noqa: E402

KEY = os.environ.get("ORCAROUTER_API_KEY", "")
API_BASE = orcarouter.API_BASE_DEFAULT
AUTH_BASE = orcarouter.AUTH_BASE_DEFAULT

requires_key = unittest.skipUnless(KEY, "ORCAROUTER_API_KEY is not set")


class LiveCatalog(unittest.TestCase):
    """The authoritative catalog, over the same client the dropdown uses."""

    @requires_key
    def test_chat_catalog_is_fetched_live(self):
        catalog = orcarouter.ModelCatalog(cache_ttl=0).fetch(API_BASE, KEY, "chat")
        self.assertEqual(catalog.source, "live", catalog.error)
        self.assertFalse(catalog.degraded, catalog.error)
        self.assertGreater(len(catalog.ids()), 0)
        print(f"\n[live] chat catalog: {len(catalog.ids())} models from {API_BASE}")

    @requires_key
    def test_every_chat_model_carries_a_supported_endpoint_type(self):
        catalog = orcarouter.ModelCatalog(cache_ttl=0).fetch(API_BASE, KEY, "chat")
        for model in catalog.models:
            with self.subTest(model=model.id):
                self.assertTrue(set(model.endpoint_types) & set(orcarouter.CHAT_ENDPOINT_TYPES),
                                f"{model.id} advertises {model.endpoint_types}")
                self.assertFalse(set(model.endpoint_types) <= set(
                    orcarouter.NON_TEXT_ENDPOINT_TYPES), model.id)

    @requires_key
    def test_model_ids_keep_their_vendor_namespace(self):
        catalog = orcarouter.ModelCatalog(cache_ttl=0).fetch(API_BASE, KEY, "chat")
        for model in catalog.models:
            with self.subTest(model=model.id):
                self.assertIn("/", model.id)

    @requires_key
    def test_text_selector_contains_only_chat_models(self):
        catalog = orcarouter.ModelCatalog(cache_ttl=0).fetch(API_BASE, KEY, "chat")
        options = orcarouter.selector_options(catalog)
        self.assertEqual(options, catalog.ids())
        self.assertTrue(all(isinstance(o, str) and o for o in options))
        print(f"[live] text dropdown options: {len(options)}")

    @requires_key
    def test_image_selector_is_the_subset_that_declares_image_input(self):
        catalog = orcarouter.ModelCatalog(cache_ttl=0).fetch(
            API_BASE, KEY, "chat", require_input="image")
        for model in catalog.models:
            with self.subTest(model=model.id):
                self.assertIn("image", model.input_modalities)
        # Every option is a chat model as well: the multimodal filter is chat first.
        chat = set(orcarouter.ModelCatalog(cache_ttl=0).fetch(API_BASE, KEY, "chat").ids())
        self.assertTrue(set(catalog.ids()) <= chat)
        print(f"[live] image-capable chat options: {len(catalog.ids())}")

    @requires_key
    def test_catalog_through_the_settings_layer_is_the_same_list(self):
        catalog = config.list_orcarouter_models(API_BASE, KEY, "chat")
        self.assertEqual(catalog.source, "live", catalog.error)
        self.assertTrue(catalog.ids())

    def test_wrong_auth_path_is_not_used_anywhere(self):
        # The documented trap: the relay's /v1/auth/keys is a 301. Our exchange URL must be
        # on the auth origin, and no code path may build the inference-origin one. The
        # module docstring names the wrong URL as a counter-example, which is documentation
        # and not an implementation — so comments and docstrings are excluded here.
        import io
        import tokenize
        with mock.patch.object(userconfig, "get", return_value=""):
            self.assertEqual(orcarouter.exchange_url(orcarouter.auth_base()),
                             f"{AUTH_BASE}/api/v1/auth/keys")
        source = Path(orcarouter.__file__).read_text()
        code = []
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type not in (tokenize.COMMENT, tokenize.STRING):
                code.append(token.string)
        self.assertNotIn("api.orcarouter.ai/v1/auth", " ".join(code))


class LiveInference(unittest.TestCase):
    """A real completion through the generation layer, with a model the catalog offers."""

    def _usable_model(self):
        catalog = orcarouter.ModelCatalog(cache_ttl=0).fetch(API_BASE, KEY, "chat")
        self.assertTrue(catalog.ids(), catalog.error)
        # Pick from the live list, preferring a vendor model: workspace-scoped routing
        # models such as orcarouter/auto are entitlement-dependent per key.
        for model in catalog.ids():
            if model.startswith(("deepseek/", "openai/", "anthropic/", "google/")):
                return model
        return catalog.ids()[0]

    @requires_key
    def test_chat_request_returns_text(self):
        model = self._usable_model()
        try:
            data = orcarouter.chat_request(
                API_BASE, KEY, model,
                [{"role": "user", "content": "请只回复：连接成功"}], max_tokens=64)
        except Exception as exc:                       # noqa: BLE001 - reported, not hidden
            if _is_entitlement_error(exc):
                self.skipTest(f"{model} is not entitled for this key")
            raise
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        self.assertTrue(content.strip(), data)
        print(f"[live] {model} -> {content.strip()[:40]!r}")

    @requires_key
    def test_generator_reaches_orcarouter_without_openai_or_anthropic_config(self):
        """The product's own path: config only ORCAROUTER_*, then generate a candidate."""
        import tempfile
        model = self._usable_model()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "env"
            path.write_text(f"ORCAROUTER_API_KEY={KEY}\nORCAROUTER_MODEL={model}\n")
            with mock.patch.object(userconfig, "_startup_sources", None), \
                    mock.patch.object(userconfig, "env_files", return_value=[path]), \
                    mock.patch.dict(os.environ, {}, clear=True):
                userconfig.load()
                base, key, resolved, source, api = generate.load_credentials()
                self.assertEqual(base, API_BASE)
                self.assertEqual(key, KEY)
                self.assertEqual(resolved, model)
                self.assertEqual(api, "openai")
                self.assertIn("OrcaRouter", source)
                self.assertNotIn(KEY, source)          # the source label never holds a key
                result = generate.Generator(timeout=60).generate(
                    "这个需求你今天跟一下", "派活", ["高情商话术"])
                texts = [t for group in result["groups"] for t in group["texts"]]
                if not texts and _is_entitlement_error_text(result.get("error", "")):
                    self.skipTest(f"{model} is not entitled for this key")
                self.assertTrue(texts, result)
                print(f"[live] generation through OrcaRouter: {len(texts)} candidate(s)")

    @requires_key
    def test_streaming_path_is_the_one_used_for_candidates(self):
        model = self._usable_model()
        generator = generate.Generator(timeout=60)
        with mock.patch.object(generate, "load_credentials",
                                        return_value=(API_BASE, KEY, model, "test", "openai")):
            seen = []
            try:
                raw = generator._call("只回复两个字：收到",
                                      on_delta=seen.append)
            except Exception as exc:                   # noqa: BLE001
                if _is_entitlement_error(exc):
                    self.skipTest(f"{model} is not entitled for this key")
                raise
        self.assertTrue(raw.strip())
        print(f"[live] streamed {len(seen)} delta(s), {len(raw)} chars total")


def _is_entitlement_error(exc: Exception) -> bool:
    import urllib.error
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code != 403:
            return False
        body = exc.read(400).decode(errors="replace")
        return "model_access_denied" in body or "does not have access" in body
    return False


def _is_entitlement_error_text(text: str) -> bool:
    return "403" in text and ("access" in text or "denied" in text)


if __name__ == "__main__":
    unittest.main()
