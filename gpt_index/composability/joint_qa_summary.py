"""Joint QA Summary graph."""


from gpt_index.composability.base import BaseGraph
from gpt_index.indices.base import DOCUMENTS_INPUT, BaseGPTIndex


class JointQASummaryGraph(BaseGraph):
    """Joint QA Summary graph."""

    @classmethod
    def build_graph(
        cls,
    ) -> "JointQASummaryGraph":
        """Build graph from index."""
