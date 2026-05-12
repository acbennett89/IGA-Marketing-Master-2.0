"""tests for ``claude_client.py``.

The Anthropic SDK is mocked at every entry point — no real API calls are
made, no real API keys are read. ``secret_store.get_anthropic_api_key`` is
monkeypatched to a stub for tests that exercise the public API.

The module operates in JSON-mode output (NOT tool use): the system prompt
embeds the literal expected JSON schema and Claude returns one JSON object
as text content per call. These tests fake that text content via
:class:`FakeMessage` whose ``content`` carries a single ``FakeTextBlock``.
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
    DEFAULT_OPUS_MODEL,
    DEFAULT_SONNET_MODEL,
    ClaudeAuthError,
    ClaudeParseError,
    ClaudeRateLimitError,
    ExtractedField,
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


class FakeTextBlock:
    """Stand-in for an Anthropic ``TextBlock`` returned in JSON mode."""

    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class FakeMessage:
    """Stand-in for an Anthropic ``Message`` carrying a single text block."""

    def __init__(
        self,
        *,
        text: str | None = None,
        content_blocks: list[Any] | None = None,
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
        if content_blocks is not None:
            self.content = list(content_blocks)
        elif text is not None:
            self.content = [FakeTextBlock(text)]
        else:
            self.content = [FakeTextBlock("{\"fields\": {}, \"repeatables\": {}}")]
        self.usage = usage or FakeUsage()

    def model_dump(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "model": self.model,
            "stop_reason": self.stop_reason,
            "content": [
                {"type": getattr(b, "type", None), "text": getattr(b, "text", None)}
                for b in self.content
            ],
            "usage": {
                "input_tokens": self.usage.input_tokens,
                "output_tokens": self.usage.output_tokens,
                "cache_creation_input_tokens": self.usage.cache_creation_input_tokens,
                "cache_read_input_tokens": self.usage.cache_read_input_tokens,
            },
        }


def make_field_entry(
    *,
    value: Any,
    confidence: float,
    source_page: int = 1,
    source_quote: str = "...",
    needs_review: bool = False,
) -> dict[str, Any]:
    """Build a single ``fields[<tag>]`` entry payload."""
    return {
        "value": value,
        "confidence": confidence,
        "source_page": source_page,
        "source_quote": source_quote,
        "needs_review": needs_review,
    }


def build_response_text(
    fields: dict[str, dict[str, Any]] | None = None,
    repeatables: dict[str, list[dict[str, Any]]] | None = None,
) -> str:
    """Serialize a JSON-mode response payload."""
    payload: dict[str, Any] = {
        "fields": fields or {},
        "repeatables": repeatables or {},
    }
    return json.dumps(payload)


def make_simple_message(
    *,
    fields: dict[str, dict[str, Any]] | None = None,
    repeatables: dict[str, list[dict[str, Any]]] | None = None,
    usage: FakeUsage | None = None,
    model: str = DEFAULT_SONNET_MODEL,
    stop_reason: str = "end_turn",
) -> FakeMessage:
    return FakeMessage(
        text=build_response_text(fields=fields, repeatables=repeatables),
        usage=usage,
        model=model,
        stop_reason=stop_reason,
    )


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

    Production code calls ``client.messages.stream(**req)`` (context
    manager) instead of ``messages.create``, but tests mock ``create`` for
    historical reasons. We bridge stream → create here so call_count,
    call_args, side_effect, and return_value on ``messages.create`` all
    keep working without rewriting every test.
    """
    client_instance = MagicMock(name="anthropic_client")

    def _stream_shim(**kwargs):
        msg = client_instance.messages.create(**kwargs)
        ctx = MagicMock(name="stream_ctx")
        stream_obj = MagicMock(name="stream_obj")
        stream_obj.get_final_message.return_value = msg
        ctx.__enter__.return_value = stream_obj
        ctx.__exit__.return_value = False
        return ctx

    client_instance.messages.stream.side_effect = _stream_shim

    factory = MagicMock(name="anthropic_factory", return_value=client_instance)
    monkeypatch.setattr(
        "iga_marketing_master_2.claude_client.anthropic.Anthropic", factory
    )
    return client_instance


