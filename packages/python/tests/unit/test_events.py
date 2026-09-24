from pydantic import TypeAdapter

from rfnry_senses.events import Closed, Event, Heard, Said
from rfnry_senses.models import Identity

ANA = Identity(role="user", id="ana", name="Ana")


def test_events_round_trip_by_kind() -> None:
    adapter: TypeAdapter[Event] = TypeAdapter(Event)
    for event in (
        Heard(scope="acme", text="a table for two", final=True, speaker=ANA),
        Said(scope="acme", text="For two.", heard="For two.", interrupted=False, triggered_by=None),
        Closed(scope="acme", reason="hung_up"),
    ):
        assert adapter.validate_json(event.model_dump_json()) == event


def test_every_event_carries_its_scope_and_time() -> None:
    dumped = Heard(scope="acme", text="hi", final=False, speaker=ANA).model_dump(mode="json")
    assert dumped["scope"] == "acme" and dumped["kind"] == "heard" and dumped["at"].endswith("Z")
