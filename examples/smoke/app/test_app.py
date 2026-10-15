import unittest
import urllib.request


class SmokeTests(unittest.TestCase):
    def test_python_runtime(self):
        self.assertGreaterEqual(__import__("sys").version_info.major, 3)

    def test_http_server(self):
        # The existing web server is available in exec mode. A temporary run
        # container can also contact it over the shared Compose network.
        url = "http://web:8000/health"
        with urllib.request.urlopen(url, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"devstack smoke ok\n")


if __name__ == "__main__":
    unittest.main()
