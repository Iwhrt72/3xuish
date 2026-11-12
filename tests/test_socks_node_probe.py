"""Probe acceptance and DNS packet regressions without external network access."""
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
import io
import ipaddress
import json
import os
from pathlib import Path
import struct
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import socks_node_probe as probe


def name(value):
    return b''.join(bytes((len(part),)) + part.encode('ascii') for part in value.split('.')) + b'\0'


def record(owner, kind, data, record_class=1):
    return owner + struct.pack('!HHIH', kind, record_class, 60, len(data)) + data


def response(records, *, ident=1234, flags=0x8180, extra=b'', additional=0):
    return (struct.pack('!6H', ident, flags, 1, len(records), 0, additional)
            + probe.DNS_QUESTION + b''.join(records) + extra)


class DnsAnswers(unittest.TestCase):
    def test_accepts_matching_compressed_a_record(self):
        packet = response([record(b'\xc0\x0c', 1, ipaddress.ip_address('192.0.2.1').packed)])
        probe._check_dns(packet, 1234)

    def test_accepts_cname_chain_ending_in_a_record(self):
        packet = response([
            record(b'\xc0\x0c', 5, name('alias.example.com')),
            record(name('alias.example.com'), 1, b'\xc0\x00\x02\x01'),
        ])
        probe._check_dns(packet, 1234)

    def test_accepts_edns_additional_record(self):
        packet = response([record(b'\xc0\x0c', 1, b'\xc0\x00\x02\x01')],
                          extra=record(b'\0', 41, b'', record_class=1232), additional=1)
        probe._check_dns(packet, 1234)

    def test_rejects_fake_answer_that_previously_passed_length_check(self):
        packet = struct.pack('!6H', 1234, 0x8180, 1, 1, 0, 0) + probe.DNS_QUESTION + b'\0' * 11
        with self.assertRaisesRegex(probe.ProbeError, 'no A record'):
            probe._check_dns(packet, 1234)

    def test_rejects_empty_wrong_sized_and_truncated_a_records(self):
        for payload in (b'', b'\1', b'\1\2\3', b'\1\2\3\4\5'):
            with self.subTest(size=len(payload)), self.assertRaises(probe.ProbeError):
                probe._check_dns(response([record(b'\xc0\x0c', 1, payload)]), 1234)
        packet = response([record(b'\xc0\x0c', 1, b'\1\2\3\4')])
        with self.assertRaises(probe.ProbeError):
            probe._check_dns(packet[:-1], 1234)

    def test_rejects_unrelated_or_wrong_class_a_record(self):
        for owner, klass in ((name('other.example.com'), 1), (b'\xc0\x0c', 3)):
            with self.subTest(owner=owner, klass=klass), self.assertRaisesRegex(probe.ProbeError, 'no A record'):
                probe._check_dns(response([record(owner, 1, b'\1\2\3\4', klass)]), 1234)

    def test_rejects_cname_loop_and_cname_without_address(self):
        for records in (
            [record(b'\xc0\x0c', 5, name('alias.example.com'))],
            [record(b'\xc0\x0c', 5, name('alias.example.com')),
             record(name('alias.example.com'), 5, b'\xc0\x0c')],
        ):
            with self.subTest(records=records), self.assertRaises(probe.ProbeError):
                probe._check_dns(response(records), 1234)

    def test_rejects_compression_loops_and_invalid_pointer_offsets(self):
        answer_offset = 12 + len(probe.DNS_QUESTION)
        self_pointer = struct.pack('!H', 0xC000 | answer_offset)
        for owner in (self_pointer, b'\xff\xff', b'\xc0\x00', b'\x40', b'\xc0'):
            with self.subTest(owner=owner), self.assertRaises(probe.ProbeError):
                probe._check_dns(response([record(owner, 1, b'\1\2\3\4')]), 1234)

    def test_rejects_bad_cname_length_and_oversized_names(self):
        for payload in (name('alias.example.com') + b'extra', b'\xc0', (b'\x3f' + b'a' * 63) * 4 + b'\0'):
            with self.subTest(payload=payload), self.assertRaises(probe.ProbeError):
                probe._check_dns(response([record(b'\xc0\x0c', 5, payload)]), 1234)

    def test_rejects_query_mismatch_errors_truncation_and_trailing_bytes(self):
        valid = [record(b'\xc0\x0c', 1, b'\1\2\3\4')]
        packets = [response(valid, ident=4321), response(valid, flags=0x8380),
                   response(valid, flags=0x8183), response(valid, flags=0x8980),
                   response(valid, flags=0x0180), response([]), response(valid) + b'\0']
        for packet in packets:
            with self.subTest(packet=packet), self.assertRaises(probe.ProbeError):
                probe._check_dns(packet, 1234)


