"""Framework-independent business logic boundary.

Regular-demand preparation, forecasting and replenishment arithmetic live
here. Versioned manager approval is implemented in the service layer because
it persists workflow state. Domain modules must not import Streamlit or access
source files directly.
"""
