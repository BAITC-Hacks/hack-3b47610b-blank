"""Framework-independent business logic boundary.

Regular-demand preparation and stage-8 forecasting live here. Replenishment
and approval remain outside the implemented boundary. Domain modules must not
import Streamlit or access source files directly.
"""
