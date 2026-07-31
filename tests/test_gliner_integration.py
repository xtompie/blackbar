"""What the local model actually detects.

Skipped unless the model is already downloaded, so the normal test run stays fast and
offline. To run it:

    pip install '.[detect]' && blackbar model pull
    pytest tests/test_gliner_integration.py
"""

from __future__ import annotations

import pytest

from blackbar.detect import Redactor
from blackbar.detect.gliner_layer import GlinerDetector
from blackbar.detect.rules import RulesDetector
from blackbar.vault import Vault

MODEL = "urchade/gliner_multi_pii-v1"


def _model_available() -> bool:
    try:
        from huggingface_hub import snapshot_download

        snapshot_download(MODEL, local_files_only=True)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _model_available(), reason="GLiNER model not downloaded")


@pytest.fixture(scope="module")
def detector() -> GlinerDetector:
    gliner = GlinerDetector(MODEL, threshold=0.5)
    assert gliner.load(), gliner.error
    return gliner


def found(detector: GlinerDetector, text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for span in detector.detect(text):
        out.setdefault(span.kind, []).append(span.text)
    return out


def test_company_is_caught_whole_not_just_the_name(detector):
    """The legal suffix must stay inside the span, otherwise the bare surname leaks."""
    hits = found(detector, "przygotuj przypomnienie dla Kowalski Sp. z o.o. o zaleglej fakturze")
    assert hits["company"] == ["Kowalski Sp. z o.o."]


def test_inflected_person_is_caught_whole(detector):
    """Polish inflection is why offsets matter - "Jana Kowalskiego" is not a substring
    of "Jan Kowalski"."""
    hits = found(detector, "napisz do Jana Kowalskiego w sprawie umowy")
    assert any("Kowalskiego" in name for name in hits["person"])


def test_person_company_and_email_in_one_sentence(detector):
    hits = found(detector, "napisz do Jana Kowalskiego z Kowalski Sp. z o.o. na jan@fhu.pl")
    assert hits["person"] and hits["company"]
    assert hits["company"] == ["Kowalski Sp. z o.o."]


def test_address_is_one_span(detector):
    hits = found(detector, "klient: Anna Nowak, ul. Dluga 5, 00-001 Warszawa")
    assert hits["person"] == ["Anna Nowak"]
    assert "Warszawa" in hits["address"][0]


def test_english_works_too(detector):
    hits = found(detector, "send a reminder to Acme Ltd about the overdue invoice")
    assert hits["company"] == ["Acme Ltd"]


def test_plain_sentence_is_left_alone(detector):
    assert found(detector, "popraw literowke w naglowku i zrob commit") == {}


async def test_all_layers_together(tmp_path, detector):
    """The whole redaction path with the model in place."""
    vault = Vault()
    redactor = Redactor(vault, RulesDetector(tmp_path / "missing.yaml"), gliner=detector)
    original = "napisz do Jana Kowalskiego z Kowalski Sp. z o.o. na jan@fhu.pl"

    masked, kinds, layers, keys = await redactor.redact(original)

    assert "Kowalski" not in masked
    assert "jan@fhu.pl" not in masked
    assert kinds["person"] == 1
    assert kinds["company"] == 1
    # The email is a regex hit, not a model one - the cheaper layer wins the overlap.
    assert layers["regex"] >= 1 and layers["gliner"] >= 2

    restored, count, orphans = vault.restore(masked)
    assert restored == original
    assert orphans == 0
