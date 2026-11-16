"""The schema both examples use. Importing this module registers it.

MetaRelay's own fiber-photometry and microscopy schemas arrive in #10 and #11.
Until then, the examples register a small one the way an application would.
"""

# [docs:schema]
import pydantic

from metarelay import register_schema


class Implant(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")  # required: see Concepts

    fiber_diameter_um: float
    target_region: str


register_schema("lab.implant", 1, Implant)
# [/docs:schema]
