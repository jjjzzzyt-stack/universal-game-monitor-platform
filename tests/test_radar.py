import json
import pathlib
import tempfile
import unittest
from unittest import mock

import radar


class FakeNotifier:
    backend = "test"

    def __init__(self):
        self.sent = []

    def send(self, title, message, url=""):
        self.sent.append((title, message, url))
        return True, "test toast sent"


class RadarTests(unittest.TestCase):
    def make_app(self, temp):
        root = pathlib.Path(temp)
        patches = [
            mock.patch.object(radar, "DATA_DIR", root),
            mock.patch.object(radar, "STATE_PATH", root / "state.json"),
            mock.patch.object(radar, "EVENTS_PATH", root / "events.jsonl"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        app = radar.RadarApp()
        app.notifier = FakeNotifier()
        return app

    def test_india_region_and_price_format(self):
        self.assertIn("country=IN", radar.URLS["epic_free"])
        self.assertIn("cc=in", radar.URLS["steam_featured"])
        self.assertEqual(radar.format_minor_price(22200, "INR"), "₹222.00")

    def test_status_mappings(self):
        self.assertEqual(radar.statuspage_state("none"), "operational")
        self.assertEqual(radar.statuspage_state("critical"), "major")
        self.assertEqual(radar.component_state("degraded_performance"), "degraded")
        self.assertEqual(radar.xbox_state("None"), "operational")
        self.assertEqual(radar.xbox_state("MajorOutage"), "major")

    def test_epic_free_parser(self):
        fixture = {"data": {"Catalog": {"searchStore": {"elements": [{
            "id": "abc", "title": "Test Game", "description": "Free fixture",
            "offerMappings": [{"pageSlug": "test-game"}],
            "keyImages": [{"type": "OfferImageWide", "url": "https://image.test/wide.jpg"}],
            "price": {"totalPrice": {"fmtPrice": {"originalPrice": "$19.99"}}},
            "promotions": {"promotionalOffers": [{"promotionalOffers": [{
                "startDate": "2026-01-01T00:00:00.000Z", "endDate": "2099-01-01T00:00:00.000Z",
                "discountSetting": {"discountPercentage": 0}
            }]}], "upcomingPromotionalOffers": []}
        }]}}}}
        with mock.patch.object(radar, "fetch_json", return_value=fixture):
            result = radar.provider_epic_free()
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["title"], "Test Game")
        self.assertEqual(result["items"][0]["status"], "free_now")
        self.assertIn("test-game", result["items"][0]["url"])

    def test_live_parser_accepts_only_current_streams(self):
        fixture = {"items": [
            {"type": "stream", "duration": -1, "url": "/watch?v=abcdefghijk", "title": "Fortnite ranked live", "uploaderName": "Player", "views": 42, "thumbnail": "x"},
            {"type": "stream", "duration": 2000, "url": "/watch?v=endedstream", "title": "Fortnite replay", "uploaderName": "Player"},
            {"type": "video", "duration": 20, "url": "/watch?v=normalvideo", "title": "Fortnite video", "uploaderName": "Player"},
            {"type": "stream", "duration": -1, "url": "/watch?v=zzzzzzzzzzz", "title": "Unrelated cooking live", "uploaderName": "Cook"},
        ]}
        with mock.patch.object(radar, "fetch_json", return_value=fixture):
            result = radar.provider_live_game("Fortnite")
        self.assertEqual([x["video_id"] for x in result["items"]], ["abcdefghijk"])

    def test_first_result_baselines_and_later_free_game_notifies(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.make_app(temp)
            first = {"id": "free:1", "title": "Old Free Game", "status": "free_now", "store": "Store", "url": "https://example.test/1"}
            app._process_new_items("epic_free", "free_games", [first])
            self.assertEqual(app.notifier.sent, [])
            second = {"id": "free:2", "title": "New Free Game", "status": "free_now", "store": "Store", "url": "https://example.test/2"}
            app._process_new_items("epic_free", "free_games", [first, second])
            self.assertEqual(len(app.notifier.sent), 1)
            self.assertIn("New Free Game", app.notifier.sent[0][1])

    def test_service_incident_and_recovery_notify_once(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.make_app(temp)
            bad = radar.service_item("svc", "Service", "major", "https://status.test", "Issue", [], "Official")
            app._process_services([bad])
            self.assertEqual(len(app.notifier.sent), 1)
            app._process_services([bad])
            self.assertEqual(len(app.notifier.sent), 1)
            good = dict(bad, status="operational")
            app._process_services([good])
            self.assertEqual(len(app.notifier.sent), 2)
            self.assertIn("recovered", app.notifier.sent[-1][0].lower())

    def test_settings_and_watchlist_persist(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.make_app(temp)
            app.update_settings({"deal_min_discount": 200, "notifications": {"deals": True}})
            self.assertEqual(app.persist["settings"]["deal_min_discount"], 100)
            self.assertTrue(app.persist["settings"]["notifications"]["deals"])
            app.update_watchlist("add", "Test Game")
            self.assertIn("Test Game", app.persist["settings"]["watchlist"])
            app.update_watchlist("remove", "test game")
            self.assertNotIn("Test Game", app.persist["settings"]["watchlist"])
            saved = json.loads((pathlib.Path(temp) / "state.json").read_text())
            self.assertEqual(saved["settings"]["deal_min_discount"], 100)

    def test_twitch_and_roblox_status_parsers(self):
        twitch = {"status": {"indicator": "none", "description": "All Systems Operational"}, "components": [{"name": "Video", "status": "operational"}]}
        roblox = {"result": {"status_overall": {"status": "Operational", "updated": "now"}, "status": [{"name": "Player", "status": "Operational", "containers": []}]}}
        with mock.patch.object(radar, "fetch_json", return_value=twitch):
            self.assertEqual(radar.provider_twitch_status()["items"][0]["status"], "operational")
        with mock.patch.object(radar, "fetch_json", return_value=roblox):
            self.assertEqual(radar.provider_roblox_status()["items"][0]["status"], "operational")

    def test_catalog_gog_and_news_parsers(self):
        catalog = [{"id": 1, "title": "Free Game", "genre": "Shooter", "platform": "PC", "publisher": "Studio", "game_url": "https://game.test", "thumbnail": "x"}]
        with mock.patch.object(radar, "fetch_json", return_value=catalog):
            self.assertEqual(radar.provider_free_to_play()["items"][0]["genre"], "Shooter")
        gog = {"products": [{"id": "2", "title": "GOG Game", "price": {"final": "$1.00", "base": "$10.00", "discount": "-90%"}, "storeLink": "https://gog.test", "genres": []}]}
        with mock.patch.object(radar, "fetch_json", return_value=gog):
            item = radar.provider_gog_deals()["items"][0]
            self.assertEqual(item["discount"], 90)
            self.assertEqual(item["store"], "GOG")
        steam = {"appnews": {"newsitems": [{"gid": "3", "title": "Patch", "url": "https://steam.test", "date": 1, "contents": "<b>Fixed</b> game"}]}}
        with mock.patch.object(radar, "fetch_json", return_value=steam):
            news = radar.provider_steam_news([{"id": "730", "name": "Counter-Strike 2"}])["items"]
            self.assertEqual(news[0]["game"], "Counter-Strike 2")
            self.assertEqual(news[0]["contents"], "Fixed game")

    def test_private_custom_monitor_urls_are_blocked(self):
        for value in ("http://localhost:8000", "http://127.0.0.1/test", "file:///tmp/test"):
            with self.assertRaises(ValueError):
                radar.safe_public_url(value)

    def test_favorites_price_rules_and_export(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.make_app(temp)
            item = {"id": "deal:1", "title": "Portal Collection", "url": "https://example.test", "sale_price": "$4.99", "discount": 80}
            app.toggle_favorite(item)
            self.assertEqual(len(app.persist["settings"]["favorites"]), 1)
            app.toggle_favorite(item)
            self.assertEqual(len(app.persist["settings"]["favorites"]), 0)
            rules = app.update_price_rule("add", {"keyword": "Portal", "min_discount": 75, "max_price": 5})
            self.assertEqual(len(rules), 1)
            self.assertEqual(len(app._matching_price_rules(item)), 1)
            exported = app.export_data()
            self.assertIn("settings", exported)
            self.assertIn("seen", exported)

    def test_calendar_and_csv_exports(self):
        state = {"free_games": [{"id": "free:1", "title": "Game", "status": "upcoming", "start_at": "2099-01-01T00:00:00Z", "store": "Store", "url": "https://example.test"}], "deals": [{"title": "Deal", "store": "Store", "sale_price": "$1", "normal_price": "$10", "discount": 90, "url": "https://example.test", "source": "Test"}]}
        self.assertIn(b"BEGIN:VCALENDAR", radar.build_calendar(state))
        csv_data = radar.build_deals_csv(state)
        self.assertIn(b"Discount percent", csv_data)
        self.assertIn(b"Deal", csv_data)

    def test_winotify_uses_real_action_api_and_sound(self):
        calls = []

        class Toast:
            def __init__(self, **kwargs): calls.append(("init", kwargs))
            def set_audio(self, sound, loop): calls.append(("audio", sound, loop))
            def add_actions(self, **kwargs): calls.append(("action", kwargs))
            def show(self): calls.append(("show",))

        class FakeWinotify:
            Notification = Toast
            class audio:
                Default = "default"

        manager = radar.NotificationManager()
        manager._winotify = FakeWinotify
        with mock.patch.object(radar.os, "name", "nt"):
            ok, _ = manager.send("Title", "Message", "https://example.test")
        self.assertTrue(ok)
        self.assertIn(("audio", "default", False), calls)
        self.assertIn(("action", {"label": "Open", "launch": "https://example.test"}), calls)
        self.assertEqual(calls[-1], ("show",))

    def test_dashboard_player_is_click_to_play_and_stable(self):
        root = pathlib.Path(__file__).parents[1]
        page = root / "dashboard.html" if (root / "dashboard.html").exists() else root / "index.html"
        source = page.read_text(encoding="utf-8")
        self.assertIn("youtube-nocookie.com/embed/", source)
        self.assertIn("autoplay=0", source)
        self.assertNotIn("autoplay=1", source)
        self.assertIn("playerId!==id", source)
        self.assertIn("allowfullscreen", source)
        self.assertIn("manifest.webmanifest", source)
        self.assertTrue((pathlib.Path(__file__).parents[1] / "service-worker.js").exists())

    def test_no_browser_auto_open_in_backend(self):
        source = (pathlib.Path(__file__).parents[1] / "radar.py").read_text(encoding="utf-8")
        self.assertNotIn("import webbrowser", source)
        self.assertNotIn("webbrowser.open", source)


if __name__ == "__main__":
    unittest.main()