# ============================================================================
# JSON-mode parser helpers
# ============================================================================


def test_parse_json_object_strips_markdown_fences() -> None:
    raw = '```json\n{"fields": {"a.b": {"value": 1, "confidence": 0.9, ' \
          '"source_page": 1, "source_quote": "x"}}}\n```'
    parsed = claude_client._parse_json_object(raw)
    assert "fields" in parsed
    assert parsed["fields"]["a.b"]["value"] == 1


def test_parse_json_object_strips_bare_fence() -> None:
    raw = '```\n{"fields": {}}\n```'
    parsed = claude_client._parse_json_object(raw)
    assert parsed == {"fields": {}}


def test_parse_json_object_unwraps_single_element_list() -> None:
    raw = '[{"fields": {}, "repeatables": {}}]'
    parsed = claude_client._parse_json_object(raw)
    assert parsed == {"fields": {}, "repeatables": {}}


def test_parse_json_object_raises_on_invalid_json() -> None:
    with pytest.raises(ClaudeParseError):
        claude_client._parse_json_object("not json {{{")


def test_parse_json_object_raises_on_empty_string() -> None:
    with pytest.raises(ClaudeParseError):
        claude_client._parse_json_object("")


def test_parse_json_object_raises_on_non_object_top_level() -> None:
    with pytest.raises(ClaudeParseError):
        claude_client._parse_json_object("[1, 2, 3]")


def test_extract_text_from_response_concatenates_text_blocks() -> None:
    msg = FakeMessage(content_blocks=[FakeTextBlock("foo "), FakeTextBlock("bar")])
    assert claude_client._extract_text_from_response(msg) == "foo bar"


def test_extract_text_from_response_returns_empty_for_no_text() -> None:
    msg = FakeMessage(content_blocks=[])
    assert claude_client._extract_text_from_response(msg) == ""


# ============================================================================
# Cache breakpoint placement tests
# ============================================================================


