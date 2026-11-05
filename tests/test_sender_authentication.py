"""Task 21: sender authentication evidence for inbound Gmail messages.

Nothing here sends mail. The evaluator must credit ONLY an aligned DMARC pass recorded by the
topmost trusted Authentication-Results header; everything else fails closed.
"""

from email import message_from_bytes

import pytest

from app.providers.email.imap import GmailImapProvider
from app.services.sender_authentication import (
    MAX_AUTH_RESULTS_LENGTH,
    SenderAuth,
    evaluate_sender_authentication,
    sender_auth_warning,
)

GOOD = (
    "mx.google.com; dkim=pass header.i=@acme.example header.s=s1 header.b=abc; "
    "spf=pass (google.com: domain of hr@acme.example designates 1.2.3.4 as permitted sender) "
    "smtp.mailfrom=hr@acme.example; dmarc=pass (p=REJECT sp=REJECT dis=NONE) "
    "header.from=acme.example"
)
FROM = "hr@acme.example"


def _eval(headers, from_address=FROM):
    return evaluate_sender_authentication(headers, from_address)


class TestEvaluator:
    def test_aligned_dmarc_pass_from_the_trusted_server(self):
        assert _eval([GOOD]) is SenderAuth.DMARC_ALIGNED

    def test_header_from_comparison_ignores_case_and_a_trailing_dot(self):
        assert _eval([GOOD.replace("acme.example", "ACME.example.")]) is SenderAuth.DMARC_ALIGNED
        assert _eval([GOOD], "HR@ACME.EXAMPLE") is SenderAuth.DMARC_ALIGNED

    def test_a_from_domain_that_differs_from_header_from_is_not_credited(self):
        assert _eval([GOOD], "hr@evil.example") is SenderAuth.UNVERIFIED

    def test_lookalike_and_subdomain_are_not_aligned(self):
        assert _eval([GOOD], "hr@acme.example.evil.example") is SenderAuth.UNVERIFIED
        assert _eval([GOOD], "hr@mail.acme.example") is SenderAuth.UNVERIFIED

    def test_spf_and_dkim_alone_are_never_enough(self):
        only_spf_dkim = (
            "mx.google.com; dkim=pass header.i=@acme.example; "
            "spf=pass smtp.mailfrom=hr@acme.example"
        )
        assert _eval([only_spf_dkim]) is SenderAuth.UNVERIFIED

    def test_spf_pass_for_an_unrelated_domain_does_not_help(self):
        header = (
            "mx.google.com; spf=pass smtp.mailfrom=bounce@evil.example; "
            "dkim=pass header.i=@evil.example; dmarc=pass header.from=evil.example"
        )
        assert _eval([header]) is SenderAuth.UNVERIFIED  # From domain is acme.example
        assert _eval([header], "x@evil.example") is SenderAuth.DMARC_ALIGNED  # the domain's own

    def test_explicit_dmarc_failure_is_failed(self):
        header = "mx.google.com; dmarc=fail (p=REJECT) header.from=acme.example"
        assert _eval([header]) is SenderAuth.FAILED

    @pytest.mark.parametrize("result", ["none", "temperror", "permerror", "softfail", "neutral"])
    def test_other_dmarc_results_are_unverified(self, result):
        header = f"mx.google.com; dmarc={result} header.from=acme.example"
        assert _eval([header]) is SenderAuth.UNVERIFIED

    def test_untrusted_authserv_id_is_ignored(self):
        forged = GOOD.replace("mx.google.com", "mx.attacker.example", 1)
        assert _eval([forged]) is SenderAuth.UNVERIFIED

    def test_a_forged_trusted_header_BELOW_the_real_one_cannot_upgrade_a_failure(self):
        real = "mx.google.com; dmarc=fail header.from=acme.example"
        assert _eval([real, GOOD]) is SenderAuth.FAILED

    def test_a_forged_header_below_an_untrusted_topmost_header_gets_no_credit(self):
        topmost = "mx.other-relay.example; spf=pass smtp.mailfrom=hr@acme.example"
        assert _eval([topmost, GOOD]) is SenderAuth.UNVERIFIED

    def test_authserv_id_must_match_exactly_not_by_substring(self):
        for bad in ("mx.google.com.evil.example", "evilmx.google.com", "google.com"):
            assert _eval([GOOD.replace("mx.google.com", bad, 1)]) is SenderAuth.UNVERIFIED

    def test_authserv_id_comments_cannot_smuggle_a_trusted_id(self):
        header = "mx.attacker.example (mx.google.com); dmarc=pass header.from=acme.example"
        assert _eval([header]) is SenderAuth.UNVERIFIED

    def test_a_trusted_text_inside_a_comment_is_not_a_result(self):
        header = "mx.google.com; (dmarc=pass header.from=acme.example)"
        assert _eval([header]) is SenderAuth.UNVERIFIED

    def test_two_dmarc_clauses_are_ambiguous(self):
        header = GOOD + "; dmarc=pass header.from=acme.example"
        assert _eval([header]) is SenderAuth.UNVERIFIED

    def test_missing_header_from_property_is_unverified(self):
        assert _eval(["mx.google.com; dmarc=pass"]) is SenderAuth.UNVERIFIED

    @pytest.mark.parametrize("value", [None, "", "not-an-address", "a@b@c", "@acme.example"])
    def test_unusable_from_address_is_unverified(self, value):
        assert _eval([GOOD], value) is SenderAuth.UNVERIFIED

    def test_no_headers_and_empty_header_are_unverified(self):
        assert _eval([]) is SenderAuth.UNVERIFIED
        assert _eval([""]) is SenderAuth.UNVERIFIED

    def test_control_characters_and_oversize_fail_closed(self):
        assert _eval([GOOD + "\x00"]) is SenderAuth.UNVERIFIED
        assert _eval([GOOD.replace("; dmarc", "\r\nX-Injected: 1; dmarc")]) is SenderAuth.UNVERIFIED
        assert _eval(["mx.google.com; " + "x" * MAX_AUTH_RESULTS_LENGTH]) is SenderAuth.UNVERIFIED

    def test_a_legitimately_folded_header_is_still_evaluated(self):
        folded = GOOD.replace("; ", ";\r\n\t", 2)
        assert _eval([folded]) is SenderAuth.DMARC_ALIGNED
        unfolded_break = folded.replace("\r\n\t", "\r\n", 1)
        assert _eval([unfolded_break]) is SenderAuth.UNVERIFIED

    # ---- review counterexamples: the grammar, not regex splitting, decides what a result is
    def test_text_inside_a_quoted_reason_is_not_a_dmarc_result(self):
        header = 'mx.google.com; spf=pass reason="text; dmarc=pass header.from=acme.example ; text"'
        assert _eval([header]) is SenderAuth.UNVERIFIED

    def test_complete_quoted_property_values_are_accepted(self):
        header = (
            'mx.google.com; dmarc=pass reason="all good; really" policy.note="a b" '
            "header.from=acme.example"
        )
        assert _eval([header]) is SenderAuth.DMARC_ALIGNED

    def test_a_quoted_string_with_semicolons_does_not_hide_the_real_result(self):
        header = (
            'mx.google.com; spf=pass reason="a; b"; dmarc=pass (p=NONE) header.from=acme.example'
        )
        assert _eval([header]) is SenderAuth.DMARC_ALIGNED

    def test_escaped_parenthesis_inside_a_comment_does_not_expose_text(self):
        header = r"mx.google.com; spf=pass (note \) ; dmarc=pass header.from=acme.example )"
        assert _eval([header]) is SenderAuth.UNVERIFIED

    def test_nested_comments_are_dropped(self):
        header = "mx.google.com; dmarc=pass (a (b) c) header.from=acme.example"
        assert _eval([header]) is SenderAuth.DMARC_ALIGNED

    @pytest.mark.parametrize(
        "header",
        [
            "mx.google.com; dmarc=pass-fail header.from=acme.example",
            "mx.google.com; dmarc=pass2 header.from=acme.example",
            "mx.google.com; dmarc = pass header.from=acme.example",
            "mx.google.com; dmarc=pass header.from=acme.example header.from=evil.example",
            'mx.google.com; dmarc=pass header.from="acme.example',
            "mx.google.com; dmarc=pass (unterminated comment header.from=acme.example",
            "mx.google.com; dmarc=pass ) header.from=acme.example",
            "mx.google.com; header.from=acme.example",
            "mx.google.com; none",
            'mx.google.com; dmarc=pass reason="ok"header.from=evil.example header.from=acme.ex',
            'mx.google.com; dmarc=pass policy.note="ok"header.from=evil.example header.from=a.ex',
            "mx.google.com; dmarc=pass reason=ok=header.from=evil.example header.from=acme.example",
            "mx.google.com; dmarc=pass policy.note=a=b header.from=acme.example",
            "mx.google.com; dmarc=pass policy.note=a/b header.from=acme.example",
            "mx.google.com; dmarc=pass header.from=acme.example,evil.example",
            'mx.google.com; dmarc=pass reason="ok" extra"quote header.from=acme.example',
            'mx.google.com; dmarc=pass policy.note=a"b header.from=acme.example',
            'mx.google.com; dmarc=pass policy.note="unterminated header.from=acme.example',
            "mx.google.com; dmarc=pass header.from=acme.example header.from = evil.example",
            "mx.google.com; dmarc=pass header.from=acme.example header (c) . from = evil.example",
            "mx.google.com; dmarc=pass header.from=acme.example header.from=",
            "mx.google.com; dmarc=pass header.from=acme.example =",
            "mx.google.com; dmarc=pass header.from=acme.example stray-token",
            "mx.google.com; dmarc=pass header.from=acme.example HEADER.FROM=evil.example",
        ],
    )
    def test_malformed_syntax_is_rejected(self, header):
        assert _eval([header]) is SenderAuth.UNVERIFIED

    def test_one_malformed_clause_gives_no_partial_credit(self):
        header = "mx.google.com; garbage-without-equals; dmarc=pass header.from=acme.example"
        assert _eval([header]) is SenderAuth.UNVERIFIED

    def test_authserv_id_is_exact_no_dot_stripping_or_extra_tokens(self):
        for bad in ("mx.google.com...", "mx.google.com.", "mx.google.com extra stuff"):
            assert _eval([GOOD.replace("mx.google.com", bad, 1)]) is SenderAuth.UNVERIFIED
        assert _eval([GOOD.replace("mx.google.com", "MX.GOOGLE.COM 1", 1)]) is (
            SenderAuth.DMARC_ALIGNED
        )

    def test_a_malformed_domain_is_never_aligned(self):
        malformed = "mx.google.com; dmarc=pass header.from=acme.example..."
        assert _eval([malformed], "hr@acme.example...") is SenderAuth.UNVERIFIED
        assert _eval([GOOD], "hr@acme..example") is SenderAuth.UNVERIFIED
        assert _eval([GOOD], "hr@-acme.example") is SenderAuth.UNVERIFIED
        assert _eval([GOOD], "hr@localhost") is SenderAuth.UNVERIFIED

    def test_internationalized_domains_are_never_converted_or_credited(self):
        """IDNA 2003 and 2008 disagree (`faß` vs `fass`); the safe answer is no credit."""
        for header_from in ("fass.example", "xn--fa-hia.example", "faß.example"):
            header = f"mx.google.com; dmarc=pass header.from={header_from}"
            assert _eval([header], "hr@faß.example") is SenderAuth.UNVERIFIED
        assert _eval([GOOD], "hr@münchen.example") is SenderAuth.UNVERIFIED
        punycode = "mx.google.com; dmarc=pass header.from=xn--mnchen-3ya.example"
        assert _eval([punycode], "hr@xn--mnchen-3ya.example") is SenderAuth.DMARC_ALIGNED

    def test_more_than_one_from_header_is_ambiguous(self):
        assert (
            evaluate_sender_authentication([GOOD], FROM, from_header_count=2)
            is SenderAuth.UNVERIFIED
        )
        assert (
            evaluate_sender_authentication([GOOD], FROM, from_header_count=0)
            is SenderAuth.UNVERIFIED
        )

    def test_non_string_input_never_raises(self):
        assert _eval([object()]) is SenderAuth.UNVERIFIED  # type: ignore[list-item]

    def test_warning_text_never_claims_identity(self):
        assert "not who" in sender_auth_warning(SenderAuth.DMARC_ALIGNED)
        for value in (None, "UNVERIFIED", "FAILED", "garbage"):
            assert "NOT authenticated" in sender_auth_warning(value)


