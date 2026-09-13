import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FollowingStaticTests(unittest.TestCase):
    def test_following_page_files_are_removed(self):
        self.assertFalse((ROOT / "static" / "following.html").exists())
        self.assertFalse((ROOT / "static" / "following.js").exists())

    def test_header_no_longer_links_to_following(self):
        header = (ROOT / "static" / "header.html").read_text()
        self.assertNotIn('href="/following"', header)
        self.assertNotIn('data-page="following"', header)

    def test_following_route_redirects_to_media(self):
        main = (ROOT / "app" / "main.py").read_text()
        self.assertIn('@app.get("/following")', main)
        self.assertIn('RedirectResponse(url="/media"', main)
        self.assertNotIn('FileResponse(str(STATIC_DIR / "following.html"))', main)


if __name__ == "__main__":
    unittest.main()