def test_cache_breakpoints_placed_in_request(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
) -> None:
    fake_anthropic.messages.create.return_value = make_simple_message(
        fields={"account.named_insured": make_field_entry(value="X", confidence=0.95)}
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
    # JSON mode: NO tools parameter passed.
    assert "tools" not in kwargs
    assert "tool_choice" not in kwargs


def test_cache_breakpoints_clear_2048_token_minimum(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Both breakpoints in a normal-size run should NOT trigger the
    'undersized breakpoint' warning (per Sonnet 4.6 2,048-token min /
    PLAN-REVIEW Amendment #17)."""
    big_glossary = "GLOSSARY LINE\n" * 800  # ~11K chars
    big_system = "SYSTEM LINE\n" * 800  # ~10K chars
    huge_field_map = StubFieldMap(
        domain_tags=[f"account.fake_field_{i:04d}" for i in range(2500)]
    )
    fake_anthropic.messages.create.return_value = make_simple_message(
        fields={
            "account.fake_field_0001": make_field_entry(value="X", confidence=0.95)
        }
    )

    with caplog.at_level(logging.WARNING, logger="iga.claude"):
        extract_from_pdf(
            small_pdf,
            huge_field_map,
            glossary=big_glossary,
            system_prompt=big_system,
            run_id="run-cache-min",
        )

    undersized = [
        r for r in caplog.records if "cache_breakpoint_undersized" in r.getMessage()
    ]
    assert undersized == []


# ============================================================================
# PDF split tests
# ============================================================================


def test_split_ranges_for_200_page_pdf() -> None:
    ranges = claude_client._compute_split_ranges(200)
    # Expected: 80 + 80 + 40 with 1-page overlap
    assert ranges == [(1, 80), (80, 159), (159, 200)]
    for prev, nxt in zip(ranges, ranges[1:]):
        assert prev[1] == nxt[0]


def test_split_ranges_for_exact_80_page_pdf() -> None:
    assert claude_client._compute_split_ranges(80) == [(1, 80)]


def test_split_ranges_for_81_page_pdf() -> None:
    assert claude_client._compute_split_ranges(81) == [(1, 80), (80, 81)]


def test_extract_splits_200_page_pdf_into_three_calls(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    large_pdf: Path,
) -> None:
    responses = [
        make_simple_message(
            fields={
                "account.named_insured": make_field_entry(
                    value=f"Chunk {i}", confidence=0.9
                )
            }
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

    assert fake_anthropic.messages.create.call_count == 3
    # All three chunks emitted the same domain_tag → one merged record.
    assert len(result) == 1
    assert result[0].domain_tag == "account.named_insured"
    assert result[0].source_doc == "big.pdf"


def test_split_calls_use_original_basename_in_extracted_records(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    large_pdf: Path,
) -> None:
    fake_anthropic.messages.create.side_effect = [
        make_simple_message(
            fields={
                f"account.field_{i}": make_field_entry(
                    value=f"v{i}", confidence=0.9
                )
            }
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
    fake_anthropic.messages.create.return_value = make_simple_message(
        fields={"account.named_insured": make_field_entry(value="X", confidence=0.9)}
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
# JSON mode → ExtractedField conversion
# ============================================================================


def test_basic_fields_converted_to_extracted_records(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
) -> None:
    fake_anthropic.messages.create.return_value = make_simple_message(
        fields={
            "account.named_insured": make_field_entry(
                value="Acme Inc",
                confidence=0.95,
                source_page=1,
                source_quote="Named Insured: Acme Inc",
            ),
            "policy.gl.aggregate_limit": make_field_entry(
                value="2,000,000",
                confidence=0.94,
                source_page=2,
                source_quote="General Aggregate Limit  $2,000,000",
            ),
        }
    )

    result = extract_from_pdf(
        small_pdf,
        stub_field_map,
        glossary="g",
        system_prompt="s",
        run_id="run-fields",
    )

    by_tag = {r.domain_tag: r for r in result}
    assert set(by_tag) == {"account.named_insured", "policy.gl.aggregate_limit"}
    ni = by_tag["account.named_insured"]
    assert ni.value == "Acme Inc"
    assert ni.confidence == 0.95
    assert ni.source_page == 1
    assert ni.source_quote == "Named Insured: Acme Inc"
    assert ni.repeatable_group is None
    assert ni.repeatable_index is None
    assert ni.source_doc == "small.pdf"


def test_repeatable_items_emit_one_record_per_subfield(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
) -> None:
    fake_anthropic.messages.create.return_value = make_simple_message(
        repeatables={
            "vehicle": [
                {
                    "vehicle.year": "2022",
                    "vehicle.make": "Freightliner",
                    "vehicle.vin": "1FUJGLDR3NLNN1234",
                    "_confidence": 0.93,
                    "_source_page": 4,
                    "_source_quote": "1 2022 FREIGHTLINER VIN 1FUJGLDR3NLNN1234",
                    "_needs_review": False,
                },
                {
                    "vehicle.year": "2023",
                    "vehicle.make": "Volvo",
                    "vehicle.vin": "4V4NC9EH3PN999999",
                    "_confidence": 0.91,
                    "_source_page": 4,
                    "_source_quote": "2 2023 VOLVO VIN 4V4NC9EH3PN999999",
                    "_needs_review": False,
                },
            ]
        }
    )

    result = extract_from_pdf(
        small_pdf,
        stub_field_map,
        glossary="g",
        system_prompt="s",
        run_id="run-rep",
    )

    # 2 vehicles × 3 sub-fields = 6 records.
    assert len(result) == 6
    by_index_tag = {(r.repeatable_index, r.domain_tag): r for r in result}
    v0_year = by_index_tag[(0, "vehicle.year")]
    assert v0_year.value == "2022"
    assert v0_year.repeatable_group == "vehicle"
    assert v0_year.repeatable_index == 0
    assert v0_year.source_page == 4
    assert v0_year.confidence == 0.93
    v1_make = by_index_tag[(1, "vehicle.make")]
    assert v1_make.value == "Volvo"
    assert v1_make.repeatable_index == 1


def test_fields_and_repeatables_combine_in_one_response(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
) -> None:
    fake_anthropic.messages.create.return_value = make_simple_message(
        fields={
            "account.named_insured": make_field_entry(value="X", confidence=0.95),
        },
        repeatables={
            "loss_payee": [
                {
                    "loss_payee.name": "First National",
                    "_confidence": 0.95,
                    "_source_page": 4,
                    "_source_quote": "Loss Payee: First National",
                    "_needs_review": False,
                }
            ]
        },
    )

    result = extract_from_pdf(
        small_pdf,
        stub_field_map,
        glossary="g",
        system_prompt="s",
        run_id="run-mix",
    )

    by_tag = {(r.domain_tag, r.repeatable_index): r for r in result}
    assert ("account.named_insured", None) in by_tag
    assert ("loss_payee.name", 0) in by_tag


def test_json_response_wrapped_in_markdown_fences_parses_correctly(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
) -> None:
    raw_payload = build_response_text(
        fields={
            "account.named_insured": make_field_entry(value="Acme", confidence=0.92)
        }
    )
    fenced = f"```json\n{raw_payload}\n```"
    fake_anthropic.messages.create.return_value = FakeMessage(text=fenced)

    result = extract_from_pdf(
        small_pdf,
        stub_field_map,
        glossary="g",
        system_prompt="s",
        run_id="run-fence",
    )
    assert len(result) == 1
    assert result[0].value == "Acme"


def test_malformed_json_raises_parse_error(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
) -> None:
    fake_anthropic.messages.create.return_value = FakeMessage(
        text="Sure, here's the data: {fields: stuff}"
    )
    with pytest.raises(ClaudeParseError):
        extract_from_pdf(
            small_pdf,
            stub_field_map,
            glossary="g",
            system_prompt="s",
            run_id="run-bad-json",
        )


def test_empty_response_text_raises_parse_error(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
) -> None:
    fake_anthropic.messages.create.return_value = FakeMessage(content_blocks=[])
    with pytest.raises(ClaudeParseError):
        extract_from_pdf(
            small_pdf,
            stub_field_map,
            glossary="g",
            system_prompt="s",
            run_id="run-empty",
        )


def test_invalid_field_payload_logs_and_skips(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A field entry missing required keys is warned-and-skipped; valid
    entries in the same response still emit records."""
    fake_anthropic.messages.create.return_value = make_simple_message(
        fields={
            "account.bad": {"value": "X"},  # missing confidence + source_page
            "account.named_insured": make_field_entry(value="OK", confidence=0.9),
        }
    )

    with caplog.at_level(logging.WARNING, logger="iga.claude"):
        result = extract_from_pdf(
            small_pdf,
            stub_field_map,
            glossary="g",
            system_prompt="s",
            run_id="run-bad-field",
        )

    assert {r.domain_tag for r in result} == {"account.named_insured"}
    bad_entry = [
        r for r in caplog.records if "json_field_invalid" in r.getMessage()
    ]
    assert bad_entry  # at least one warning emitted


def test_repeatable_with_per_subfield_metadata_honors_overrides(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
) -> None:
    """A sub-field that's itself a {value, confidence, ...} dict overrides
    item-level metadata."""
    fake_anthropic.messages.create.return_value = make_simple_message(
        repeatables={
            "vehicle": [
                {
                    "vehicle.year": {
                        "value": "2022",
                        "confidence": 0.99,
                        "source_page": 5,
                        "source_quote": "row says 2022",
                        "needs_review": False,
                    },
                    "vehicle.vin": "1XYZ",
                    "_confidence": 0.7,
                    "_source_page": 4,
                    "_source_quote": "fallback quote",
                    "_needs_review": True,
                }
            ]
        }
    )

    result = extract_from_pdf(
        small_pdf,
        stub_field_map,
        glossary="g",
        system_prompt="s",
        run_id="run-rep-override",
    )

    by_tag = {r.domain_tag: r for r in result}
    year = by_tag["vehicle.year"]
    assert year.confidence == 0.99
    assert year.source_page == 5
    assert year.needs_review is False
    vin = by_tag["vehicle.vin"]
    assert vin.confidence == 0.7
    assert vin.source_page == 4
    assert vin.needs_review is True


# ============================================================================
# Auto-escalation tests
# ============================================================================


def test_low_confidence_triggers_opus_reextract(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
) -> None:
    sonnet_response = make_simple_message(
        fields={
            "account.named_insured": make_field_entry(
                value="Bobby Luttrell", confidence=0.97
            ),
        },
        repeatables={
            "vehicle": [
                {
                    "vehicle.vin": "XYZ123",
                    "_confidence": 0.5,  # below threshold → escalate
                    "_source_page": 1,
                    "_source_quote": "...",
                    "_needs_review": False,
                }
            ]
        },
    )
    opus_response = make_simple_message(
        model=DEFAULT_OPUS_MODEL,
        repeatables={
            "vehicle": [
                {
                    "vehicle.vin": "1FA6P0HD3K5123456",
                    "_confidence": 0.95,
                    "_source_page": 1,
                    "_source_quote": "VIN 1FA6P0HD3K5123456",
                    "_needs_review": False,
                }
            ]
        },
    )
    fake_anthropic.messages.create.side_effect = [sonnet_response, opus_response]

    result = extract_from_pdf(
        small_pdf,
        stub_field_map,
        glossary="g",
        system_prompt="s",
        run_id="run-esc",
    )

    assert fake_anthropic.messages.create.call_count == 2
    sonnet_call = fake_anthropic.messages.create.call_args_list[0]
    opus_call = fake_anthropic.messages.create.call_args_list[1]
    assert sonnet_call.kwargs["model"] == DEFAULT_SONNET_MODEL
    assert opus_call.kwargs["model"] == DEFAULT_OPUS_MODEL

    by_tag = {(r.domain_tag, r.repeatable_index): r for r in result}
    assert by_tag[("account.named_insured", None)].model_used == DEFAULT_SONNET_MODEL
    vin = by_tag[("vehicle.vin", 0)]
    assert vin.value == "1FA6P0HD3K5123456"
    assert vin.confidence == 0.95
    assert vin.model_used == DEFAULT_OPUS_MODEL


def test_needs_review_triggers_escalation(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
) -> None:
    sonnet_response = make_simple_message(
        fields={
            "account.named_insured": make_field_entry(
                value="Maybe Inc",
                confidence=0.92,  # high confidence
                needs_review=True,  # but flagged for review → escalate
            )
        }
    )
    opus_response = make_simple_message(
        model=DEFAULT_OPUS_MODEL,
        fields={
            "account.named_insured": make_field_entry(
                value="Definitely Inc", confidence=0.99
            )
        },
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
    sonnet_response = make_simple_message(
        fields={
            "submission.effective_date": make_field_entry(
                value=None, confidence=0.95
            )
        }
    )
    opus_response = make_simple_message(
        model=DEFAULT_OPUS_MODEL,
        fields={
            "submission.effective_date": make_field_entry(
                value="2026-05-01", confidence=0.96
            )
        },
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
    fake_anthropic.messages.create.return_value = make_simple_message(
        fields={
            "account.named_insured": make_field_entry(value="X", confidence=0.95),
            "vehicle.vin": make_field_entry(value="VIN", confidence=0.92),
        }
    )

    result = extract_from_pdf(
        small_pdf,
        stub_field_map,
        glossary="g",
        system_prompt="s",
        run_id="run-no-esc",
    )

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
    fake_anthropic.messages.create.return_value = make_simple_message(
        model=DEFAULT_OPUS_MODEL,
        fields={
            "account.named_insured": make_field_entry(
                value="X", confidence=0.5  # would normally trigger escalation
            )
        },
    )

    result = extract_from_pdf(
        small_pdf,
        stub_field_map,
        glossary="g",
        system_prompt="s",
        run_id="run-force-opus",
        force_opus=True,
    )

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
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the second call shows zero cache_creation AND zero cache_read,
    the module logs a WARNING tagged claude.cache_miss."""
    big_pdf = tmp_path / "big.pdf"
    big_pdf.write_bytes(make_pdf_bytes(200))

    fake_anthropic.messages.create.side_effect = [
        make_simple_message(
            fields={
                "account.named_insured": make_field_entry(value="X", confidence=0.9)
            },
            usage=FakeUsage(
                cache_creation_input_tokens=50_000,
                cache_read_input_tokens=0,
            ),
        ),
        make_simple_message(
            fields={
                "account.named_insured": make_field_entry(value="Y", confidence=0.9)
            },
            usage=FakeUsage(
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
        ),
        make_simple_message(
            fields={
                "account.named_insured": make_field_entry(value="Z", confidence=0.9)
            },
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
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "cache_miss" in r.getMessage()
    ]
    assert len(cache_miss_warnings) == 1


def test_no_cache_miss_warning_on_first_call(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_anthropic.messages.create.return_value = make_simple_message(
        fields={
            "account.named_insured": make_field_entry(value="X", confidence=0.95)
        },
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
    fake_anthropic.messages.create.return_value = make_simple_message(
        fields={
            "account.named_insured": make_field_entry(value="X", confidence=0.95)
        },
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
        make_simple_message(
            fields={
                "account.named_insured": make_field_entry(value="X", confidence=0.9)
            }
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
        make_simple_message(
            fields={
                "account.named_insured": make_field_entry(value="X", confidence=0.9)
            }
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
    fake_anthropic.messages.create.return_value = make_simple_message(
        fields={
            "account.named_insured": make_field_entry(value="X", confidence=0.95)
        }
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
    document_blocks = [
        b
        for msg in req_payload.get("messages", [])
        for b in msg.get("content", [])
        if isinstance(b, dict) and b.get("type") == "document"
    ]
    assert len(document_blocks) == 1
    assert document_blocks[0]["source"]["data"].startswith("<base64 elided sha256=")

    resp_payload = json.loads(resp_files[0].read_text(encoding="utf-8"))
    assert "usage" in resp_payload
    assert "cache_creation_input_tokens" in resp_payload["usage"]


def test_no_debug_artifacts_when_debug_dir_none(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
    tmp_path: Path,
) -> None:
    fake_anthropic.messages.create.return_value = make_simple_message(
        fields={
            "account.named_insured": make_field_entry(value="X", confidence=0.95)
        }
    )
    extract_from_pdf(
        small_pdf,
        stub_field_map,
        glossary="g",
        system_prompt="s",
        run_id="run-no-debug",
        debug_dir=None,
    )
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
    fake_anthropic.messages.create.return_value = make_simple_message(
        model=DEFAULT_OPUS_MODEL,
        fields={
            "vehicle.vin": make_field_entry(
                value="1FA6P0HD3K5123456", confidence=0.95
            )
        },
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
    fake_anthropic.messages.create.return_value = make_simple_message(
        model=DEFAULT_OPUS_MODEL,
        fields={
            "vehicle.vin": make_field_entry(value="VINGOOD", confidence=0.95),
            "account.named_insured": make_field_entry(value="X", confidence=0.9),
        },
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
# Field Map prompt block (Bug 3 regression)
# ============================================================================


def test_serialize_field_map_against_real_field_map_clears_2048_tokens() -> None:
    """Regression for Bug 3: the on-disk Field Map JSON must produce a
    prompt block that comfortably exceeds the 2,048-token Sonnet 4.6
    cache-prefix minimum (RESEARCH.md Finding 5)."""
    from iga_marketing_master_2 import field_map as field_map_mod

    fm = field_map_mod.load()
    text = claude_client._serialize_field_map_for_prompt(fm)
    approx_tokens = len(text) // 4
    assert approx_tokens > 2048, (
        f"Field Map prompt block only {approx_tokens} tokens — Sonnet's "
        f"2048-token cache threshold won't activate, and Claude won't see "
        f"the EPIC field universe."
    )
    assert "## Screen:" in text
    assert "label=" in text
    assert "name=" in text
    assert "domain_tag" in text and "lowercase" in text


def test_call_outbound_and_call_returned_logged(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression for Bug 6: every Anthropic round-trip must emit
    ``claude.call_outbound`` BEFORE ``messages.create`` and
    ``claude.call_returned`` AFTER."""
    fake_anthropic.messages.create.return_value = make_simple_message(
        fields={
            "account.named_insured": make_field_entry(value="X", confidence=0.95)
        }
    )
    with caplog.at_level(logging.INFO, logger="iga.claude"):
        extract_from_pdf(
            small_pdf,
            stub_field_map,
            glossary="g",
            system_prompt="s",
            run_id="run-diag",
        )
    info_lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    outbound = [m for m in info_lines if "claude.call_outbound" in m]
    returned = [m for m in info_lines if "claude.call_returned" in m]
    assert len(outbound) == 1, info_lines
    assert len(returned) == 1, info_lines
    assert "model=" in outbound[0]
    assert "api_key_fingerprint=" in outbound[0]
    assert "response_id=" in returned[0]
    assert "stop_reason=" in returned[0]


def test_serialize_field_map_handles_empty_enum_with_grammar_reminder() -> None:
    """When the enum is empty (bootstrap state), the block should still
    include the grammar reminder so Claude knows the target format."""
    empty_fm = StubFieldMap()
    text = claude_client._serialize_field_map_for_prompt(empty_fm)
    assert "lowercase" in text
    assert "domain_tag" in text
    assert "free-text domain_tag" in text


# ============================================================================
# Bug 7 regression — per-call usage attached to returned list
# ============================================================================


def test_extract_from_pdf_attaches_cache_usage_to_returned_list(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
) -> None:
    """Bug 7 fix: claude_client must attach a cache_usage block to its
    returned list so extract.py can aggregate cache + token totals."""
    fake_anthropic.messages.create.return_value = make_simple_message(
        fields={
            "account.named_insured": make_field_entry(value="X", confidence=0.95)
        },
        usage=FakeUsage(
            input_tokens=23_217,
            output_tokens=175,
            cache_creation_input_tokens=42_772,
            cache_read_input_tokens=0,
        ),
    )

    result = extract_from_pdf(
        small_pdf,
        stub_field_map,
        glossary="g",
        system_prompt="s",
        run_id="run-usage",
    )

    usage = getattr(result, "cache_usage", None)
    assert usage is not None, "extract_from_pdf must attach cache_usage"
    assert usage.input_tokens == 23_217
    assert usage.output_tokens == 175
    assert usage.cache_creation_input_tokens == 42_772
    assert usage.cache_read_input_tokens == 0
    assert usage.api_calls == 1


def test_extract_from_pdf_sums_usage_across_split_chunks(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    large_pdf: Path,
) -> None:
    """For a multi-chunk PDF, cache_usage on the returned list must equal
    the per-chunk totals summed across all chunks. Bug 7 regression."""
    fake_anthropic.messages.create.side_effect = [
        make_simple_message(
            fields={
                "account.named_insured": make_field_entry(
                    value=f"v{i}", confidence=0.9
                )
            },
            usage=FakeUsage(
                input_tokens=1_000 * (i + 1),
                output_tokens=100 * (i + 1),
                cache_creation_input_tokens=10_000 * (i + 1),
                cache_read_input_tokens=5_000 * (i + 1),
            ),
        )
        for i in range(3)
    ]

    result = extract_from_pdf(
        large_pdf,
        stub_field_map,
        glossary="g",
        system_prompt="s",
        run_id="run-multi-chunk",
    )

    usage = getattr(result, "cache_usage", None)
    assert usage is not None
    assert usage.api_calls == 3
    assert usage.input_tokens == 6_000
    assert usage.output_tokens == 600
    assert usage.cache_creation_input_tokens == 60_000
    assert usage.cache_read_input_tokens == 30_000


def test_reextract_low_confidence_fields_attaches_cache_usage(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
) -> None:
    """Re-extract path must propagate usage too — Opus calls cost more
    so the operator-visible aggregate especially matters here."""
    fake_anthropic.messages.create.return_value = make_simple_message(
        model=DEFAULT_OPUS_MODEL,
        fields={"vehicle.vin": make_field_entry(value="VIN", confidence=0.95)},
        usage=FakeUsage(
            input_tokens=8_000,
            output_tokens=200,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=42_000,
        ),
    )

    targets = [
        ExtractedField(
            domain_tag="vehicle.vin",
            value="VINBAD",
            source_doc="small.pdf",
            source_page=1,
            source_quote="q",
            confidence=0.4,
            model_used=DEFAULT_SONNET_MODEL,
        ),
    ]
    result = reextract_low_confidence_fields(
        targets,
        small_pdf,
        stub_field_map,
        system_prompt="s",
        run_id="run-reext-usage",
        glossary="g",
    )

    usage = getattr(result, "cache_usage", None)
    assert usage is not None
    assert usage.api_calls == 1
    assert usage.cache_read_input_tokens == 42_000
    assert usage.input_tokens == 8_000


def test_extract_from_pdf_includes_opus_escalation_in_usage_total(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
) -> None:
    """When auto-escalation kicks in (Sonnet → Opus), the returned
    cache_usage must include BOTH the Sonnet first-pass and the Opus
    second-pass numbers."""
    sonnet_response = make_simple_message(
        repeatables={
            "vehicle": [
                {
                    "vehicle.vin": "XYZ",
                    "_confidence": 0.5,
                    "_source_page": 1,
                    "_source_quote": "...",
                    "_needs_review": False,
                }
            ]
        },
        usage=FakeUsage(
            input_tokens=10_000,
            output_tokens=300,
            cache_creation_input_tokens=20_000,
            cache_read_input_tokens=0,
        ),
    )
    opus_response = make_simple_message(
        model=DEFAULT_OPUS_MODEL,
        repeatables={
            "vehicle": [
                {
                    "vehicle.vin": "GOOD_VIN",
                    "_confidence": 0.95,
                    "_source_page": 1,
                    "_source_quote": "...",
                    "_needs_review": False,
                }
            ]
        },
        usage=FakeUsage(
            input_tokens=3_000,
            output_tokens=120,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=20_000,
        ),
    )
    fake_anthropic.messages.create.side_effect = [sonnet_response, opus_response]

    result = extract_from_pdf(
        small_pdf,
        stub_field_map,
        glossary="g",
        system_prompt="s",
        run_id="run-esc-usage",
    )

    usage = getattr(result, "cache_usage", None)
    assert usage is not None
    assert usage.api_calls == 2
    assert usage.input_tokens == 13_000
    assert usage.output_tokens == 420
    assert usage.cache_creation_input_tokens == 20_000
    assert usage.cache_read_input_tokens == 20_000


# ============================================================================
# JSON-mode contract: NO tools parameter is ever sent
# ============================================================================


def test_messages_create_never_receives_tools_parameter(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
) -> None:
    """Hard contract: the JSON-mode client never passes ``tools`` or
    ``tool_choice`` to ``messages.create``. Tool use was the v1 approach
    that Sonnet 4.6 didn't reliably honor — this test guards against
    regression."""
    fake_anthropic.messages.create.return_value = make_simple_message(
        fields={
            "account.named_insured": make_field_entry(value="X", confidence=0.95)
        }
    )
    extract_from_pdf(
        small_pdf,
        stub_field_map,
        glossary="g",
        system_prompt="s",
        run_id="run-no-tools",
    )
    kwargs = fake_anthropic.messages.create.call_args.kwargs
    assert "tools" not in kwargs
    assert "tool_choice" not in kwargs


def test_user_instruction_describes_json_mode_output(
    fake_anthropic: MagicMock,
    stub_field_map: StubFieldMap,
    small_pdf: Path,
) -> None:
    """The per-call user instruction must tell Claude to return a JSON
    object (not a tool call). Without this the model could regress to
    prose output."""
    fake_anthropic.messages.create.return_value = make_simple_message(
        fields={
            "account.named_insured": make_field_entry(value="X", confidence=0.95)
        }
    )
    extract_from_pdf(
        small_pdf,
        stub_field_map,
        glossary="g",
        system_prompt="s",
        run_id="run-instruction",
    )
    kwargs = fake_anthropic.messages.create.call_args.kwargs
    user_msg = kwargs["messages"][0]
    instruction_block = user_msg["content"][2]
    assert instruction_block["type"] == "text"
    instruction = instruction_block["text"].lower()
    assert "json" in instruction
    assert "fields" in instruction
    assert "repeatables" in instruction