def _raw(*auth_headers: str, from_header: str = "Recruiter <hr@acme.example>") -> bytes:
    lines = [f"Authentication-Results: {value}" for value in auth_headers]
    lines += [
        f"From: {from_header}",
        "To: me@example.com",
        "Subject: Offer",
        "Message-ID: <1@acme.example>",
        "Date: Mon, 03 Nov 2026 10:00:00 +0000",
        "",
        "Hello",
    ]
    return "\r\n".join(lines).encode()


def _provider() -> GmailImapProvider:
    return GmailImapProvider(
        imap_host="imap.gmail.com",
        imap_port=993,
        username="me@example.com",
        app_password="app-password",
        mailbox="INBOX",
        lookback_days=30,
        imap_client=object(),
    )


def _parsed(raw: bytes):
    return _provider()._parse_message(
        message_from_bytes(raw), uid=1, uid_validity=1, provider_arrival_at=None
    )


class TestImapParsing:
    def test_gmail_header_on_top_credits_the_from_domain(self):
        assert _parsed(_raw(GOOD)).sender_auth == "DMARC_ALIGNED"

    def test_no_header_is_unverified(self):
        assert _parsed(_raw()).sender_auth == "UNVERIFIED"

    def test_a_forged_header_below_the_real_failure_does_not_upgrade(self):
        real = "mx.google.com; dmarc=fail header.from=acme.example"
        assert _parsed(_raw(real, GOOD)).sender_auth == "FAILED"

    def test_a_display_name_with_a_trusted_domain_does_not_change_the_result(self):
        raw = _raw(GOOD, from_header='"hr@acme.example" <hr@evil.example>')
        assert _parsed(raw).sender_auth == "UNVERIFIED"

    def test_header_injection_through_folded_value_is_not_credited(self):
        injected = (
            "mx.attacker.example; spf=pass\r\n"
            " dmarc=pass header.from=acme.example\r\n"
            "Authentication-Results: mx.google.com; dmarc=pass header.from=acme.example"
        )
        # the injected second header sits BELOW the (untrusted) topmost one
        assert _parsed(_raw(injected)).sender_auth == "UNVERIFIED"

    def test_two_from_headers_are_unverified(self):
        raw = (
            b"Authentication-Results: " + GOOD.encode() + b"\r\n"
            b"From: hr@acme.example\r\nFrom: ceo@evil.example\r\n"
            b"To: me@example.com\r\nSubject: x\r\nMessage-ID: <1@a>\r\n\r\nhi"
        )
        assert _parsed(raw).sender_auth == "UNVERIFIED"

    def test_only_inbound_mail_is_ever_credited(self):
        provider = _provider()
        provider.trusted_outbound = True
        parsed = provider._parse_message(
            message_from_bytes(_raw(GOOD)), uid=1, uid_validity=1, provider_arrival_at=None
        )
        assert parsed.direction == "OUTBOUND" and parsed.sender_auth == "UNVERIFIED"

    def test_oversized_header_is_clamped_and_unverified(self):
        raw = _raw("mx.google.com; " + "a=b; " * 4000 + "dmarc=pass header.from=acme.example")
        assert _parsed(raw).sender_auth == "UNVERIFIED"
