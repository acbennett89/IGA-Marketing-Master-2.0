"""tests for ``claude_client.py``.

The Anthropic SDK is mocked at every entry point — no real API calls are
made, no real API keys are read. ``secret_store.get_anthropic_api_key`` is
monkeypatched to a stub for tests that exercise the public API.
"""

from __future__ import annotations

import io
import json
import logging
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pypdf
import pytest

# ----- Ensure src/ is importable when running from repo root -----------------
_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import anthropic  # noqa: E402  (after sys.path manipulation)

from iga_marketing_master_2 import claude_client  # noqa: E402
from iga_marketing_master_2.claude_client import (  # noqa: E402
    CONFIDENCE_LOW_THRESHOLD,
    DEFAULT_OPUS_MODEL,
    DEFAULT_SONNET_MODEL,
    ClaudeAuthError,
    ClaudeRateLimitError,
    ClaudeServerError,
    ExtractedField,
    build_record_field_tool_schema,
    extract_from_pdf,
    reextract_low_confidence_fields,
)


# ============================================================================
# Test doubles
# ============================================================================


@dataclass(slots=True)
class StubFieldEntry:
    is_required: bool = False


@dataclass(slots=True)
class StubFieldMap:
    """Minimal stand-in for the FieldMap protocol the module expects."""

    domain_tags: list[str] = field(default_factory=list)
    required_tags: set[str] = field(default_factory=set)
    notes: list[tuple[str, str]] = field(default_factory=list)

    def generate_domain_tag_enum(self) -> list[str]:
        return list(self.domain_tags)

    def lookup_by_domain_tag(self, domain_tag: str) -> StubFieldEntry | None:
        if domain_tag in self.required_tags:
            return StubFieldEntry(is_required=True)
        if domain_tag in self.domain_tags:
            return StubFieldEntry(is_required=False)
        return None

    def iter_notes_for_claude(self) -> Iterable[tuple[str, str]]:
        return list(self.notes)


class FakeUsage:
    def __init__(
        self,
        *,
        input_tokens: int = 1000,
        output_tokens: int = 500,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
    ) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens
        self.cache_read_input_tokens = cache_read_input_tokens


class FakeToolUseBlock:
    def __init__(self, name: str, input_payload: dict[str, Any]) -> None:
        self.type = "tool_use"
        self.name = name
        self.input = input_payload


class FakeMessage:
    def __init__(
        self,
        *,
        tool_uses: list[FakeToolUseBlock] | None = None,
        usage: FakeUsage | None = None,
        model: str = DEFAULT_SONNET_MODEL,
        stop_reason: str = "end_turn",
    ) -> None:
        self.id = "msg_test"
        self.model = model
        self.role = "assistant"
        self.type = "message"
        self.stop_reason = stop_reason
        self.stop_sequence = None
        self.content = list(tool_uses or [])
        self.usage = usage or FakeUsage()

    def model_dump(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "model": self.model,
            "stop_reason": self.stop_reason,
            "content": [
                {"type": b.type, "name": b.name, "input": b.input} for b in self.content
            ],
            "usage": {
                "input_tokens": self.usage.input_tokens,
                "output_tokens": self.usage.output_tokens,
                "cache_creation_input_tokens": self.usage.cache_creation_input_tokens,
                "cache_read_input_tokens": self.usage.cache_read_input_tokens,
            },
        }


def make_tool_use(
    *,
    domain_tag: str,
    value: Any,
    confidence: float,
    source_doc: str = "x.pdf",
    source_page: int = 1,
    source_quote: str = "...",
    needs_review: bool = False,
    repeatable_group: str | None = None,
    repeatable_index: int | None = None,
) -> FakeToolUseBlock:
    payload: dict[str, Any] = {
        "domain_tag": domain_tag,
        "value": value,
        "source_doc": source_doc,
        "source_page": source_page,
        "source_quote": source_quote,
        "confidence": confidence,
    }
    if needs_review:
        payload["needs_review"] = True
    if repeatable_group is not None:
        payload["repeatable_group"] = repeatable_group
    if repeatable_index is not None:
        payload["repeatable_index"] = repeatable_index
    return FakeToolUseBlock("record_extracted_field", payload)


