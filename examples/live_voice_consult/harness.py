from rfnry_engine import Engine

from providers import AnthropicProvider

INSTRUCTIONS = (
    "You are the voice concierge of the Acme bistro. Keep every answer to one short sentence. "
    "You know nothing about tonight yourself: for any question about tables, hours or the "
    "terrace, call the consult tool first and answer from what it says, in your own words, as "
    "your own knowledge. Never mention the tool or anyone behind you."
)

engine = Engine("agents", generation=AnthropicProvider())
consult = engine.tool(
    "core",
    scope="acme",
    name="consult",
    description="Look up tonight's tables, hours and the terrace.",
)
