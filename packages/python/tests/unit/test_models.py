import pytest

from rfnry_senses.errors import InputError, ModelError
from rfnry_senses.models import CheckAnswer, Decided, Identity, Image, Text, as_decided


def test_a_media_part_carries_data_or_url_not_both() -> None:
    with pytest.raises(InputError, match="^models: image carries data or url, not both$"):
        Image(media_type="image/jpeg", data=b"x", url="https://example.com/a.jpg")
    with pytest.raises(InputError, match="^models: image carries data or url, not both$"):
        Image(media_type="image/jpeg")


def test_bytes_travel_as_standard_base64() -> None:
    image = Image(media_type="image/jpeg", data=b"\xff\xd8\xff")
    assert image.model_dump(mode="json")["data"] == "/9j/"
    assert Image.model_validate_json(image.model_dump_json()) == image
    assert Text(text="hi").model_dump() == {"type": "text", "text": "hi"}


def test_a_decision_is_read_by_its_fields() -> None:
    assert isinstance(as_decided({"done": CheckAnswer(probability=0.8)}), Decided)
    with pytest.raises(
        ModelError, match="^models: the decision model's answers do not fit the contract"
    ):
        as_decided({"done": {"type": "check", "probability": 2.0}})


def test_an_identity_has_a_role() -> None:
    ana = Identity(role="user", id="ana", name="Ana")
    assert ana.metadata == {} and Identity.model_validate(ana.model_dump()) == ana
