"""Who is calling behind a gateway: the header is believed only from a named proxy."""

from __future__ import annotations

import pytest

from vectrixdb.api.forwarded import caller, client_address, parse_proxies, trusted_from_env

PROXIES = parse_proxies("10.0.0.0/8, 192.168.1.5")


class FakeClient:
    def __init__(self, host):
        self.host = host


class FakeRequest:
    def __init__(self, peer, forwarded=None):
        self.client = FakeClient(peer) if peer else None
        self.headers = {"x-forwarded-for": forwarded} if forwarded else {}


class TestParsing:
    def test_addresses_and_networks_both_read(self):
        networks = parse_proxies("10.0.0.0/8 192.168.1.5, 2001:db8::/32")
        assert [str(n) for n in networks] == ["10.0.0.0/8", "192.168.1.5/32", "2001:db8::/32"]

    def test_nothing_is_no_networks(self):
        assert parse_proxies("") == () and parse_proxies(None) == ()

    def test_a_value_nobody_can_parse_is_refused(self):
        """Silently reading it as "trust nothing" would leave the lockout
        counting the gateway, which is the bug this setting exists to fix."""
        with pytest.raises(ValueError) as info:
            parse_proxies("10.0.0.0/8, apim.company.com")
        assert "apim.company.com" in str(info.value)

    def test_the_environment_is_read(self, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_TRUSTED_PROXIES", "10.0.0.0/8")
        assert [str(n) for n in trusted_from_env()] == ["10.0.0.0/8"]
        monkeypatch.delenv("VECTRIXDB_TRUSTED_PROXIES")
        assert trusted_from_env() == ()


class TestWhoIsCalling:
    def test_without_a_proxy_list_the_header_is_ignored(self):
        assert caller("10.0.0.9", "203.0.113.7", ()) == "10.0.0.9"

    def test_a_trusted_peer_hands_over_the_address_it_saw(self):
        assert caller("10.0.0.9", "203.0.113.7", PROXIES) == "203.0.113.7"

    def test_an_untrusted_peer_cannot_name_its_own_address(self):
        """The header is a claim. From anybody but a named proxy it is a way
        to dodge the lockout and the guest limit, so it is not read."""
        assert caller("198.51.100.4", "203.0.113.7", PROXIES) == "198.51.100.4"

    def test_our_own_hops_are_dropped_from_the_right(self):
        assert caller("10.0.0.9", "203.0.113.7, 10.1.2.3, 192.168.1.5", PROXIES) == "203.0.113.7"

    def test_a_chain_of_only_our_own_gives_the_oldest(self):
        assert caller("10.0.0.9", "10.1.1.1, 10.2.2.2", PROXIES) == "10.1.1.1"

    def test_an_empty_header_leaves_the_peer(self):
        assert caller("10.0.0.9", "", PROXIES) == "10.0.0.9"
        assert caller("10.0.0.9", None, PROXIES) == "10.0.0.9"

    def test_no_peer_at_all(self):
        assert caller(None, "203.0.113.7", PROXIES) is None

    def test_ipv6_peers_and_chains(self):
        networks = parse_proxies("2001:db8::/32")
        assert caller("2001:db8::1", "2001:db8:1::99, 2001:db8::2", networks) == "2001:db8:1::99"


class TestOnARequest:
    def test_a_request_behind_a_named_proxy(self, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_TRUSTED_PROXIES", "10.0.0.0/8")
        assert client_address(FakeRequest("10.0.0.9", "203.0.113.7")) == "203.0.113.7"

    def test_a_request_with_the_setting_unset(self, monkeypatch):
        monkeypatch.delenv("VECTRIXDB_TRUSTED_PROXIES", raising=False)
        assert client_address(FakeRequest("10.0.0.9", "203.0.113.7")) == "10.0.0.9"
