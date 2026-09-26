import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fetch import configured_proxy, fetch


class ProxyConfigurationTests(unittest.TestCase):
    def test_optional_proxy_is_read_from_local_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            def retrieve(command, **kwargs):
                self.assertEqual(command[1:3], ['--proxy', 'socks5h://proxy.example:9050'])
                Path(command[command.index('--output') + 1]).write_text('<title>Homes</title><p>For sale</p>')
                return subprocess.CompletedProcess(command, 0, json.dumps({'http_code': 200}), '')
            with patch('fetch.public_url'), patch.dict('os.environ', {'DUNDEE_TOR_PROXY': 'socks5h://proxy.example:9050'}), patch('fetch.subprocess.run', side_effect=retrieve):
                result = fetch('https://agent.example/', Path(temp) / 'snapshot.json', tor=True)
            self.assertEqual(result['status'], 200)
            self.assertEqual(result['error'], '')
            self.assertNotIn('proxy.example', json.dumps(result))

    def test_missing_proxy_fails_optional_retry_without_a_network_request(self):
        with tempfile.TemporaryDirectory() as temp, patch('fetch.Path.home', return_value=Path(temp)), patch('fetch.public_url'), patch.dict('os.environ', {}, clear=True), patch('fetch.subprocess.run') as network:
            result = fetch('https://agent.example/', Path(temp) / 'snapshot.json', tor=True)
            network.assert_not_called()
            self.assertIn('DUNDEE_TOR_PROXY is not configured', result['error'])

    def test_manual_calls_use_same_private_config_and_environment_can_override(self):
        with tempfile.TemporaryDirectory() as temp, patch('fetch.Path.home', return_value=Path(temp)), patch.dict('os.environ', {}, clear=True):
            config = Path(temp) / '.config/dundee-property-watch.env'
            config.parent.mkdir()
            config.write_text('# Local only\nDUNDEE_TOR_PROXY="socks5h://proxy.example:9050"\n')
            self.assertEqual(configured_proxy(), 'socks5h://proxy.example:9050')
            with patch.dict('os.environ', {'DUNDEE_TOR_PROXY': 'socks5h://other.example:9050'}):
                self.assertEqual(configured_proxy(), 'socks5h://other.example:9050')