# ============================================================================
# PDF helpers
# ============================================================================


def make_pdf_bytes(num_pages: int) -> bytes:
    """Build a real PDF with `num_pages` blank pages (using pypdf)."""
    writer = pypdf.PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


@pytest.fixture
def small_pdf(tmp_path: Path) -> Path:
    p = tmp_path / "small.pdf"
    p.write_bytes(make_pdf_bytes(3))
    return p


@pytest.fixture
def large_pdf(tmp_path: Path) -> Path:
    p = tmp_path / "big.pdf"
    p.write_bytes(make_pdf_bytes(200))
    return p


@pytest.fixture
def stub_field_map() -> StubFieldMap:
    return StubFieldMap(
        domain_tags=[
            "account.named_insured",
            "policy.gl.aggregate_limit",
            "vehicle.vin",
            "vehicle.year",
            "submission.effective_date",
        ],
        required_tags={"submission.effective_date"},
        notes=[("account.named_insured", "Look for 'Named Insured:' label.")],
    )


@pytest.fixture(autouse=True)
def _stub_secret_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a fake API key so _resolve_client doesn't raise."""
    monkeypatch.setattr(
        "iga_marketing_master_2.claude_client.secret_store.get_anthropic_api_key",
        lambda: "sk-test-fake",
    )


@pytest.fixture
def fake_anthropic(monkeypatch: pytest.MonkeyPatch):
    """Replace ``anthropic.Anthropic`` with a MagicMock factory.

    Tests can configure ``client_instance.messages.create.return_value`` or
    ``side_effect`` per scenario.
    """
    client_instance = MagicMock(name="anthropic_client")
    factory = MagicMock(name="anthropic_factory", return_value=client_instance)
    monkeypatch.setattr(
        "iga_marketing_master_2.claude_client.anthropic.Anthropic", factory
    )
    return client_instance


# ============================================================================
# Tool schema tests
# ============================================================================


def test_tool_schema_uses_field_map_enum(stub_field_map: StubFieldMap) -> None:
    schema = build_record_field_tool_schema(stub_field_map)
    assert schema["name"] == "record_extracted_field"
    domain_tag_prop = schema["input_schema"]["properties"]["domain_tag"]
    assert "enum" in domain_tag_prop
    assert domain_tag_prop["enum"] == sorted(stub_field_map.domain_tags)
    # Required fields per ARCHITECTURE.md §6.2.
    assert set(schema["input_schema"]["required"]) == {
        "domain_tag",
        "value",
        "source_doc",
        "source_page",
        "source_quote",
        "confidence",
    }


def test_tool_schema_omits_enum_when_field_map_empty() -> None:
    schema = build_record_field_tool_schema(StubFieldMap())
    domain_tag_prop = schema["input_schema"]["properties"]["domain_tag"]
    assert "enum" not in domain_tag_prop
    assert domain_tag_prop["type"] == "string"


# ============================================================================
# Cache breakpoint placement tests
# ============================================================================


def test_cache_breakpoints_placed_in_request(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
) -> None:
    fake_anthropic.messages.create.return_value = FakeMessage(
        tool_uses=[make_tool_use(domain_tag="account.named_insured", value="X", confidence=0.95)]
    )

    extract_from_pdf(
        small_pdf,
        stub_field_map,
        glossary="g" * 10000,
        system_prompt="s" * 10000,
        run_id="run-1",
    )

    assert fake_anthropic.messages.create.call_count == 1
    kwargs = fake_anthropic.messages.create.call_args.kwargs
    # Breakpoint 1: system block
    system_blocks = kwargs["system"]
    assert len(system_blocks) == 1
    assert system_blocks[0]["cache_control"] == {"type": "ephemeral"}
    # Breakpoint 2: first text block of the user message (Field Map)
    user_msg = kwargs["messages"][0]
    assert user_msg["role"] == "user"
    field_map_block = user_msg["content"][0]
    assert field_map_block["type"] == "text"
    assert field_map_block["cache_control"] == {"type": "ephemeral"}
    # PDF document block follows the Field Map block.
    document_block = user_msg["content"][1]
    assert document_block["type"] == "document"
    assert document_block["source"]["media_type"] == "application/pdf"
    # Final per-call text instruction is present and unmarked.
    assert user_msg["content"][2]["type"] == "text"
    assert "cache_control" not in user_msg["content"][2]
    # Tool plumbing
    assert len(kwargs["tools"]) == 1
    assert kwargs["tools"][0]["name"] == "record_extracted_field"
    assert kwargs["tool_choice"] == {"type": "any"}


