import pathlib
import time
import unittest
from unittest import mock

from api import state


class VercelRadarTests(unittest.TestCase):
    def test_cloud_excludes_arbitrary_custom_monitors(self):
        settings = state.sanitize_settings({"custom_monitors": [{"name": "Unsafe", "url": "http://127.0.0.1"}]})
        self.assertEqual(settings["custom_monitors"], [])

    def test_continuity_is_bounded(self):
        raw = {"seen": {"deals": [str(x) for x in range(7000)]}, "settings": {"watchlist": ["X"] * 100}}
        continuity = state.new_continuity(raw)
        self.assertEqual(len(continuity["seen"]["deals"]), state.MAX_SEEN)
        self.assertEqual(len(continuity["settings"]["watchlist"]), 30)

    def test_service_transition_creates_notification_event(self):
        c = state.new_continuity({"service_states": {"epic": "operational"}})
        events = []
        item = {"id": "epic", "name": "Epic Games", "status": "major", "description": "Outage", "url": "https://status.test"}
        state.process_services(c, [item], events)
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["notify"])
        self.assertEqual(events[0]["category"], "service_incidents")

    def test_first_content_result_silently_baselines(self):
        c = state.new_continuity({})
        events = []
        state.process_items(c, "epic_free", "free_games", [{"id": "free:1", "title": "Old", "status": "free_now"}], events)
        self.assertTrue(c["baselined"]["epic_free"])
        self.assertFalse(any(x.get("notify") for x in events))

    def test_price_rule_matching(self):
        settings = state.sanitize_settings({"price_rules": [{"keyword": "Portal", "min_discount": 75, "max_price": 5, "enabled": True}]})
        self.assertTrue(state.price_matches(settings, {"title": "Portal Bundle", "discount": 80, "sale_price": "$4.99"}))
        self.assertFalse(state.price_matches(settings, {"title": "Portal Bundle", "discount": 80, "sale_price": "$6.00"}))

    def test_no_due_continuity_refresh_uses_zero_providers(self):
        now = time.time()
        meta = {name: {"ok": True, "last_success": now, "last_attempt": now, "items": 0} for name in state.ALLOWED_PROVIDERS}
        cache = {name: [] for name in state.ALLOWED_PROVIDERS if name not in {"steam_featured", "live_radar"}}
        cache["steam"] = {}
        cache["live"] = {}
        prior = {"settings": state.sanitize_settings({}), "provider_meta": meta, "cache": cache, "settings_fingerprint": state.fingerprint(state.sanitize_settings({}))}
        with mock.patch.object(state, "execute_provider") as execute:
            result = state.build_state({"continuity": prior})
        execute.assert_not_called()
        self.assertTrue(result["serverlessMode"])
        self.assertEqual(result["stats"]["providers_total"], len(state.ALLOWED_PROVIDERS))

    def test_frontend_uses_post_and_localstorage(self):
        source = pathlib.Path(__file__).parents[1].joinpath("index.html").read_text(encoding="utf-8")
        self.assertIn("localStorage.setItem", source)
        self.assertIn("method:'POST'", source)
        self.assertIn("Notification.requestPermission", source)
        self.assertIn("setInterval(()=>refresh(false),30000)", source)
        self.assertNotIn("setInterval(refresh,2500)", source)

    def test_vercel_and_pwa_files(self):
        root = pathlib.Path(__file__).parents[1]
        self.assertTrue((root / "vercel.json").exists())
        self.assertTrue((root / "manifest.webmanifest").exists())
        self.assertTrue((root / "service-worker.js").exists())
        self.assertIn("maxDuration", (root / "vercel.json").read_text())


if __name__ == "__main__":
    unittest.main()
