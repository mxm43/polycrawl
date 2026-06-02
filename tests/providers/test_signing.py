from __future__ import annotations

from packages.provider_impls.douyin.signing import compute_xbogus, sign_query, DEFAULT_UA


def test_xbogus_length_and_chars() -> None:
    """X-Bogus value must be 28 characters from the custom alphabet."""
    allowed = set("Dkdpgh4ZKsQB80/Mfvw36XI1R25-WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe=")
    payload = "sec_user_id=MS4wLjABAAAAtest&count=18&max_cursor=0&device_platform=webapp&aid=6383"
    xb = compute_xbogus(payload)
    assert len(xb) == 28, f"Expected 28 chars, got {len(xb)}"
    assert all(c in allowed for c in xb), f"Unexpected characters in: {xb}"


def test_sign_query_appends_xbogus() -> None:
    """sign_query must return original payload + '&X-Bogus=<28chars>'."""
    payload = "sec_user_id=ABC&count=18&max_cursor=0&device_platform=webapp&aid=6383"
    signed = sign_query(payload)
    assert signed.startswith(payload + "&X-Bogus="), "Signed query must start with original payload"
    xb_part = signed[len(payload) + len("&X-Bogus="):]
    assert len(xb_part) == 28


def test_xbogus_determinism_within_same_second() -> None:
    """Two calls within the same second must produce the same value
    (timestamp is the only non-deterministic input)."""
    import time
    payload = "sec_user_id=STABLE&count=18&max_cursor=0&device_platform=webapp&aid=6383"
    # Compute twice in tight succession 鈥?should be equal unless a second boundary passes.
    xb1 = compute_xbogus(payload)
    xb2 = compute_xbogus(payload)
    # Allow one retry if the second flipped between the two calls (extremely rare)
    if xb1 != xb2:
        xb2 = compute_xbogus(payload)
    assert xb1 == xb2, "X-Bogus must be deterministic for the same timestamp"


def test_xbogus_differs_for_different_payloads() -> None:
    """Different query strings must produce different X-Bogus values."""
    xb_a = compute_xbogus("sec_user_id=AAA&count=18&max_cursor=0&device_platform=webapp&aid=6383")
    xb_b = compute_xbogus("sec_user_id=BBB&count=18&max_cursor=0&device_platform=webapp&aid=6383")
    assert xb_a != xb_b