class NodeAcceptance(unittest.TestCase):
    def run_node(self, actual, *, strict='', previous='203.0.113.10', dns_error=None):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, {'SOCKS_EXIT_EXPECTED_IP': strict}))
            stack.enter_context(mock.patch.object(probe, '_read_config', return_value={'protocol': 'vless'}))
            stack.enter_context(mock.patch.object(probe, '_local_port', return_value=12345))
            stack.enter_context(mock.patch.object(probe.subprocess, 'Popen'))
            stack.enter_context(mock.patch.object(probe, '_await_listener'))
            dns = stack.enter_context(mock.patch.object(probe, '_udp_dns', side_effect=dns_error))
            stack.enter_context(mock.patch.object(probe, '_https_exit', return_value=actual))
            stop = stack.enter_context(mock.patch.object(probe, '_stop'))
            result = probe._probe_node('fixture-xray', '.', 'fixture.json', previous)
            dns.assert_called_once()
            stop.assert_called_once()
            return result

    def test_default_accepts_rotating_exit_and_returns_actual_ip(self):
        self.assertEqual(self.run_node('203.0.113.11'), '203.0.113.11')

    def test_default_accepts_change_in_address_family(self):
        self.assertEqual(self.run_node('2001:db8::1'), '2001:db8::1')

    def test_explicit_strict_ip_accepts_actual_not_preflight_ip(self):
        self.assertEqual(self.run_node('203.0.113.11', strict='203.0.113.11'), '203.0.113.11')

    def test_explicit_strict_ip_rejects_wrong_exit(self):
        with self.assertRaisesRegex(probe.ProbeError, 'expected 203.0.113.10'):
            self.run_node('203.0.113.11', strict='203.0.113.10')

    def test_invalid_strict_ip_fails_before_client_launch(self):
        for value in ('invalid', '192.0.2.1:443', 'fe80::1%eth0'):
            with self.subTest(value=value), mock.patch.object(probe.subprocess, 'Popen') as popen:
                with mock.patch.dict(os.environ, {'SOCKS_EXIT_EXPECTED_IP': value}):
                    with mock.patch.object(probe, '_read_config', return_value={'protocol': 'vless'}):
                        with self.assertRaisesRegex(probe.ProbeError, 'SOCKS_EXIT_EXPECTED_IP'):
                            probe._probe_node('unused', '.', 'unused', '203.0.113.10')
                popen.assert_not_called()

    def test_dns_failure_still_fails_node(self):
        with self.assertRaisesRegex(probe.ProbeError, 'UDP DNS.*timed out'):
            self.run_node('203.0.113.11', dns_error=TimeoutError())

    def test_cli_prints_actual_ip_only(self):
        output = io.StringIO()
        with mock.patch.object(probe, '_probe_node', return_value='203.0.113.11'), redirect_stdout(output):
            self.assertEqual(probe.main(['node', 'xray', '.', 'client.json', '203.0.113.10']), 0)
        self.assertEqual(output.getvalue(), '203.0.113.11\n')