def test_cache_breakpoints_clear_2048_token_minimum(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Both breakpoints in a normal-size run should NOT trigger the
    'undersized breakpoint' warning (per Sonnet 4.6 2,048-token min /
    PLAN-REVIEW Amendment #17). Use ~16K char prompt and ~16K char
    field-map (>>4096 chars => >>1024 tokens => >>2048-token check still
    passes since the field map prompt has many tags)."""
    # Make sure both blocks comfortably exceed 2048 * 4 chars = 8192 chars.
    big_glossary = "GLOSSARY LINE\n" * 800  # ~11K chars
    big_system = "SYSTEM LINE\n" * 800  # ~10K chars
    # Beef up domain tags so the field-map block clears the threshold.
    huge_field_map = StubFieldMap(
        domain_tags=[f"account.fake_field_{i:04d}" for i in range(2500)]
    )
    fake_anthropic.messages.create.return_value = FakeMessage(
        tool_uses=[make_tool_use(domain_tag="account.fake_field_0001", value="X", confidence=0.95)]
    )

    with caplog.at_level(logging.WARNING, logger="iga.claude"):
        extract_from_pdf(
            small_pdf,
            huge_field_map,
            glossary=big_glossary,
            system_prompt=big_system,
            run_id="run-cache-min",
        )

    undersized = [r for r in caplog.records if "cache_breakpoint_undersized" in r.getMessage()]
    assert undersized == []


# ============================================================================
# PDF split tests
# ============================================================================


def test_split_ranges_for_200_page_pdf() -> None:
    ranges = claude_client._compute_split_ranges(200)
    # Expected: 80 + 80 + 40 with 1-page overlap (chunk N's last == chunk N+1's first)
    # chunk 1: 1-80
    # chunk 2: 80-159
    # chunk 3: 159-200  (42 pages, but within MAX so OK)
    assert ranges == [(1, 80), (80, 159), (159, 200)]
    # Overlap is exactly 1 page between adjacent chunks.
    for prev, nxt in zip(ranges, ranges[1:]):
        assert prev[1] == nxt[0]


def test_split_ranges_for_exact_80_page_pdf() -> None:
    assert claude_client._compute_split_ranges(80) == [(1, 80)]


def test_split_ranges_for_81_page_pdf() -> None:
    # Just over: 1-80, 80-81 (overlap of 1)
    assert claude_client._compute_split_ranges(81) == [(1, 80), (80, 81)]


def test_extract_splits_200_page_pdf_into_three_calls(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    large_pdf: Path,
) -> None:
    # Each chunk returns one record; merge should produce 3 unique records.
    responses = [
        FakeMessage(
            tool_uses=[
                make_tool_use(
                    domain_tag="account.named_insured",
                    value=f"Chunk {i}",
                    confidence=0.9,
                )
            ]
        )
        for i in range(3)
    ]
    fake_anthropic.messages.create.side_effect = responses

    result = extract_from_pdf(
        large_pdf,
        stub_field_map,
        glossary="glossary",
        system_prompt="system",
        run_id="run-split",
    )

    # 3 API calls for split. (No escalation; confidence is 0.9.)
    assert fake_anthropic.messages.create.call_count == 3
    # Merge dedupes by (domain_tag, group, index): all 3 have same key, so
    # the highest-confidence wins. With equal confidence, last-write-wins.
    assert len(result) == 1
    assert result[0].domain_tag == "account.named_insured"
    # source_doc is rewritten to the original basename, never the chunk basename.
    assert result[0].source_doc == "big.pdf"


def test_split_calls_use_original_basename_in_extracted_records(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    large_pdf: Path,
) -> None:
    # Each chunk returns a *different* record; the merged result should
    # have all three with source_doc=big.pdf.
    fake_anthropic.messages.create.side_effect = [
        FakeMessage(
            tool_uses=[
                make_tool_use(
                    domain_tag=f"account.field_{i}",
                    value=f"v{i}",
                    confidence=0.9,
                    source_doc="claude-says-something-else.pdf",
                )
            ]
        )
        for i in range(3)
    ]

    result = extract_from_pdf(
        large_pdf,
        stub_field_map,
        glossary="g",
        system_prompt="s",
        run_id="run-split-2",
    )

    assert {r.domain_tag for r in result} == {
        "account.field_0",
        "account.field_1",
        "account.field_2",
    }
    for r in result:
        assert r.source_doc == "big.pdf"


def test_split_writes_chunks_to_debug_split_dir(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    large_pdf: Path,
    tmp_path: Path,
) -> None:
    fake_anthropic.messages.create.return_value = FakeMessage(
        tool_uses=[make_tool_use(domain_tag="account.named_insured", value="X", confidence=0.9)]
    )
    debug_dir = tmp_path / "client" / "debug" / "claude" / "run-x"
    extract_from_pdf(
        large_pdf,
        stub_field_map,
        glossary="g",
        system_prompt="s",
        run_id="run-x",
        debug_dir=debug_dir,
    )
    split_dir = tmp_path / "client" / "debug" / "split" / "run-x"
    assert split_dir.exists()
    chunks = sorted(split_dir.glob("*.pdf"))
    assert len(chunks) == 3


# ============================================================================
# Auto-escalation tests
# ============================================================================


def test_low_confidence_triggers_opus_reextract(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
) -> None:
    sonnet_response = FakeMessage(
        tool_uses=[
            make_tool_use(
                domain_tag="account.named_insured",
                value="Bobby Luttrell",
                confidence=0.97,
            ),
            make_tool_use(
                domain_tag="vehicle.vin",
                value="XYZ123",
                confidence=0.5,  # below threshold → escalate
                repeatable_group="vehicle",
                repeatable_index=0,
            ),
        ]
    )
    opus_response = FakeMessage(
        model=DEFAULT_OPUS_MODEL,
        tool_uses=[
            make_tool_use(
                domain_tag="vehicle.vin",
                value="1FA6P0HD3K5123456",
                confidence=0.95,
                repeatable_group="vehicle",
                repeatable_index=0,
            ),
        ],
    )
    fake_anthropic.messages.create.side_effect = [sonnet_response, opus_response]

    result = extract_from_pdf(
        small_pdf,
        stub_field_map,
        glossary="g",
        system_prompt="s",
        run_id="run-esc",
    )

    # 2 calls: 1 Sonnet pass + 1 Opus re-prompt.
    assert fake_anthropic.messages.create.call_count == 2
    sonnet_call = fake_anthropic.messages.create.call_args_list[0]
    opus_call = fake_anthropic.messages.create.call_args_list[1]
    assert sonnet_call.kwargs["model"] == DEFAULT_SONNET_MODEL
    assert opus_call.kwargs["model"] == DEFAULT_OPUS_MODEL

    by_tag = {(r.domain_tag, r.repeatable_index): r for r in result}
    # High-confidence Sonnet record kept as-is.
    assert by_tag[("account.named_insured", None)].model_used == DEFAULT_SONNET_MODEL
    # Escalated record replaced by Opus version, tagged opus.
    vin = by_tag[("vehicle.vin", 0)]
    assert vin.value == "1FA6P0HD3K5123456"
    assert vin.confidence == 0.95
    assert vin.model_used == DEFAULT_OPUS_MODEL


def test_needs_review_triggers_escalation(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
) -> None:
    sonnet_response = FakeMessage(
        tool_uses=[
            make_tool_use(
                domain_tag="account.named_insured",
                value="Maybe Inc",
                confidence=0.92,  # high confidence
                needs_review=True,  # but flagged for review → escalate
            ),
        ]
    )
    opus_response = FakeMessage(
        model=DEFAULT_OPUS_MODEL,
        tool_uses=[
            make_tool_use(
                domain_tag="account.named_insured",
                value="Definitely Inc",
                confidence=0.99,
            )
        ],
    )
    fake_anthropic.messages.create.side_effect = [sonnet_response, opus_response]

    result = extract_from_pdf(
        small_pdf,
        stub_field_map,
        glossary="g",
        system_prompt="s",
        run_id="run-needs-review",
    )

    assert fake_anthropic.messages.create.call_count == 2
    assert result[0].value == "Definitely Inc"
    assert result[0].model_used == DEFAULT_OPUS_MODEL


def test_required_missing_triggers_escalation(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
) -> None:
    # submission.effective_date is required; Sonnet returns null with high confidence.
    sonnet_response = FakeMessage(
        tool_uses=[
            make_tool_use(
                domain_tag="submission.effective_date",
                value=None,
                confidence=0.95,
            )
        ]
    )
    opus_response = FakeMessage(
        model=DEFAULT_OPUS_MODEL,
        tool_uses=[
            make_tool_use(
                domain_tag="submission.effective_date",
                value="2026-05-01",
                confidence=0.96,
            )
        ],
    )
    fake_anthropic.messages.create.side_effect = [sonnet_response, opus_response]

    result = extract_from_pdf(
        small_pdf,
        stub_field_map,
        glossary="g",
        system_prompt="s",
        run_id="run-req-missing",
    )

    assert fake_anthropic.messages.create.call_count == 2
    assert result[0].value == "2026-05-01"
    assert result[0].model_used == DEFAULT_OPUS_MODEL


def test_no_escalation_when_all_records_confident(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
) -> None:
    fake_anthropic.messages.create.return_value = FakeMessage(
        tool_uses=[
            make_tool_use(domain_tag="account.named_insured", value="X", confidence=0.95),
            make_tool_use(domain_tag="vehicle.vin", value="VIN", confidence=0.92),
        ]
    )

    result = extract_from_pdf(
        small_pdf,
        stub_field_map,
        glossary="g",
        system_prompt="s",
        run_id="run-no-esc",
    )

    # Single Sonnet call — no escalation.
    assert fake_anthropic.messages.create.call_count == 1
    for rec in result:
        assert rec.model_used == DEFAULT_SONNET_MODEL


# ============================================================================
# force_opus tests
# ============================================================================


def test_force_opus_skips_sonnet_first_pass(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
) -> None:
    fake_anthropic.messages.create.return_value = FakeMessage(
        model=DEFAULT_OPUS_MODEL,
        tool_uses=[
            make_tool_use(
                domain_tag="account.named_insured",
                value="X",
                confidence=0.5,  # would normally trigger escalation
            )
        ],
    )

    result = extract_from_pdf(
        small_pdf,
        stub_field_map,
        glossary="g",
        system_prompt="s",
        run_id="run-force-opus",
        force_opus=True,
    )

    # Exactly one call, with the Opus model — no escalation second call.
    assert fake_anthropic.messages.create.call_count == 1
    call_kwargs = fake_anthropic.messages.create.call_args.kwargs
    assert call_kwargs["model"] == DEFAULT_OPUS_MODEL
    assert result[0].model_used == DEFAULT_OPUS_MODEL


# ============================================================================
# Cache verification logging tests
# ============================================================================


def test_cache_miss_warning_logged_when_both_cache_tokens_zero(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the second call shows zero cache_creation AND zero cache_read,
    the module logs a WARNING tagged claude.cache_miss."""
    # Two chunks (force a split via large_pdf) → first call sets up the
    # cache, second call should hit it. Returning all zeros simulates a
    # genuine cache miss on the second call (e.g., a Field Map churn
    # between calls).
    big_pdf = tmp_path / "big.pdf"
    big_pdf.write_bytes(make_pdf_bytes(200))

    fake_anthropic.messages.create.side_effect = [
        FakeMessage(
            tool_uses=[
                make_tool_use(domain_tag="account.named_insured", value="X", confidence=0.9)
            ],
            usage=FakeUsage(
                cache_creation_input_tokens=50_000,
                cache_read_input_tokens=0,
            ),
        ),
        FakeMessage(
            tool_uses=[
                make_tool_use(
                    domain_tag="account.named_insured", value="Y", confidence=0.9
                )
            ],
            usage=FakeUsage(
                cache_creation_input_tokens=0,  # ← both zero on a cacheable call
                cache_read_input_tokens=0,
            ),
        ),
        FakeMessage(
            tool_uses=[
                make_tool_use(
                    domain_tag="account.named_insured", value="Z", confidence=0.9
                )
            ],
            usage=FakeUsage(
                cache_creation_input_tokens=0,
                cache_read_input_tokens=50_000,
            ),
        ),
    ]

    with caplog.at_level(logging.INFO, logger="iga.claude"):
        extract_from_pdf(
            big_pdf,
            stub_field_map,
            glossary="g",
            system_prompt="s",
            run_id="run-cachemiss",
        )

    cache_miss_warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and "cache_miss" in r.getMessage()
    ]
    assert len(cache_miss_warnings) == 1


def test_no_cache_miss_warning_on_first_call(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The first call in a session can't possibly hit the cache; we should
    not warn."""
    fake_anthropic.messages.create.return_value = FakeMessage(
        tool_uses=[
            make_tool_use(domain_tag="account.named_insured", value="X", confidence=0.95)
        ],
        usage=FakeUsage(
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )

    with caplog.at_level(logging.WARNING, logger="iga.claude"):
        extract_from_pdf(
            small_pdf,
            stub_field_map,
            glossary="g",
            system_prompt="s",
            run_id="run-first-call",
        )

    cache_miss_warnings = [r for r in caplog.records if "cache_miss" in r.getMessage()]
    assert cache_miss_warnings == []


def test_cache_usage_logged_at_info(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_anthropic.messages.create.return_value = FakeMessage(
        tool_uses=[make_tool_use(domain_tag="account.named_insured", value="X", confidence=0.95)],
        usage=FakeUsage(
            input_tokens=12_345,
            output_tokens=678,
            cache_creation_input_tokens=8_000,
            cache_read_input_tokens=0,
        ),
    )

    with caplog.at_level(logging.INFO, logger="iga.claude"):
        extract_from_pdf(
            small_pdf,
            stub_field_map,
            glossary="g",
            system_prompt="s",
            run_id="run-info",
        )

    info_lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("cache_creation=8000" in m for m in info_lines)
    assert any("input=12345" in m for m in info_lines)


# ============================================================================
# Retry tests
# ============================================================================


def _make_rate_limit_error() -> anthropic.RateLimitError:
    """Construct a RateLimitError without going over the network."""
    response = MagicMock()
    response.status_code = 429
    response.headers = {}
    response.request = MagicMock()
    return anthropic.RateLimitError("rate limit", response=response, body=None)


def _make_internal_error() -> anthropic.InternalServerError:
    response = MagicMock()
    response.status_code = 500
    response.headers = {}
    response.request = MagicMock()
    return anthropic.InternalServerError("server fail", response=response, body=None)


def _make_auth_error() -> anthropic.AuthenticationError:
    response = MagicMock()
    response.status_code = 401
    response.headers = {}
    response.request = MagicMock()
    return anthropic.AuthenticationError("bad key", response=response, body=None)


def test_retry_on_rate_limit_then_succeeds(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleep_calls: list[float] = []
    monkeypatch.setattr(
        "iga_marketing_master_2.claude_client.time.sleep",
        lambda s: sleep_calls.append(s),
    )

    fake_anthropic.messages.create.side_effect = [
        _make_rate_limit_error(),
        _make_rate_limit_error(),
        FakeMessage(
            tool_uses=[
                make_tool_use(domain_tag="account.named_insured", value="X", confidence=0.9)
            ]
        ),
    ]

    result = extract_from_pdf(
        small_pdf,
        stub_field_map,
        glossary="g",
        system_prompt="s",
        run_id="run-retry",
    )
    assert len(result) == 1
    assert fake_anthropic.messages.create.call_count == 3
    assert sleep_calls == [1, 2]


def test_retry_on_5xx_then_succeeds(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "iga_marketing_master_2.claude_client.time.sleep", lambda _s: None
    )
    fake_anthropic.messages.create.side_effect = [
        _make_internal_error(),
        FakeMessage(
            tool_uses=[
                make_tool_use(domain_tag="account.named_insured", value="X", confidence=0.9)
            ]
        ),
    ]

    extract_from_pdf(
        small_pdf,
        stub_field_map,
        glossary="g",
        system_prompt="s",
        run_id="run-retry-5xx",
    )
    assert fake_anthropic.messages.create.call_count == 2


def test_auth_error_not_retried(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
) -> None:
    fake_anthropic.messages.create.side_effect = _make_auth_error()
    with pytest.raises(ClaudeAuthError):
        extract_from_pdf(
            small_pdf,
            stub_field_map,
            glossary="g",
            system_prompt="s",
            run_id="run-auth-fail",
        )
    assert fake_anthropic.messages.create.call_count == 1


def test_rate_limit_exhausts_retries_then_raises(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "iga_marketing_master_2.claude_client.time.sleep", lambda _s: None
    )
    fake_anthropic.messages.create.side_effect = [_make_rate_limit_error()] * 5
    with pytest.raises(ClaudeRateLimitError):
        extract_from_pdf(
            small_pdf,
            stub_field_map,
            glossary="g",
            system_prompt="s",
            run_id="run-retry-out",
        )
    # 1 initial + 4 retries = 5 attempts.
    assert fake_anthropic.messages.create.call_count == 5


# ============================================================================
# Debug artifact tests
# ============================================================================


def test_debug_artifacts_written_when_debug_dir_set(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
    tmp_path: Path,
) -> None:
    fake_anthropic.messages.create.return_value = FakeMessage(
        tool_uses=[make_tool_use(domain_tag="account.named_insured", value="X", confidence=0.95)]
    )
    debug_dir = tmp_path / "client" / "debug" / "claude" / "run-debug"

    extract_from_pdf(
        small_pdf,
        stub_field_map,
        glossary="g",
        system_prompt="s",
        run_id="run-debug",
        debug_dir=debug_dir,
    )

    assert debug_dir.exists()
    req_files = sorted(debug_dir.glob("*.req.json"))
    resp_files = sorted(debug_dir.glob("*.resp.json"))
    assert len(req_files) == 1
    assert len(resp_files) == 1

    req_payload = json.loads(req_files[0].read_text(encoding="utf-8"))
    # PDF base64 should be elided to a hash placeholder.
    document_blocks = [
        b
        for msg in req_payload.get("messages", [])
        for b in msg.get("content", [])
        if isinstance(b, dict) and b.get("type") == "document"
    ]
    assert len(document_blocks) == 1
    assert document_blocks[0]["source"]["data"].startswith("<base64 elided sha256=")

    resp_payload = json.loads(resp_files[0].read_text(encoding="utf-8"))
    # Cache usage in the saved response payload.
    assert "usage" in resp_payload
    assert "cache_creation_input_tokens" in resp_payload["usage"]


def test_no_debug_artifacts_when_debug_dir_none(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
    tmp_path: Path,
) -> None:
    fake_anthropic.messages.create.return_value = FakeMessage(
        tool_uses=[make_tool_use(domain_tag="account.named_insured", value="X", confidence=0.95)]
    )
    extract_from_pdf(
        small_pdf,
        stub_field_map,
        glossary="g",
        system_prompt="s",
        run_id="run-no-debug",
        debug_dir=None,
    )
    # Nothing written under tmp_path.
    assert list(tmp_path.glob("**/*.req.json")) == []
    assert list(tmp_path.glob("**/*.resp.json")) == []


# ============================================================================
# Public API: reextract_low_confidence_fields
# ============================================================================


def test_reextract_low_confidence_fields_runs_opus(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
) -> None:
    fake_anthropic.messages.create.return_value = FakeMessage(
        model=DEFAULT_OPUS_MODEL,
        tool_uses=[
            make_tool_use(
                domain_tag="vehicle.vin",
                value="1FA6P0HD3K5123456",
                confidence=0.95,
            )
        ],
    )

    targets = [
        ExtractedField(
            domain_tag="vehicle.vin",
            value="XYZ",
            source_doc="small.pdf",
            source_page=1,
            source_quote="bad quote",
            confidence=0.4,
            model_used=DEFAULT_SONNET_MODEL,
        ),
    ]

    result = reextract_low_confidence_fields(
        targets,
        small_pdf,
        stub_field_map,
        system_prompt="s",
        run_id="run-reext",
        glossary="g",
    )

    assert fake_anthropic.messages.create.call_count == 1
    kwargs = fake_anthropic.messages.create.call_args.kwargs
    assert kwargs["model"] == DEFAULT_OPUS_MODEL
    assert len(result) == 1
    assert result[0].model_used == DEFAULT_OPUS_MODEL


def test_reextract_with_empty_targets_skips_api(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
) -> None:
    result = reextract_low_confidence_fields(
        [],
        small_pdf,
        stub_field_map,
        system_prompt="s",
        run_id="run-empty",
    )
    assert result == []
    assert fake_anthropic.messages.create.call_count == 0


def test_reextract_filters_to_requested_tags(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
) -> None:
    # Opus returns extra fields the caller didn't ask about; we drop them.
    fake_anthropic.messages.create.return_value = FakeMessage(
        model=DEFAULT_OPUS_MODEL,
        tool_uses=[
            make_tool_use(
                domain_tag="vehicle.vin", value="VINGOOD", confidence=0.95
            ),
            make_tool_use(
                domain_tag="account.named_insured",
                value="X",
                confidence=0.9,
            ),
        ],
    )
    targets = [
        ExtractedField(
            domain_tag="vehicle.vin",
            value="VINBAD",
            source_doc="small.pdf",
            source_page=1,
            source_quote="q",
            confidence=0.3,
            model_used=DEFAULT_SONNET_MODEL,
        ),
    ]

    result = reextract_low_confidence_fields(
        targets,
        small_pdf,
        stub_field_map,
        system_prompt="s",
        run_id="run-filter",
        glossary="g",
    )
    assert {r.domain_tag for r in result} == {"vehicle.vin"}


# ============================================================================
# Auth-key resolution
# ============================================================================


def test_extract_from_pdf_raises_auth_error_without_key(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "iga_marketing_master_2.claude_client.secret_store.get_anthropic_api_key",
        lambda: None,
    )
    with pytest.raises(ClaudeAuthError):
        extract_from_pdf(
            small_pdf,
            stub_field_map,
            glossary="g",
            system_prompt="s",
            run_id="no-key",
        )
    assert fake_anthropic.messages.create.call_count == 0


# ============================================================================
# Tool input parsing
# ============================================================================


def test_invalid_tool_input_is_skipped_not_raised(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Malformed tool_use blocks are warned-and-skipped, not raised."""
    fake_anthropic.messages.create.return_value = FakeMessage(
        tool_uses=[
            FakeToolUseBlock("record_extracted_field", {"missing": "fields"}),
            make_tool_use(domain_tag="account.named_insured", value="OK", confidence=0.9),
        ]
    )

    with caplog.at_level(logging.WARNING, logger="iga.claude"):
        result = extract_from_pdf(
            small_pdf,
            stub_field_map,
            glossary="g",
            system_prompt="s",
            run_id="run-bad-input",
        )

    assert {r.domain_tag for r in result} == {"account.named_insured"}
    bad_input = [r for r in caplog.records if "tool_input_invalid" in r.getMessage()]
    assert len(bad_input) == 1


def test_unknown_tool_name_warned_and_skipped(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_anthropic.messages.create.return_value = FakeMessage(
        tool_uses=[
            FakeToolUseBlock("some_other_tool", {}),
            make_tool_use(domain_tag="account.named_insured", value="X", confidence=0.9),
        ]
    )
    with caplog.at_level(logging.WARNING, logger="iga.claude"):
        result = extract_from_pdf(
            small_pdf,
            stub_field_map,
            glossary="g",
            system_prompt="s",
            run_id="run-unknown-tool",
        )
    assert len(result) == 1
    unexpected = [r for r in caplog.records if "unexpected_tool" in r.getMessage()]
    assert len(unexpected) == 1
