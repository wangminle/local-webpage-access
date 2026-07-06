"""V1 验收样例夹具包（WBS-27）。"""

from tests.fixtures.samples import (
    BUILD_FAILURE,
    EXPECTED_KIND,
    FASTAPI_SQLITE,
    NODE_EXPRESS,
    PENDING_UNKNOWN,
    SAMPLES,
    STATIC_HTML,
    VITE_REACT,
    build_all,
    build_zip,
)

__all__ = [
    "STATIC_HTML",
    "VITE_REACT",
    "NODE_EXPRESS",
    "FASTAPI_SQLITE",
    "BUILD_FAILURE",
    "PENDING_UNKNOWN",
    "SAMPLES",
    "EXPECTED_KIND",
    "build_zip",
    "build_all",
]