class UpstreamAcceptance(unittest.TestCase):
    def setUp(self):
        self.environment = mock.patch.dict(os.environ, {'SOCKS_EXIT_EXPECTED_IP': ''})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.config = {'protocol': 'socks', 'settings': {'servers': [{
            'address': '192.0.2.10', 'port': 1080,
            'users': [{'user': 'fixture-user', 'pass': 'private-fixture-password'}],
        }]}}

    def test_success_requires_https_and_tcp_dns(self):
        with mock.patch.object(probe, '_read_config', return_value=self.config), \
                mock.patch.object(probe, '_https_exit', return_value='203.0.113.10') as https, \
                mock.patch.object(probe, '_tcp_dns') as dns:
            self.assertEqual(probe._probe_upstream('unused'), '203.0.113.10')
            https.assert_called_once()
            dns.assert_called_once()

    def test_network_errors_do_not_expose_credentials(self):
        with mock.patch.object(probe, '_read_config', return_value=self.config), \
                mock.patch.object(probe, '_https_exit', side_effect=OSError('private-fixture-password')), \
                mock.patch.object(probe, '_tcp_dns') as dns:
            with self.assertRaises(probe.ProbeError) as caught:
                probe._probe_upstream('unused')
            self.assertNotIn('private-fixture-password', str(caught.exception))
            self.assertIn('HTTPS', str(caught.exception))
            dns.assert_not_called()

    def test_dns_failure_cannot_pass_upstream_preflight(self):
        with mock.patch.object(probe, '_read_config', return_value=self.config), \
                mock.patch.object(probe, '_https_exit', return_value='203.0.113.10'), \
                mock.patch.object(probe, '_tcp_dns', side_effect=TimeoutError()):
            with self.assertRaisesRegex(probe.ProbeError, 'DNS over TCP.*timed out'):
                probe._probe_upstream('unused')

    def test_invalid_strict_ip_fails_before_preflight_network(self):
        with mock.patch.dict(os.environ, {'SOCKS_EXIT_EXPECTED_IP': 'invalid'}), \
                mock.patch.object(probe, '_https_exit') as https:
            with self.assertRaisesRegex(probe.ProbeError, 'SOCKS_EXIT_EXPECTED_IP'):
                probe._probe_upstream('unused')
            https.assert_not_called()

    def test_socks_hostname_and_idna_use_the_same_authenticated_checks(self):
        for host, expected in (('proxy.example', 'proxy.example'), ('例子.example', 'xn--fsqu00a.example'),
                               ('localhost', 'localhost'), ('2001:db8::10', '2001:db8::10')):
            with self.subTest(host=host):
                config = deepcopy(self.config)
                config['settings']['servers'][0]['address'] = host
                with mock.patch.object(probe, '_read_config', return_value=config), \
                        mock.patch.object(probe, '_https_exit', return_value='203.0.113.10') as https, \
                        mock.patch.object(probe, '_tcp_dns') as dns:
                    self.assertEqual(probe._probe_upstream('unused'), '203.0.113.10')
                    endpoint = (expected, 1080, b'fixture-user', b'private-fixture-password')
                    https.assert_called_once_with(*endpoint)
                    dns.assert_called_once_with(*endpoint)

    def test_socks_whitelist_auth_accepts_missing_or_empty_users(self):
        for users in (None, []):
            with self.subTest(users=users):
                config = deepcopy(self.config)
                server = config['settings']['servers'][0]
                if users is None:
                    del server['users']
                else:
                    server['users'] = users
                with mock.patch.object(probe, '_read_config', return_value=config), \
                        mock.patch.object(probe, '_https_exit', return_value='203.0.113.10') as https, \
                        mock.patch.object(probe, '_tcp_dns'):
                    probe._probe_upstream('unused')
                    https.assert_called_once_with('192.0.2.10', 1080, b'', b'')

    def test_invalid_hosts_ports_users_or_transport_stop_before_network(self):
        configs = []
        for host in ('', 'proxy.example\nprivate-fixture-password', 'http://proxy.example',
                     'a..example', '[2001:db8::10]', '::', 'fe80::1%eth0', '224.0.0.1'):
            config = deepcopy(self.config)
            config['settings']['servers'][0]['address'] = host
            configs.append(config)
        for port in (True, '1080', 0, 65536):
            config = deepcopy(self.config)
            config['settings']['servers'][0]['port'] = port
            configs.append(config)
        for users in (None, {}, [{'user': 'fixture', 'pass': ''}],
                      [{'user': 'a' * 256, 'pass': 'private-fixture-password'}],
                      [{'user': 'fixture', 'pass': 'private-fixture-password\r'}]):
            config = deepcopy(self.config)
            config['settings']['servers'][0]['users'] = users
            configs.append(config)
        config = deepcopy(self.config)
        config['streamSettings'] = {'security': 'tls'}
        configs.append(config)
        for config in configs:
            with self.subTest(config=config), mock.patch.object(probe, '_read_config', return_value=config), \
                    mock.patch.object(probe, '_https_exit') as https:
                with self.assertRaises(probe.ProbeError) as error:
                    probe._probe_upstream('unused')
                self.assertNotIn('private-fixture-password', str(error.exception))
                https.assert_not_called()


class HttpUpstreamAcceptance(unittest.TestCase):
    def setUp(self):
        self.environment = mock.patch.dict(os.environ, {'SOCKS_EXIT_EXPECTED_IP': ''})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.config = {'tag': 'fixture-http', 'protocol': 'http', 'settings': {'servers': [{
            'address': 'proxy.example', 'port': 8080,
            'users': [{'user': 'fixture-user', 'pass': 'private-fixture-password'}],
        }]}}

    def run_upstream(self, config=None, *, https_error=None, dns_error=None, startup_error=None):
        config = self.config if config is None else config
        observed = {}
        process = mock.MagicMock()

        def capture(command, **kwargs):
            path = Path(command[-1])
            observed.update(path=path, config=json.loads(path.read_text()), launch=kwargs)
            return process

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(probe, '_read_config', return_value=config))
            stack.enter_context(mock.patch.object(probe, '_local_port', return_value=12345))
            stack.enter_context(mock.patch.object(probe.subprocess, 'Popen', side_effect=capture))
            stack.enter_context(mock.patch.object(probe, '_await_listener', side_effect=startup_error))
            https = stack.enter_context(mock.patch.object(probe, '_https_exit',
                return_value='203.0.113.10', side_effect=https_error))
            dns = stack.enter_context(mock.patch.object(probe, '_tcp_dns', side_effect=dns_error))
            stop = stack.enter_context(mock.patch.object(probe, '_stop'))
            try:
                result = probe._probe_upstream('fixture.json', 'fixture-xray', '.')
            finally:
                stop.assert_called_once_with(process.__enter__.return_value)
                self.assertFalse(observed['path'].exists(), 'private adapter configuration remained')
            https.assert_called_once_with('127.0.0.1', 12345, b'', b'')
            dns.assert_called_once_with('127.0.0.1', 12345, b'', b'')
        self.assertEqual(observed['config']['outbounds'], [config], 'preflight changed the deployed outbound')
        self.assertEqual(observed['config']['log']['loglevel'], 'none')
        return result

    def test_http_connect_preflight_uses_exact_outbound_and_both_tcp_checks(self):
        self.assertEqual(self.run_upstream(), '203.0.113.10')

    def test_https_proxy_transport_and_certificate_policy_are_preserved(self):
        config = deepcopy(self.config)
        config['streamSettings'] = {'security': 'tls', 'tlsSettings': {'serverName': 'proxy.example'}}
        self.assertEqual(self.run_upstream(config), '203.0.113.10')

    def test_whitelist_http_proxy_without_credentials(self):
        config = deepcopy(self.config)
        del config['settings']['servers'][0]['users']
        self.assertEqual(self.run_upstream(config), '203.0.113.10')

    def test_http_dns_failure_names_connect53_requirement_and_cleans_up(self):
        with self.assertRaises(probe.ProbeError) as error:
            self.run_upstream(dns_error=TimeoutError('private-fixture-password'))
        self.assertIn('DNS over TCP', str(error.exception))
        self.assertIn('CONNECT to 1.1.1.1:53', str(error.exception))
        self.assertNotIn('private-fixture-password', str(error.exception))

    def test_https_transfer_failure_is_reported_separately_and_cleans_up(self):
        with self.assertRaisesRegex(probe.ProbeError, 'HTTPS through the HTTP CONNECT exit failed'):
            self.run_upstream(https_error=OSError('private-fixture-password'))

    def test_adapter_startup_failure_also_stops_the_child(self):
        with self.assertRaisesRegex(probe.ProbeError, 'stopped before listening'):
            self.run_upstream(startup_error=probe.ProbeError('temporary Xray client stopped before listening'))

    def test_http_missing_binary_fails_before_starting_a_client(self):
        with mock.patch.object(probe, '_read_config', return_value=self.config), \
                mock.patch.object(probe.subprocess, 'Popen') as popen:
            with self.assertRaisesRegex(probe.ProbeError, 'requires the Xray binary'):
                probe._probe_upstream('unused')
            popen.assert_not_called()

    def test_http_colon_in_username_and_unsupported_transport_are_rejected(self):
        for stream, user in (({}, 'user:ambiguous'), ({'security': 'reality'}, 'fixture-user'),
                             ({'network': 'ws'}, 'fixture-user')):
            config = deepcopy(self.config)
            config['streamSettings'] = stream
            config['settings']['servers'][0]['users'][0]['user'] = user
            with self.subTest(stream=stream, user=user), \
                    mock.patch.object(probe, '_read_config', return_value=config), \
                    mock.patch.object(probe.subprocess, 'Popen') as popen:
                with self.assertRaises(probe.ProbeError):
                    probe._probe_upstream('unused', 'xray', '.')
                popen.assert_not_called()

    def test_extended_upstream_cli_passes_binary_and_working_directory(self):
        output = io.StringIO()
        with mock.patch.object(probe, '_probe_upstream', return_value='203.0.113.10') as upstream, \
                redirect_stdout(output):
            self.assertEqual(probe.main(['upstream', 'proxy.json', '/app/xray', '/app']), 0)
        upstream.assert_called_once_with('proxy.json', '/app/xray', '/app')
        self.assertEqual(output.getvalue(), '203.0.113.10\n')


if __name__ == '__main__':
    unittest.main()
